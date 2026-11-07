import io
import unittest
from unittest import mock

import cv2
import numpy as np

import tests  # noqa: F401
from tests import test_api as _api
from backend.app import floors
from backend.app.db import Camera, Floor, SessionLocal

GOOD_SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 200"><rect width="400" height="200" '
            b'fill="#eee"/><text x="10" y="20">Plan</text></svg>')


def png(w=200, h=100):
    ok, buf = cv2.imencode(".png", np.full((h, w, 3), 200, np.uint8))
    return buf.tobytes()


def jpeg(w=120, h=120):
    ok, buf = cv2.imencode(".jpg", np.full((h, w, 3), 90, np.uint8))
    return buf.tobytes()


class SvgCheckTests(unittest.TestCase):
    def test_good_svg_gives_its_size(self):
        self.assertEqual(floors.check_svg(GOOD_SVG), (400, 200))

    def test_width_height_without_viewbox(self):
        self.assertEqual(floors.check_svg(b'<svg xmlns="http://www.w3.org/2000/svg" width="640px" height="320"/>'), (640, 320))

    def test_dangerous_svgs_are_refused(self):
        ns = 'xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 10 10"'
        bad = {
            "script": f'<svg {ns}><script>alert(1)</script></svg>',
            "onload": f'<svg {ns} onload="alert(1)"/>',
            "onclick": f'<svg {ns}><rect onclick="x()" width="1" height="1"/></svg>',
            "foreignObject": f'<svg {ns}><foreignObject><div/></foreignObject></svg>',
            "external href": f'<svg {ns}><image href="http://evil.test/a.png"/></svg>',
            "xlink href": f'<svg {ns}><image xlink:href="https://evil.test/a.png"/></svg>',
            "data uri": f'<svg {ns}><image href="data:image/png;base64,AAAA"/></svg>',
            "js url": f'<svg {ns}><a href="javascript:alert(1)"><rect/></a></svg>',
            "css import": f'<svg {ns}><style>@import url(http://evil.test/x.css);</style></svg>',
            "css url": f'<svg {ns}><rect style="fill:url(http://evil.test/x)"/></svg>',
            "doctype": f'<!DOCTYPE svg [<!ENTITY a "aaaa">]><svg {ns}/>',
            "animate": f'<svg {ns}><animate attributeName="href" values="javascript:alert(1)"/></svg>',
        }
        for name, svg in bad.items():
            with self.subTest(name):
                with self.assertRaises(floors.FloorError):
                    floors.check_svg(svg.encode())

    def test_internal_references_are_allowed(self):
        svg = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><defs><rect id="a" width="1" height="1"/></defs>'
               b'<use href="#a"/></svg>')
        self.assertEqual(floors.check_svg(svg), (10, 10))

    def test_not_an_svg_or_broken(self):
        for data in (b"<html></html>", b"<svg", b"\xff\xfe<svg/>"):
            with self.subTest(data=data):
                with self.assertRaises(floors.FloorError):
                    floors.check_svg(data)


class PrepareImageTests(unittest.TestCase):
    def test_png_and_jpeg_become_png(self):
        for data, size in ((png(200, 100), (200, 100)), (jpeg(), (120, 120))):
            out = floors.prepare_image(data)
            self.assertEqual(out["mime"], "image/png")
            self.assertEqual((out["width"], out["height"]), size)
            self.assertTrue(out["image"].startswith(b"\x89PNG"))

    def test_large_image_is_reduced(self):
        out = floors.prepare_image(png(3000, 1500))
        self.assertEqual(max(out["width"], out["height"]), floors.MAX_PIXELS)
        self.assertEqual(out["width"] / out["height"], 2.0)

    def test_unsupported_empty_corrupt_and_oversized(self):
        for data, status in ((b"", 400), (b"GIF89a....", 400), (b"\x89PNG\r\n\x1a\nbroken", 400),
                             (b"\xff\xd8\xffbroken", 400), (b"x" * (floors.MAX_UPLOAD_BYTES + 1), 413)):
            with self.subTest(data=data[:12]):
                with self.assertRaises(floors.FloorError) as cm:
                    floors.prepare_image(data)
                self.assertEqual(cm.exception.status, status)


class FloorApiTests(_api.ApiTests):
    def setUp(self):
        super().setUp()
        s = SessionLocal()
        s.query(Floor).delete()
        s.commit()
        s.close()

    def add_floor(self, name="Rez-de-chaussée", level="0", data=None, filename="plan.png"):
        return self.client.post("/api/floors", data={"name": name, "level": level},
                                files={"file": (filename, io.BytesIO(data or png()), "application/octet-stream")})

    def test_no_floor_by_default(self):
        j = self.client.get("/api/floors").json()
        self.assertEqual(j["floors"], [])
        self.assertEqual(j["default_plan"], "/static/site-plan.svg")

    def test_create_list_and_serve_image(self):
        r = self.add_floor()
        self.assertEqual(r.status_code, 201, r.text)
        f = r.json()
        self.assertEqual((f["name"], f["width"], f["height"]), ("Rez-de-chaussée", 200, 100))
        img = self.client.get(f["image_url"])
        self.assertEqual(img.status_code, 200)
        self.assertEqual(img.headers["content-type"], "image/png")
        self.assertEqual(img.headers["x-content-type-options"], "nosniff")
        self.assertIn("sandbox", img.headers["content-security-policy"])
        self.assertEqual([x["id"] for x in self.client.get("/api/floors").json()["floors"]], [f["id"]])

    def test_svg_floor_is_served_with_a_locked_down_policy(self):
        f = self.add_floor(data=GOOD_SVG, filename="p.svg").json()
        img = self.client.get(f["image_url"])
        self.assertEqual(img.headers["content-type"], "image/svg+xml")
        self.assertIn("default-src 'none'", img.headers["content-security-policy"])

    def test_floors_are_sorted_by_level(self):
        self.add_floor("Étage 1", "1")
        self.add_floor("Sous-sol", "-1")
        self.add_floor("RDC", "0")
        self.assertEqual([f["name"] for f in self.client.get("/api/floors").json()["floors"]], ["Sous-sol", "RDC", "Étage 1"])

    def test_validation_errors(self):
        for kwargs, code in (({"name": ""}, 400), ({"name": "x" * 41}, 400), ({"level": "abc"}, 400),
                             ({"level": "500"}, 400), ({"data": b"junk"}, 400), ({"data": b"<svg><script/></svg>"}, 400)):
            with self.subTest(kwargs=kwargs):
                args = {"name": "Ok", **kwargs}
                self.assertEqual(self.add_floor(**args).status_code, code)
        self.assertEqual(self.client.get("/api/floors").json()["floors"], [])

    def test_error_messages_are_plain_strings(self):
        r = self.add_floor(name="")
        self.assertIsInstance(r.json()["detail"], str)

    def test_maximum_number_of_floors(self):
        with mock.patch.object(floors, "MAX_FLOORS", 2):
            self.add_floor("A"); self.add_floor("B")
            self.assertEqual(self.add_floor("C").status_code, 409)

    def test_rename_and_replace_image(self):
        f = self.add_floor().json()
        r = self.client.put(f"/api/floors/{f['id']}", data={"name": "Hall", "level": "2"})
        self.assertEqual((r.json()["name"], r.json()["level"], r.json()["ratio_changed"]), ("Hall", 2, False))
        r = self.client.put(f"/api/floors/{f['id']}", files={"file": ("p.png", io.BytesIO(png(100, 200)), "image/png")})
        self.assertEqual((r.json()["width"], r.json()["height"], r.json()["ratio_changed"]), (100, 200, True))
        self.assertEqual(self.client.put("/api/floors/999", data={"name": "x"}).status_code, 404)

    def test_place_cameras_on_a_floor(self):
        f = self.add_floor().json()
        r = self.client.put("/api/admin/cameras/entre",
                            json={"floor_id": f["id"], "x": .25, "y": .5, "heading": 360, "fov": 90, "view_range": .2})
        self.assertEqual(r.status_code, 200, r.text)
        cam = r.json()
        self.assertEqual((cam["floor_id"], cam["x"], cam["y"], cam["heading"], cam["fov"], cam["view_range"]),
                         (f["id"], .25, .5, 0.0, 90.0, .2))                   # 360° est ramené à 0°
        meta = self.client.get("/api/cameras").json()["camera_meta"]["entre"]
        self.assertEqual((meta["floor_id"], meta["heading"], meta["fov"], meta["placed"]), (f["id"], 0.0, 90.0, True))
        self.assertEqual(self.client.get("/api/floors").json()["floors"][0]["cameras"], ["entre"])

    def test_invalid_placement_is_refused(self):
        f = self.add_floor().json()
        for body in ({"floor_id": 999}, {"fov": 5}, {"fov": 400}, {"view_range": 0}, {"view_range": 2},
                     {"heading": "nord"}, {"heading": 450}, {"heading": -1}, {"floor_id": 0}, {"x": 1.5}):
            with self.subTest(body=body):
                self.assertIn(self.client.put("/api/admin/cameras/entre", json=body).status_code, (400, 422))
        self.assertEqual(self.client.put("/api/admin/cameras/entre", json={"floor_id": f["id"]}).status_code, 200)

    def test_unplaced_camera_is_flagged(self):
        meta = self.client.get("/api/cameras").json()["camera_meta"]["entre"]
        self.assertFalse(meta["placed"])
        self.assertIsNone(meta["floor_id"])

    def test_deleting_a_floor_unplaces_its_cameras(self):
        f = self.add_floor().json()
        self.client.put("/api/admin/cameras/entre", json={"floor_id": f["id"], "x": .2, "y": .2, "heading": 10, "fov": 60})
        r = self.client.delete(f"/api/floors/{f['id']}").json()
        self.assertEqual(r, {"deleted": True, "cameras_unplaced": 1})
        meta = self.client.get("/api/cameras").json()["camera_meta"]["entre"]
        self.assertEqual((meta["floor_id"], meta["placed"], meta["heading"]), (None, False, None))
        s = SessionLocal(); row = s.get(Camera, "entre"); s.close()
        self.assertIsNone(row.floor_id)
        self.assertEqual(self.client.delete(f"/api/floors/{f['id']}").status_code, 404)

    def test_image_of_unknown_floor_and_page(self):
        self.assertEqual(self.client.get("/api/floors/999/image").status_code, 404)
        self.assertEqual(self.client.get("/map").status_code, 200)

    def test_old_database_gets_the_new_columns(self):
        from sqlalchemy import create_engine, inspect, text
        import tempfile, os
        from backend.app import db as dbmod
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        eng = create_engine(f"sqlite:///{path}")
        with eng.begin() as c:
            c.execute(text("CREATE TABLE cameras (id VARCHAR PRIMARY KEY, site_id VARCHAR NOT NULL, label VARCHAR NOT NULL, "
                           "location VARCHAR, source TEXT NOT NULL, enabled BOOLEAN NOT NULL, x FLOAT, y FLOAT, "
                           "capture_width INTEGER, capture_height INTEGER, process_fps FLOAT, position INTEGER NOT NULL, "
                           "created_at DATETIME, updated_at DATETIME)"))
            c.execute(text("INSERT INTO cameras (id, site_id, label, source, enabled, position) VALUES ('a','p','A','0',1,0)"))
        with mock.patch.object(dbmod, "engine", eng):
            dbmod.init_db()
        cols = {c["name"] for c in inspect(eng).get_columns("cameras")}
        self.assertTrue({"floor_id", "heading", "fov", "view_range"} <= cols)
        self.assertIn("floors", inspect(eng).get_table_names())
        with eng.connect() as c:
            self.assertEqual(c.execute(text("SELECT label, floor_id FROM cameras")).fetchall(), [("A", None)])


for _name in [n for n in dir(_api.ApiTests) if n.startswith("test_") and n not in FloorApiTests.__dict__]:
    setattr(FloorApiTests, _name, None)

if __name__ == "__main__":
    unittest.main()
