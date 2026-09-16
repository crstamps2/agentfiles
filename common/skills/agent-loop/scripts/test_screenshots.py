import unittest
from unittest.mock import patch

import screenshots

NAV = """
<a href="/lookbook/inspect/zui/card/variants">v</a><a href="/lookbook/inspect/zui/card/playground">p</a>
<a href="/lookbook/inspect/zui/nav/nav_link/states">s</a><a href="/lookbook/inspect/zui/nav/nav_link/variants">v</a>
<a href="/lookbook/inspect/zui/well/playground">p</a>
"""


class PreviewPathTests(unittest.TestCase):
    def test_namespaced_preview_is_found_from_the_nav(self):
        with patch.object(screenshots, "_get", return_value=(200, NAV)):
            self.assertEqual(screenshots.preview_path("https://x", "nav_link"), "zui/nav/nav_link")
            self.assertEqual(screenshots.preview_path("https://x", "well"), "zui/well")
            self.assertEqual(screenshots.scenarios("https://x", "nav_link"), ["states", "variants"])

    def test_missing_preview_names_what_exists(self):
        with patch.object(screenshots, "_get", return_value=(200, NAV)), self.assertRaises(screenshots.VisualGateError) as cm:
            screenshots.preview_path("https://x", "stepper")
        self.assertIn("zui/card", str(cm.exception))

    def test_table_lists_every_scenario_and_marks_missing_upload(self):
        import pathlib
        shots = {"states": pathlib.Path("/tmp/a.png"), "variants": pathlib.Path("/tmp/b.png")}
        md = screenshots.screenshots_table(shots, {pathlib.Path("/tmp/a.png"): "![a](https://u/a)"})
        self.assertIn("|`states`|![a](https://u/a)|", md); self.assertIn("gh-image upload unavailable", md)


if __name__ == "__main__":
    unittest.main()
