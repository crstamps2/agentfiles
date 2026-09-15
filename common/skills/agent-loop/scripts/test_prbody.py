import unittest

import prbody

GOOD = """🎁 Summary
---

Adds `ZUI::Well`, a recessed content surface with header, body and footer slots.

☎️ Related links and discussions
---

- [ZIP-7873](https://zipline.atlassian.net/browse/ZIP-7873)

✋ Deployment Dependencies
---

None.

🗺 Where should a review start?
---

1. `app/views/components/zui/well/well.rb`

📝 Documentation
---

Lookbook preview under ZUI > Well.

✅ Quality Assurances
---

- [x] Component suite: 14 tests, 31 assertions

<details>
<summary><h2>🤖 AI Conversation Summary</h2></summary>

### Intent
Ship the Well container.

</details>
"""


class LintTests(unittest.TestCase):
    def test_house_style_body_passes(self):
        self.assertEqual(prbody.lint(GOOD), [])

    def test_workflow_vocabulary_is_rejected(self):
        for bad in ("Implemented by the agent loop.", "the runner's verification gate", "a cheap worker", "adjudicated by Fable",
                    "Terra took attempt 3", "see planning/zip-7873/plan.md", "Human Gate 1", "— posted on his behalf", "tier-3 decision"):
            with self.subTest(bad=bad):
                self.assertTrue(any("forbidden" in p for p in prbody.lint(GOOD + "\n" + bad)), bad)

    def test_missing_section_and_wrong_jira_host_rejected(self):
        self.assertIn("missing section '✋ Deployment Dependencies'", prbody.lint(GOOD.replace("✋ Deployment Dependencies", "Deps")))
        self.assertIn("wrong Jira host", prbody.lint(GOOD.replace("zipline.atlassian.net", "retailzipline.atlassian.net") + "\nhttps://zipline.atlassian.net/browse/X"))

    def test_fallback_is_clean(self):
        b = prbody.fallback_body("ZIP-1", "ZUI Well", ["app/a.rb"], None)
        self.assertEqual(prbody.lint(b), [])
        self.assertNotIn("Screenshots", b)


if __name__ == "__main__":
    unittest.main()
