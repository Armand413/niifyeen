import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "backend" / "app" / "static"


class StaticPagesTests(unittest.TestCase):
    def test_no_innerhtml_in_pages(self):
        """Les données (noms, ids de caméra) ne doivent jamais être injectées comme du HTML (XSS)."""
        for page in [*STATIC.glob("*.html"), *STATIC.glob("*.js")]:
            with self.subTest(page=page.name):
                self.assertIsNone(re.search(r"innerHTML|insertAdjacentHTML|document\.write", page.read_text(encoding="utf-8")))


    def test_target_panel_is_wired_and_calls_existing_routes(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        js = (STATIC / "targets-panel.js").read_text(encoding="utf-8")
        for needle in ('src="/static/targets-panel.js"', "TargetsPanel.init", 'id="tpPick"', 'id="tpMax"',
                       'id="tpSave"', 'id="tpResume"'):
            with self.subTest(needle=needle):
                self.assertIn(needle, html)
        for route in ("/api/targets/from-person", "/api/targets/settings", "/api/targets/session",
                      "/api/targets/session/save", "/api/targets/session/resume"):
            with self.subTest(route=route):
                self.assertIn(route, js)

    def test_history_page_reads_the_person_filter(self):
        js = (STATIC / "history.js").read_text(encoding="utf-8")
        html = (STATIC / "history.html").read_text(encoding="utf-8")
        self.assertIn("participant_id", js)
        self.assertIn('id="personFilter"', html)
        view = (STATIC / "targets-view.js").read_text(encoding="utf-8")
        self.assertIn("/history?participant_id=", view)

    def test_map_page_is_wired_and_linked(self):
        html = (STATIC / "map.html").read_text(encoding="utf-8")
        for needle in ('src="/static/shell.js"', 'src="/static/plan.js"', 'src="/static/map-logic.js"',
                       'src="/static/map.js"', 'id="mapSvg"', 'id="floorTabs"', 'id="rSlider"', 'id="aCams"'):
            with self.subTest(needle=needle):
                self.assertIn(needle, html)
        js = (STATIC / "map.js").read_text(encoding="utf-8")
        for route in ("/api/floors", "/api/admin/cameras/", "/api/history/trajectories", "/api/supervision/cameras", "/api/cameras"):
            with self.subTest(route=route):
                self.assertIn(route, js)
        self.assertIn('href: "/map"', (STATIC / "shell.js").read_text(encoding="utf-8"))





    def test_alerts_page_is_wired_and_linked(self):
        html = (STATIC / "alerts.html").read_text(encoding="utf-8")
        for needle in ('src="/static/shell.js"', 'src="/static/alerts-logic.js"', 'src="/static/alerts.js"',
                       'id="rows"', 'id="statusSeg"', 'id="rulesBody"', 'id="dHistory"', 'id="btnSound"', 'id="fQ"'):
            with self.subTest(needle=needle):
                self.assertIn(needle, html)
        js = (STATIC / "alerts.js").read_text(encoding="utf-8")
        for route in ("/api/alerts", "/api/alerts/rules", "/api/alerts/channels", "/api/alerts/bulk", "/status", "/note"):
            with self.subTest(route=route):
                self.assertIn(route, js)
        shell = (STATIC / "shell.js").read_text(encoding="utf-8")
        self.assertIn('href: "/alerts"', shell)
        self.assertIn("/api/alerts/summary", shell)


    def test_plan_is_wired_into_the_cameras_page(self):
        html = (STATIC / "cameras.html").read_text(encoding="utf-8")
        for needle in ('src="/static/plan.js"', 'id="planOverlay"', 'id="planStatus"', "SitePlan.render"):
            with self.subTest(needle=needle):
                self.assertIn(needle, html)

    def test_target_list_component_is_wired_into_both_pages(self):
        for page in ("index.html", "cameras.html"):
            html = (STATIC / page).read_text(encoding="utf-8")
            for needle in ('src="/static/targets-view.js"', 'src="/static/profile.js"', 'id="targetsBox"', "TargetsView.render"):
                with self.subTest(page=page, needle=needle):
                    self.assertIn(needle, html)

    def test_pages_no_longer_use_the_single_target_api(self):
        for page in ("index.html", "cameras.html", "history.js"):
            text = (STATIC / page).read_text(encoding="utf-8")
            for old in ("/target/profile", "target_session", "target_path", "data.target\\b"):
                with self.subTest(page=page, old=old):
                    self.assertIsNone(re.search(old, text))

    def test_supervision_page_is_wired(self):
        html = (STATIC / "supervision.html").read_text(encoding="utf-8")
        for needle in ('href="/static/app.css"', 'src="/static/shell.js"', 'src="/static/supervision.js"', 'id="sidebar"'):
            with self.subTest(needle=needle):
                self.assertIn(needle, html)
        self.assertIn('"/supervision"', (STATIC / "shell.js").read_text(encoding="utf-8"))

    def test_every_element_the_supervision_script_looks_up_exists(self):
        """Un identifiant renommé d'un côté seulement casserait la page sans qu'aucune erreur ne soit visible."""
        html = (STATIC / "supervision.html").read_text(encoding="utf-8")
        js = (STATIC / "supervision.js").read_text(encoding="utf-8")
        ids_in_html = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
        used = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', js))
        used |= {prefix + name for prefix in ("tab", "pane") for name in ("Events", "Camera", "Targets")}
        created_by_the_shell = {"linkDot", "linkText", "themeBtn", "toasts"}
        self.assertEqual(sorted(used - ids_in_html - created_by_the_shell), [])

    def test_every_element_the_camera_admin_script_looks_up_exists(self):
        html = (STATIC / "cameras-admin.html").read_text(encoding="utf-8")
        js = (STATIC / "cameras-admin.js").read_text(encoding="utf-8")
        ids_in_html = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
        used = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', js))
        created_by_the_shell = {"linkDot", "linkText", "themeBtn", "toasts"}
        self.assertEqual(sorted(used - ids_in_html - created_by_the_shell), [])

    def test_the_camera_form_fields_have_matching_server_fields(self):
        """Les champs « data-f » du formulaire doivent exister côté serveur (sinon l'erreur ne s'affiche nulle part)."""
        html = (STATIC / "cameras-admin.html").read_text(encoding="utf-8")
        fields = set(re.findall(r'data-f="([a-z_]+)"', html))
        from backend.app.main import CameraIn
        self.assertEqual(sorted(fields - set(CameraIn.model_fields)), [])

    def test_camera_admin_page_is_wired_and_linked(self):
        html = (STATIC / "cameras-admin.html").read_text(encoding="utf-8")
        for needle in ('src="/static/shell.js"', 'src="/static/cameras-admin.js"', 'href="/static/app.css"', 'id="sidebar"'):
            with self.subTest(needle=needle):
                self.assertIn(needle, html)
        self.assertIn('"/admin/cameras"', (STATIC / "shell.js").read_text(encoding="utf-8"))
        js = (STATIC / "cameras-admin.js").read_text(encoding="utf-8")
        for route in ("/api/admin/cameras", "/api/admin/cameras/test", "/api/admin/cameras/webcams",
                      "/api/admin/cameras/discover", "/ws/live"):
            with self.subTest(route=route):
                self.assertIn(route, js)

    def test_every_element_the_people_script_looks_up_exists(self):
        html = (STATIC / "people.html").read_text(encoding="utf-8")
        js = (STATIC / "people.js").read_text(encoding="utf-8")
        ids_in_html = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
        used = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', js))
        created_by_the_shell = {"linkDot", "linkText", "themeBtn", "toasts"}
        self.assertEqual(sorted(used - ids_in_html - created_by_the_shell), [])

    def test_people_form_fields_match_the_server(self):
        """Les champs « data-f » du formulaire doivent exister côté serveur (sinon l'erreur ne s'affiche nulle part)."""
        html = (STATIC / "people.html").read_text(encoding="utf-8")
        form_fields = set(re.findall(r'data-f="([a-z_]+)"', html)) - {"photos", "consentement"}
        from backend.app.main import PersonIn
        self.assertEqual(sorted(form_fields - set(PersonIn.model_fields)), [])

    def test_people_page_is_wired_and_linked(self):
        html = (STATIC / "people.html").read_text(encoding="utf-8")
        for needle in ('src="/static/shell.js"', 'src="/static/people.js"', 'href="/static/app.css"', 'id="sidebar"'):
            with self.subTest(needle=needle):
                self.assertIn(needle, html)
        self.assertIn('"/people"', (STATIC / "shell.js").read_text(encoding="utf-8"))
        js = (STATIC / "people.js").read_text(encoding="utf-8")
        for route in ("/api/people", "/participants/enroll", "/api/cameras"):
            with self.subTest(route=route):
                self.assertIn(route, js)

    def test_supervision_links_to_the_people_card(self):
        js = (STATIC / "supervision.js").read_text(encoding="utf-8")
        self.assertIn('"/people/"', js)
        self.assertIn("participant_id", js)

    def test_supervision_uses_only_endpoints_that_exist(self):
        js = (STATIC / "supervision.js").read_text(encoding="utf-8")
        for route in ("/api/supervision/cameras", "/api/events", "/ws/video", "/ws/live"):
            with self.subTest(route=route):
                self.assertIn(route, js)

    def test_history_page_is_wired(self):
        html = (STATIC / "history.html").read_text(encoding="utf-8")
        for needle in ('src="/static/history.js"', 'src="/static/plan.js"', 'id="export"', 'id="planOverlay"'):
            with self.subTest(needle=needle):
                self.assertIn(needle, html)
        self.assertIn('href: "/history"', (STATIC / "shell.js").read_text(encoding="utf-8"))      # navigation : barre latérale

    def test_legacy_pages_use_the_design_system(self):
        """Module 13 : les quatre anciennes pages passent par la coque commune (menu, thème) et plus par layout.css."""
        shell = (STATIC / "shell.js").read_text(encoding="utf-8")
        for page, key in (("index.html", "home"), ("cameras.html", "cameras"), ("history.html", "history")):
            html = (STATIC / page).read_text(encoding="utf-8")
            with self.subTest(page=page):
                for needle in ('href="/static/app.css"', 'href="/static/legacy.css"', 'src="/static/shell.js"', 'id="sidebar"',
                               'class="page-body legacy', f'Shell.mount({{ active: "{key}" }})', 'data-theme="dark"', 'niifyeen.theme'):
                    self.assertIn(needle, html)
                self.assertNotIn("layout.css", html)
                self.assertNotIn("<style", html)                     # plus de style en ligne : tout passe par les variables du thème
                self.assertIn(f'key: "{key}"', shell)

    def test_legacy_stylesheet_is_scoped_and_themed(self):
        css = (STATIC / "legacy.css").read_text(encoding="utf-8")
        rules = [r.strip() for r in css.replace("}", "}\n").splitlines() if "{" in r and not r.strip().startswith(("/*", "@", "from", "to"))]
        for rule in rules:
            selector = rule.split("{")[0]
            with self.subTest(selector=selector[:60]):
                self.assertTrue(all(part.strip().startswith(".legacy") for part in selector.split(",")), selector)
        for token in ("var(--surface)", "var(--border)", "var(--text)", "var(--accent)"):
            self.assertIn(token, css)

    def test_building_plan_is_gone(self):
        """Le plan du bâtiment est supprimé : aucune page ni script ne doit plus le référencer."""
        self.assertFalse((STATIC / "site-plan.svg").exists())
        for p in STATIC.iterdir():
            if p.suffix in (".html", ".js", ".css"):
                self.assertNotIn("site-plan.svg", p.read_text(encoding="utf-8"), p.name)


if __name__ == "__main__":
    unittest.main()
