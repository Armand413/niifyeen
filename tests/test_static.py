import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "backend" / "app" / "static"


class StaticPagesTests(unittest.TestCase):
    def test_no_innerhtml_in_pages(self):
        """Les données (noms, ids de caméra) ne doivent jamais être injectées comme du HTML (XSS)."""
        for page in STATIC.glob("*.html"):
            with self.subTest(page=page.name):
                self.assertIsNone(re.search(r"innerHTML|insertAdjacentHTML|document\.write", page.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
