import unittest

import tests  # noqa: F401
from tests import test_api as _api
from backend.app import config, main
from backend.app.db import Detection, SessionLocal


class DemoApiOutsideDemoTests(_api.ApiTests):
    """Hors mode démonstration : rien ne doit être modifiable ni simulé."""

    def test_status_says_not_demo(self):
        self.assertEqual(self.client.get("/api/demo").json(), {"demo": False})

    def test_page_is_served(self):
        r = self.client.get("/demo")
        self.assertEqual(r.status_code, 200)
        self.assertIn("demo.js", r.text)

    def test_mutations_are_refused_and_data_untouched(self):
        s = SessionLocal(); before = s.query(Detection).count(); s.close()
        for path in ("/api/demo/restart", "/api/demo/reset"):
            with self.subTest(path=path):
                r = self.client.post(path)
                self.assertEqual(r.status_code, 403)
                self.assertIn("démonstration", r.json()["detail"])
        s = SessionLocal(); self.assertEqual(s.query(Detection).count(), before); s.close()

    def test_real_mode_never_uses_simulated_cameras(self):
        self.assertFalse(config.DEMO)
        self.assertIsNone(main.app.state.demo)
        self.assertFalse(any(type(c).__name__ == "SimCamera" for c in main.app.state.cams.values()))


for _name in [n for n in dir(_api.ApiTests) if n.startswith("test_") and n not in DemoApiOutsideDemoTests.__dict__]:
    setattr(DemoApiOutsideDemoTests, _name, None)

if __name__ == "__main__":
    unittest.main()
