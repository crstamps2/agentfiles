import pathlib, tempfile, unittest
import contracts

GOOD = """
# Result
STATUS: pass
REASON: none
BASE: abc123
FILES: app/a.rb, app/b.rb
EVIDENCE: ran bin/rails test test/a_test.rb → 3 runs, 0 failures
UNVERIFIED: none
NEXT: none
"""
SLOPPY = """
## Reslt (typo heading)
* status = Fail
- reason: test
files: app/a.rb
some chatter the model added
Next: fix the assertion in a_test.rb
"""
NO_STATUS = "REASON: none\nFILES: x\n"

class ParseResultTests(unittest.TestCase):
    def test_parses_well_formed(self):
        r = contracts.parse_result(GOOD)
        self.assertEqual((r.status, r.reason, r.base), ("pass", "none", "abc123"))
        self.assertEqual(r.files, ["app/a.rb", "app/b.rb"])
        self.assertIn("0 failures", r.evidence)
    def test_lenient_on_format(self):
        r = contracts.parse_result(SLOPPY)
        self.assertEqual(r.status, "fail"); self.assertEqual(r.reason, "test")
        self.assertEqual(r.files, ["app/a.rb"]); self.assertIn("assertion", r.next)
    def test_missing_status_is_protocol_error(self):
        with self.assertRaises(contracts.ProtocolError):
            contracts.parse_result(NO_STATUS)
    def test_bad_status_value_is_protocol_error(self):
        with self.assertRaises(contracts.ProtocolError):
            contracts.parse_result("STATUS: maybe\n")
    def test_missing_optional_fields_default_empty(self):
        r = contracts.parse_result("STATUS: blocked\nREASON: owner\n")
        self.assertEqual(r.files, []); self.assertEqual(r.base, "")
    def test_unknown_reason_normalizes_to_empty(self):
        r = contracts.parse_result("STATUS: fail\nREASON: bogus\n")
        self.assertEqual(r.reason, "")

TASKS = """
[[tasks]]
id = "001"
slug = "well-component-skeleton"
summary = "Add ZUI::Well component class with typed slots"
allowed_files = ["app/views/components/zui/well/**"]
verification_commands = ["bin/rails test test/components/zui/well_test.rb"]
acceptance = ["AC-1: renders header/body/footer slots"]
may_edit_tests = true
visual = false
[[tasks]]
id = "002"
slug = "well-scss-sidecar"
summary = "Move Cable 2 .well styles into the sidecar"
allowed_files = ["app/views/components/zui/well/well.scss", "app/assets/stylesheets/cable_2/manifest.scss"]
verification_commands = ["bin/lint-scss app/views/components/zui/well"]
acceptance = ["AC-2: legacy source removed, tombstone present"]
visual = true
figma_nodes = ["9281-1211"]
"""

class ManifestFieldValidationTests(unittest.TestCase):
    def test_manifest_rejects_each_bad_field(self):
        good = {"id": "001", "slug": "s", "summary": "s", "allowed_files": ["a/**"],
                "verification_commands": ["true"], "acceptance": ["a"]}
        for k, v in [("allowed_files", [7]), ("verification_commands", [""]), ("acceptance", [None]), ("timeout_s", True),
                     ("figma_nodes", [1]), ("allowed_files", ["/abs"]), ("allowed_files", ["../x"]), ("invariants", "notalist")]:
            with self.subTest(field=k, value=v):
                errs = contracts.validate_tasks({"tasks": [{**good, k: v}]})
                self.assertTrue(any(k in e for e in errs), errs)
        self.assertTrue(contracts.validate_tasks({"tasks": "notalist"}))


class TasksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.p = pathlib.Path(self.tmp.name) / "tasks.toml"
    def tearDown(self):
        self.tmp.cleanup()
    def test_loads_valid_manifest_with_defaults(self):
        self.p.write_text(TASKS)
        ts = contracts.load_tasks(self.p)
        self.assertEqual([t.id for t in ts], ["001", "002"])
        self.assertTrue(ts[0].may_edit_tests); self.assertFalse(ts[1].may_edit_tests)
        self.assertEqual(ts[1].timeout_s, 4500); self.assertEqual(ts[1].figma_nodes, ["9281-1211"])
    def test_validation_errors_are_specific(self):
        bad = {"tasks": [
            {"id": "1", "slug": "x", "summary": "s", "allowed_files": [], "verification_commands": ["c"], "acceptance": ["a"]},
            {"id": "001", "slug": "y", "summary": "s", "allowed_files": ["f"], "verification_commands": [], "acceptance": ["a"]},
            {"id": "001", "slug": "z", "summary": "s", "allowed_files": ["f"], "verification_commands": ["c"]},
        ]}
        errs = contracts.validate_tasks(bad)
        joined = "\n".join(errs)
        self.assertIn("tasks[0].id", joined)              # bad format
        self.assertIn("tasks[0].allowed_files", joined)   # empty
        self.assertIn("tasks[1].verification_commands", joined)
        self.assertIn("tasks[2].acceptance", joined)      # missing
        self.assertIn("duplicate id 001", joined)
    def test_empty_manifest_is_invalid(self):
        self.assertTrue(contracts.validate_tasks({}))
        self.assertTrue(contracts.validate_tasks({"tasks": []}))
    def test_load_raises_on_invalid(self):
        self.p.write_text("[[tasks]]\nid='001'\n")
        with self.assertRaises(contracts.ManifestError):
            contracts.load_tasks(self.p)
    def test_unknown_key_is_validation_error(self):
        bad_toml = """
[[tasks]]
id = "001"
slug = "test"
summary = "test task"
allowed_files = ["f"]
verification_commands = ["c"]
acceptance = ["a"]
figma_node = ["x"]
"""
        self.p.write_text(bad_toml)
        with self.assertRaises(contracts.ManifestError) as cm:
            contracts.load_tasks(self.p)
        self.assertIn("unknown key 'figma_node'", str(cm.exception))

if __name__ == "__main__":
    unittest.main()


class SuffixIdTests(unittest.TestCase):
    def test_letter_suffix_ids_order_between_neighbours(self):
        mk = lambda i: {"id": i, "slug": "s", "summary": "x", "allowed_files": ["a"], "verification_commands": ["true"], "acceptance": ["y"]}
        self.assertEqual(contracts.validate_tasks({"tasks": [mk("006"), mk("006a"), mk("007")]}), [])
        self.assertTrue(any("ascending" in e for e in contracts.validate_tasks({"tasks": [mk("007"), mk("006a")]})))


class VerificationShapeTests(unittest.TestCase):
    def mk(self, cmds):
        return {"tasks": [{"id": "001", "slug": "s", "summary": "x", "allowed_files": ["a"], "verification_commands": cmds, "acceptance": ["y"]}]}

    def test_planning_probe_references_are_rejected(self):
        errs = contracts.validate_tasks(self.mk(["bin/agent_run ruby planning/zip-7872/qa/contract_probe.rb"]))
        self.assertTrue(any("references planning/" in e for e in errs))
        errs = contracts.validate_tasks(self.mk(["bash planning/x/qa/with_chrome.sh bin/rails test t.rb"]))
        self.assertTrue(any("references planning/" in e for e in errs))

    def test_bespoke_command_budget(self):
        std = ["bin/agent_run bin/wt prepare --for rails", "bin/agent_run rails test test/a_test.rb", "bin/agent_run rubocop --cache false a.rb", "git diff --check"]
        ok = std + ["bin/rails runner 'abort unless X'", "ruby -e 'exit 1 unless File.read(\"a\").include?(\"b\")'", "grep -q foo a.rb"]
        self.assertEqual(contracts.validate_tasks(self.mk(ok)), [])
        errs = contracts.validate_tasks(self.mk(ok + ["bin/rails runner 'one more'"]))
        self.assertTrue(any("bespoke commands" in e for e in errs))

