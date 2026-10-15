import json
import pathlib
import subprocess
import tempfile
import unittest

import config
import heal
import repair
import state


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True, check=True).stdout.strip()


SRC = "def add(a, b):\n    return a - b\n"
TEST = "import unittest\nimport calc\n\n\nclass CalcTests(unittest.TestCase):\n    def test_zero(self):\n        self.assertEqual(calc.add(0, 0), 0)\n"
NEW_TEST = TEST + "\n    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n"


class Fixture(unittest.TestCase):
    """A throwaway 'live' repo whose runner code lives under common/skills/agent-loop/scripts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        toml = (pathlib.Path(__file__).resolve().parent.parent / "hopper.toml").read_text()
        toml = toml.replace('state_root = "~/.local/state/agent-loop"', f'state_root = "{root}/state"')
        (root / "hopper.toml").write_text(toml)
        self.cfg = config.load(root / "hopper.toml"); self.cfg.ensure_dirs()
        self.live = root / "live"; self.scripts = self.live / "common/skills/agent-loop/scripts"; self.scripts.mkdir(parents=True)
        git(self.live, "init", "-q", "-b", "agent-loop-1"); git(self.live, "config", "user.email", "t@t"); git(self.live, "config", "user.name", "t")
        (self.scripts / "calc.py").write_text(SRC); (self.scripts / "test_calc.py").write_text(TEST)
        (self.scripts / "contracts.py").write_text("RULES = 1\n")
        git(self.live, "add", "-A"); git(self.live, "commit", "-qm", "init")
        self.base = git(self.live, "rev-parse", "HEAD")
        self.notified = []; self.agent_calls = []
        self.review_verdict = "approve"; self.edits = {"calc.py": SRC.replace("a - b", "a + b"), "test_calc.py": NEW_TEST}

    def fake_agent(self, cfg, name, prompt, stage, cwd, timeout):
        self.agent_calls.append(name); stage.mkdir(parents=True, exist_ok=True)
        if name == "repair-agent":
            for f, body in self.edits.items():
                (pathlib.Path(cwd) / f).write_text(body)
            (stage / "commit-message.txt").write_text("calc: add adds\n\nIt subtracted.\n")
            (stage / "stdout.log").write_text("REPAIR: fixed\n")
        else:
            (stage / "review.md").write_text(f"VERDICT: {self.review_verdict}\nreason\n")
            (stage / "stdout.log").write_text(f"REVIEW: {self.review_verdict}\n")

    def run_tests(self, cwd, ids):
        return subprocess.run(["python3", "-m", "unittest", *(ids or ["discover", "-p", "test_*.py"])],
                              cwd=str(cwd), capture_output=True).returncode

    def run_pass(self):
        return repair.run_pass(self.cfg, scripts_dir=self.scripts, run_agent=self.fake_agent, run_tests=self.run_tests,
                               notify=lambda title, body: self.notified.append(title), log=lambda m: None)


class RecordTests(Fixture):
    def test_a_defect_is_queued_once_per_signature(self):
        a = repair.record_defect(self.cfg, "ZIP-1", "traceback", "TypeError: x at line 12", "tb 1")
        b = repair.record_defect(self.cfg, "ZIP-1", "traceback", "TypeError: x at line 13", "tb 2")
        self.assertEqual(a, b)
        recs = repair.open_defects(self.cfg)
        self.assertEqual(len(recs), 1); self.assertEqual(recs[0]["key"], "ZIP-1"); self.assertIn("tb 1", recs[0]["evidence"])

    def test_duplicate_signature_merges_affected_tickets_once(self):
        first = {"ticket": "ZIP-1", "recovery_id": "recovery-1", "contract_fingerprint": "a"}
        second = {"ticket": "ZIP-2", "recovery_id": "recovery-2", "contract_fingerprint": "b"}
        sig = repair.record_defect(self.cfg, "ZIP-1", "heal", "same defect", "first", recovery_ref=first)
        repair.record_defect(self.cfg, "ZIP-2", "heal", "same defect", "second", recovery_ref=second)
        repair.record_defect(self.cfg, "ZIP-2", "heal", "same defect", "second", recovery_ref=second)
        record = repair.open_defects(self.cfg)[0]
        self.assertEqual(record["id"], sig)
        self.assertEqual([(row["ticket"], row["recovery_id"]) for row in record["affected_tickets"]],
                         [("ZIP-1", "recovery-1"), ("ZIP-2", "recovery-2")])
        self.assertIn("first", record["evidence"])
        self.assertIn("second", record["evidence"])

    def test_queue_reconciliation_deduplicates_refs_and_marks_landed_signature_not_needed(self):
        ref = {"ticket": "ZIP-1", "recovery_id": "recovery-1", "contract_fingerprint": "a"}
        sig = repair.record_defect(self.cfg, "ZIP-1", "heal", "same defect", "evidence", recovery_ref=ref)
        path = self.cfg.state_root / "defects" / "open" / f"{sig}.json"
        record = json.loads(path.read_text()); record["affected_tickets"].append(dict(ref)); path.write_text(json.dumps(record))
        actionable = repair.reconcile_queue(self.cfg)
        self.assertEqual(len(actionable[0]["affected_tickets"]), 1)
        (self.cfg.state_root / "defects" / "landed" / f"{sig}-old.json").write_text("{}")
        self.assertEqual(repair.reconcile_queue(self.cfg), [])
        self.assertFalse(path.exists())
        self.assertEqual(len(list((self.cfg.state_root / "defects" / "events").glob("*.json"))), 1)

    def test_heal_giving_up_on_a_recurring_failure_records_a_defect(self):
        tdir = self.cfg.ticket_dir("ZIP-1"); tdir.mkdir(parents=True, exist_ok=True)
        for _ in range(heal.MAX_SAME_SIGNATURE):
            heal.record(tdir, "repair", "guard failed on the BASE tree too: x", "bot-loop")
        repair.on_heal_verdict(self.cfg, "ZIP-1", "guard failed on the BASE tree too: x", *heal.decide(tdir, "guard failed on the BASE tree too: x"))
        self.assertEqual([r["source"] for r in repair.open_defects(self.cfg)], ["heal"])

    def test_a_resource_pause_is_not_a_runner_defect(self):
        repair.on_heal_verdict(self.cfg, "ZIP-1", "resource: load 35.49 on 10 cores > 2.0/core", "human", "the same failure has recurred 4 times (x)")
        self.assertEqual(repair.open_defects(self.cfg), [])

    def test_an_owner_question_is_not_a_runner_defect(self):
        repair.on_heal_verdict(self.cfg, "ZIP-1", "reviewer questions need Cody: x", "human", "policy: needs an owner")
        self.assertEqual(repair.open_defects(self.cfg), [])


class GateTests(unittest.TestCase):
    R = "common/skills/agent-loop/scripts"

    def check(self, changes):
        return repair.check_diff(changes, self.R)

    def test_a_tested_fix_passes(self):
        self.assertEqual(self.check({f"{self.R}/calc.py": (SRC, "x"), f"{self.R}/test_calc.py": (TEST, NEW_TEST)}), [])

    def test_violations(self):
        cases = {
            "protected": {f"{self.R}/contracts.py": ("a", "b"), f"{self.R}/test_calc.py": (TEST, NEW_TEST)},
            "outside the runner": {"common/instructions/AGENTS.md": ("a", "b"), f"{self.R}/test_calc.py": (TEST, NEW_TEST)},
            "no new test": {f"{self.R}/calc.py": (SRC, "x")},
            "removed test": {f"{self.R}/test_calc.py": (NEW_TEST, NEW_TEST.replace("def test_zero", "def helper_zero"))},
            "fewer assertions": {f"{self.R}/test_calc.py": (NEW_TEST, NEW_TEST.replace("self.assertEqual(calc.add(0, 0), 0)", "pass") + "\n    def test_new(self):\n        pass\n")},
        }
        for why, changes in cases.items():
            with self.subTest(why):
                self.assertTrue(self.check(changes), why)

    def test_new_test_ids_are_module_class_name(self):
        self.assertEqual(repair.new_test_ids(TEST, NEW_TEST, "test_calc"), ["test_calc.CalcTests.test_add"])


class PassTests(Fixture):
    def test_a_reviewed_fix_whose_test_fails_on_base_lands_on_the_live_branch(self):
        tdir = self.cfg.ticket_dir("ZIP-1"); state.save(tdir, state.Ticket(key="ZIP-1", state="paused", previous="implement", needs_operator="recurred"))
        repair.record_defect(self.cfg, "ZIP-1", "traceback", "AssertionError in calc.add", "trace")
        self.assertEqual(self.run_pass(), "landed")
        self.assertNotEqual(git(self.live, "rev-parse", "HEAD"), self.base)
        self.assertIn("a + b", (self.scripts / "calc.py").read_text())
        self.assertEqual(git(self.live, "log", "-1", "--format=%s"), "calc: add adds")
        self.assertEqual(self.agent_calls, ["repair-agent", "repair-reviewer"])
        self.assertEqual(repair.open_defects(self.cfg), [])
        self.assertEqual(heal.history(tdir)[-1]["action"], heal.OPERATOR_RESUME)       # fresh budgets for the ticket
        self.assertEqual(state.load(tdir).needs_operator, "")
        self.assertEqual(git(self.live, "worktree", "list").count("\n"), 0)              # repair worktrees cleaned up

    def test_harness_artifacts_never_enter_the_change(self):
        """Real smoke run, 2026-10-07: `git add -A` swept pi-lens probe logs (local paths, session ids),
        .pi/loops state and __pycache__ into the repair; the reviewer's own session then grew those logs
        and the gate read that as the reviewer modifying the change."""
        def noisy(cfg, name, prompt, stage, cwd, timeout):
            for junk in (".pi-lens-probe-home/cascade.log", ".pi/loops/loops-1.json", "__pycache__/calc.cpython-314.pyc"):
                f = pathlib.Path(cwd) / junk; f.parent.mkdir(parents=True, exist_ok=True)
                with open(f, "ab") as fh: fh.write(f"{name}\n".encode())
            self.fake_agent(cfg, name, prompt, stage, cwd, timeout)
        repair.record_defect(self.cfg, None, "traceback", "AssertionError in calc.add", "trace")
        out = repair.run_pass(self.cfg, scripts_dir=self.scripts, run_agent=noisy, run_tests=self.run_tests,
                              notify=lambda t, b: self.notified.append(t), log=lambda m: None)
        self.assertEqual(out, "landed")
        files = git(self.live, "show", "--name-only", "--format=", "HEAD").splitlines()
        self.assertEqual(sorted(files), [f"common/skills/agent-loop/scripts/{n}" for n in ("calc.py", "test_calc.py")])

    def test_a_rejected_fix_enters_diagnostic_retry_instead_of_being_abandoned(self):
        repair.record_defect(self.cfg, None, "traceback", "AssertionError in calc.add", "trace")
        self.review_verdict = "reject"
        self.assertEqual(self.run_pass(), "rejected")
        self.assertEqual(git(self.live, "rev-parse", "HEAD"), self.base); self.assertEqual(self.notified, [])
        self.assertEqual(self.run_pass(), "diagnostic-retry")
        record = repair.open_defects(self.cfg)[0]
        self.assertEqual(record["attempts"], 0)
        self.assertTrue(any("diagnostic" in row for row in record["history"]))
        self.assertEqual(len(list((self.cfg.state_root / "defects" / "events").glob("*.json"))), 1)

    def test_reviewer_auth_failure_does_not_spend_repair_budget(self):
        repair.record_defect(self.cfg, None, "traceback", "AssertionError in calc.add", "trace")

        def agent(cfg, name, prompt, stage, cwd, timeout):
            if name == "repair-reviewer":
                stage.mkdir(parents=True, exist_ok=True)
                (stage / "stderr.log").write_text("OAuth refresh failed: invalid_grant")
                return
            self.fake_agent(cfg, name, prompt, stage, cwd, timeout)

        outcome = repair.run_pass(self.cfg, scripts_dir=self.scripts, run_agent=agent, run_tests=self.run_tests,
                                  notify=lambda title, body: self.notified.append(title), log=lambda m: None)
        self.assertEqual(outcome, "skipped")
        self.assertEqual(repair.open_defects(self.cfg)[0]["attempts"], 0)

    def test_the_second_attempt_is_told_why_the_first_was_rejected(self):
        # Real smoke run 2: the fix was right but the agent also reordered imports; the reviewer rejected it
        # and attempt 2 started blind, free to make the same unrelated change again.
        prompts = []
        def agent(cfg, name, prompt, stage, cwd, timeout):
            if name == "repair-agent": prompts.append(prompt)
            self.fake_agent(cfg, name, prompt, stage, cwd, timeout)
        repair.record_defect(self.cfg, None, "traceback", "AssertionError in calc.add", "trace")
        self.review_verdict = "reject"
        run = lambda: repair.run_pass(self.cfg, scripts_dir=self.scripts, run_agent=agent, run_tests=self.run_tests,
                                      notify=lambda t, b: None, log=lambda m: None)
        run(); run()
        self.assertEqual(len(prompts), 2)
        self.assertNotIn("Earlier attempts", prompts[0])
        self.assertIn("Earlier attempts", prompts[1]); self.assertIn("reviewer: reject", prompts[1])

    def test_a_test_that_passes_on_base_proves_nothing(self):
        repair.record_defect(self.cfg, None, "traceback", "x", "trace")
        self.edits = {"calc.py": SRC.replace("a - b", "a + b"), "test_calc.py": TEST + "\n    def test_one(self):\n        self.assertEqual(calc.add(1, 0), 1)\n"}
        self.assertEqual(self.run_pass(), "rejected")
        self.assertEqual(self.agent_calls, ["repair-agent"])                              # never reached review

    def test_a_protected_file_change_waits_for_cody(self):
        repair.record_defect(self.cfg, None, "traceback", "x", "trace")
        self.edits = {"contracts.py": "RULES = 0\n", "calc.py": SRC.replace("a - b", "a + b"), "test_calc.py": NEW_TEST}
        self.assertEqual(self.run_pass(), "needs-approval")
        self.assertEqual(git(self.live, "rev-parse", "HEAD"), self.base); self.assertEqual(len(self.notified), 1)
        self.assertTrue(list((self.cfg.state_root / "defects" / "needs-approval").glob("*.patch")))

    def test_a_dirty_or_moved_live_checkout_skips_without_spending_an_attempt(self):
        repair.record_defect(self.cfg, None, "traceback", "x", "trace")
        (self.scripts / "calc.py").write_text("dirty\n")
        self.assertEqual(self.run_pass(), "skipped")
        self.assertEqual(repair.open_defects(self.cfg)[0]["attempts"], 0); self.assertEqual(self.agent_calls, [])

    def test_a_commit_landing_during_the_repair_is_a_retry_not_a_failure(self):
        # 2026-10-08: operator commits moved the live branch during two repairs; both counted as failed
        # attempts, the defect was abandoned and Cody was notified.
        repair.record_defect(self.cfg, None, "traceback", "AssertionError in calc.add", "trace")
        def agent(cfg, name, prompt, stage, cwd, timeout):
            self.fake_agent(cfg, name, prompt, stage, cwd, timeout)
            if name == "repair-agent":
                (self.scripts / "other.py").write_text("x = 1\n"); git(self.live, "add", "-A"); git(self.live, "commit", "-qm", "unrelated")
        out = repair.run_pass(self.cfg, scripts_dir=self.scripts, run_agent=agent, run_tests=self.run_tests,
                              notify=lambda t, b: self.notified.append(t), log=lambda m: None)
        self.assertEqual(out, "skipped")
        self.assertEqual(repair.open_defects(self.cfg)[0]["attempts"], 0); self.assertEqual(self.notified, [])

    def test_pause_file_stops_repair(self):
        repair.record_defect(self.cfg, None, "traceback", "x", "trace")
        (self.cfg.state_root / "PAUSE").touch()
        self.assertEqual(self.run_pass(), "paused"); self.assertEqual(self.agent_calls, [])


class HookTests(unittest.TestCase):
    def test_runner_records_a_crash_before_reraising(self):
        import inspect, runner
        src = inspect.getsource(runner.main)
        self.assertTrue("repair.record_crash(" in src, "runner.main does not record crashes")

    def test_lifecycle_reports_heal_verdicts_to_repair(self):
        import inspect, lifecycle
        self.assertTrue("repair.on_heal_verdict(" in inspect.getsource(lifecycle.step), "lifecycle.step does not report heal verdicts")


if __name__ == "__main__":
    unittest.main()
