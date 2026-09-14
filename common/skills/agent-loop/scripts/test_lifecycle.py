import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import botreview
import ci
import config
import lifecycle
import state

MANIFEST = '''[[tasks]]
id = "001"
slug = "a"
summary = "A"
allowed_files = ["app/a.rb"]
verification_commands = ["true"]
acceptance = ["AC-1"]

[[tasks]]
id = "002"
slug = "b"
summary = "B"
allowed_files = ["app/b.rb"]
verification_commands = ["true"]
acceptance = ["AC-1"]
'''


class FakeRunner:
    def __init__(self, cfg, outcomes):
        self.cfg, self.outcomes, self.calls = cfg, list(outcomes), []

    def implement_task(self, ctx, t, task, idx, wt):
        self.calls.append(task.id)
        out = self.outcomes.pop(0)
        tdir = self.cfg.ticket_dir(t.key); tt = state.load(tdir)
        n = len(tt.attempts.get(f"{t.key}/{task.id}", [])) + 1
        tt.attempts.setdefault(f"{t.key}/{task.id}", []).append({"n": n, "outcome": out, "rung": {"agent": "x", "tier": "cheap", "n": 1}})
        state.save(tdir, tt)
        d = self.cfg.state_root / "attempts" / t.key / task.id / str(n); d.mkdir(parents=True, exist_ok=True)
        (d / "task.toml").write_text(f'id = "{task.id}"\nslug = "{task.slug}"\n')
        return out


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = pathlib.Path(self.tmp.name)
        toml = (pathlib.Path(__file__).resolve().parent.parent / "hopper.toml").read_text()
        toml = toml.replace('state_root = "~/.local/state/agent-loop"', f'state_root = "{root}/state"')
        toml = toml.replace('pi_agents_dir = "~/.pi/agent/agents"', f'pi_agents_dir = "{root}"')
        (root / "hopper.toml").write_text(toml)
        self.cfg = config.load(root / "hopper.toml"); self.cfg.ensure_dirs()
        self.wt = root / "wt"; (self.wt / "planning" / "zip-7873").mkdir(parents=True)
        (self.wt / "planning" / "zip-7873" / "tasks.toml").write_text(MANIFEST)
        (self.wt / "planning" / "zip-7873" / "plan-review.md").write_text("VERDICT: approve\n")
        t = state.Ticket(key="ZIP-7873", state="implement", previous="plan-review"); state.save(self.cfg.ticket_dir("ZIP-7873"), t)
        self.patches = [
            patch.object(lifecycle, "_sh", side_effect=self.fake_sh),
            patch.object(lifecycle, "_head", return_value="h" * 40),
            patch("runner.publish_accepted", lambda *a, **k: None),
            patch.object(lifecycle.publish, "guard_branch", return_value="internal/zip-7873-x"),
            patch.object(lifecycle.publish, "push", return_value=True),
            patch.object(lifecycle.publish, "existing_pr", return_value={"number": 47888, "isDraft": True}),
            patch.object(lifecycle.ci, "behind_base", return_value=0),
            patch.object(lifecycle.ci, "fetch_checks", side_effect=lambda *a: self.checks),
            patch.object(lifecycle.botreview, "mark_ready", side_effect=lambda pr, wt: self.marked.append(pr)),
            patch.object(lifecycle.botreview, "fetch_bot_comments", side_effect=lambda *a: self.comments),
            patch.object(lifecycle.botreview, "reply", side_effect=lambda pr, c, text, wt: self.replies.append((c["id"], text))),
            patch.object(lifecycle.plan_mod, "_run_agent", side_effect=self.fake_adjudicator),
            patch.object(lifecycle.screenshots, "ensure_dev_server", return_value="https://admin.wt.test"),
            patch.object(lifecycle.screenshots, "capture", side_effect=lambda base, comp, out: {"default": out / "x.png"}),
            patch.object(lifecycle.screenshots, "upload", return_value={}),
            patch.object(lifecycle, "_component_name", return_value="well"),
            patch.object(lifecycle.publish, "current_branch", return_value="internal/zip-7873-x"),
            patch.object(lifecycle.publish, "github_write", side_effect=lambda v, a, c: self.gh_writes.append((v, a))),
            patch.object(lifecycle.publish, "record_pr_on_worktree"),
        ]
        for p in self.patches: p.start()
        self.gh_writes = []; self.checks = [ci.Check("zipline", "pass")]; self.comments = []; self.marked = []; self.replies = []; self.adjudication = None

    def tearDown(self):
        for p in self.patches: p.stop()
        self.tmp.cleanup()

    def fake_sh(self, cmd, cwd, timeout):
        import subprocess
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def fake_adjudicator(self, cfg, agent, prompt, stage, wt, timeout, model=None):
        (stage / "adjudication.json").write_text(json.dumps(self.adjudication or {"decisions": []}))

    def run_once(self, outcomes=("accepted", "accepted"), max_steps=12):
        r = FakeRunner(self.cfg, outcomes)
        steps = lifecycle.run_once(self.cfg, r, None, "ZIP-7873", self.wt, hopper_index=2, max_steps=max_steps)
        return r, steps, state.load(self.cfg.ticket_dir("ZIP-7873"))

    def test_happy_path_reaches_human_gate_1(self):
        self.comments = [{"kind": "issue", "id": 1, "path": None, "line": None, "reply_to": None, "sha": None, "body": "LGTM summary", "body_hash": "h1"}]
        self.adjudication = {"decisions": [{"id": 1, "decision": "decline", "reply": "No change needed; summary only."}]}
        r, steps, t = self.run_once()
        self.assertEqual(r.calls, ["001", "002"])
        self.assertEqual(self.marked, [47888])
        self.assertEqual([s.stage for s in steps], ["implement", "implement", "implement", "gates", "draft-pr", "ready", "bot-loop", "bot-loop"])
        self.assertEqual(t.state, "human-gate-1"); self.assertEqual(len(self.replies), 1)
        self.assertEqual([v for v, a in self.gh_writes], ["pr-edit-body"])      # existing PR gets the plan-derived title/body
        self.assertTrue(self.replies[0][1].endswith("on his behalf"))

    def test_rejected_task_waits_and_does_not_advance(self):
        r, steps, t = self.run_once(outcomes=("rejected",))
        self.assertEqual(t.state, "implement"); self.assertTrue(steps[-1].wait); self.assertEqual(r.calls, ["001"])

    def test_ci_pending_waits_in_ready(self):
        self.checks = [ci.Check("zipline", "pending")]
        r, steps, t = self.run_once()
        self.assertEqual(t.state, "ready"); self.assertEqual(steps[-1].action, "CI running"); self.assertEqual(self.marked, [])

    def test_ci_code_failure_queues_fix_task_and_returns_to_implement(self):
        self.checks = [ci.Check("rubocop", "fail", link="https://github.com/x/y/actions/runs/1")]
        with patch.object(lifecycle.ci, "gha_failure_excerpt", return_value="Style/Foo: offense"):
            r, steps, t = self.run_once(outcomes=("accepted", "accepted", "rejected"))
        self.assertEqual(r.calls[-1], "801"); self.assertEqual(t.state, "implement")
        manifest = (self.wt / "planning" / "zip-7873" / "tasks.toml").read_text()
        self.assertIn('id = "801"', manifest); self.assertIn("ci-fix-1", manifest)
        self.assertTrue((self.cfg.state_root / "attempts" / "ZIP-7873" / "801" / "feedback.md").exists())

    def test_ci_infra_failure_reruns_and_waits(self):
        self.checks = [ci.Check("zipline", "fail", link="https://github.com/x/y/actions/runs/9")]
        with patch.object(lifecycle.ci, "gha_failure_excerpt", return_value="ECONNRESET"), patch.object(lifecycle.ci, "rerun_gha", return_value=True) as rr:
            r, steps, t = self.run_once()
        self.assertEqual(t.state, "ready"); self.assertEqual(steps[-1].action, "CI rerun requested"); rr.assert_called_once()

    def test_tier3_flag_blocks_mark_ready(self):
        p = self.wt / "planning" / "zip-7873" / "tasks.toml"; p.write_text("human_confirm_before_ready = true\n" + MANIFEST)
        r, steps, t = self.run_once()
        self.assertEqual(t.state, "paused"); self.assertIn("confirmation", t.reason); self.assertEqual(self.marked, [])

    def test_review_fix_decision_queues_task_and_replies(self):
        self.comments = [{"kind": "inline", "id": 5, "path": "app/a.rb", "line": 3, "reply_to": None, "sha": "s", "body": "nil guard missing", "body_hash": "h5"}]
        self.adjudication = {"decisions": [{"id": 5, "decision": "fix", "reply": "Adding the guard; see follow-up commit.",
                                            "task": {"allowed_files": ["app/a.rb"], "verification_commands": ["true"], "summary": "Add nil guard"}}]}
        r, steps, t = self.run_once(outcomes=("accepted", "accepted", "rejected"))
        self.assertEqual(r.calls, ["001", "002", "901"])          # the review fix task was dispatched to the ladder
        self.assertEqual(t.state, "implement"); self.assertIn("review-fix", (self.wt / "planning" / "zip-7873" / "tasks.toml").read_text())
        self.assertEqual(self.replies[0][0], 5)
        ledger = json.loads((self.cfg.ticket_dir("ZIP-7873") / "bot-ledger.json").read_text()); self.assertEqual(ledger["5"], "h5")

    def test_reviewer_question_pauses_for_cody(self):
        self.comments = [{"kind": "inline", "id": 6, "path": "app/a.rb", "line": 3, "reply_to": None, "sha": "s", "body": "should this be public API?", "body_hash": "h6"}]
        self.adjudication = {"decisions": [{"id": 6, "decision": "question", "reply": "Is Well's slot API public?"}]}
        r, steps, t = self.run_once()
        self.assertEqual(t.state, "paused"); self.assertIn("need Cody", t.reason); self.assertEqual(self.replies, [])

    def test_accepted_task_with_same_id_but_other_slug_is_not_skipped(self):
        """Re-plans restart numbering at 001; an old accepted 001 with a different slug must not satisfy the new 001."""
        tdir = self.cfg.ticket_dir("ZIP-7873"); tt = state.load(tdir)
        tt.attempts["ZIP-7873/001"] = [{"n": 1, "outcome": "accepted", "rung": {"agent": "x", "tier": "cheap", "n": 1}}]; state.save(tdir, tt)
        d = self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "1"; d.mkdir(parents=True); (d / "task.toml").write_text('id = "001"\nslug = "old-hand-task"\n')
        r, steps, t = self.run_once(outcomes=("rejected",))
        self.assertEqual(r.calls, ["001"])

    def test_visual_task_runs_screenshot_gate_and_pr_body_has_table(self):
        p = self.wt / "planning" / "zip-7873" / "tasks.toml"; p.write_text(MANIFEST.replace('slug = "b"', 'slug = "b"\nvisual = true'))
        (self.wt / "planning" / "zip-7873" / "plan.md").write_text("# plan\n\n## Design decisions\n\n- Tier 1: follow the skill\n\n## Evidence\n\n- read things\n")
        bodies = []
        with patch.object(lifecycle.publish, "ensure_draft_pr", side_effect=lambda wt, b, title, body: bodies.append((title, body)) or {"number": 1, "isDraft": True}), \
             patch.object(lifecycle.publish, "existing_pr", return_value=None), patch.object(lifecycle.publish, "record_pr_on_worktree"):
            r, steps, t = self.run_once(outcomes=("accepted", "accepted"), max_steps=6)
        self.assertTrue((self.cfg.ticket_dir("ZIP-7873") / "screenshots.json").exists())
        title, body = bodies[0]
        self.assertEqual(title, "INTERNAL: Add ZUI Well component ZIP-7873")
        self.assertIn("|Scenario|AFTER|", body); self.assertIn("Tier 1: follow the skill", body); self.assertIn("planning/zip-7873/plan.md", body)

    def test_visual_gate_failure_pauses(self):
        p = self.wt / "planning" / "zip-7873" / "tasks.toml"; p.write_text(MANIFEST.replace('slug = "b"', 'slug = "b"\nvisual = true'))
        with patch.object(lifecycle.screenshots, "capture", side_effect=lifecycle.screenshots.VisualGateError("preview 500")):
            r, steps, t = self.run_once()
        self.assertEqual(t.state, "paused"); self.assertIn("visual gate failed", t.reason)

    def test_accepted_task_is_recognised_from_real_attempt_layout(self):
        """Live 2026-09-14: slug lives in task.toml (attempt.py writes both); checking task.md never matched,
        so task 001 was re-run/re-published in a loop until max_steps."""
        import attempt as attempt_mod, contracts as c
        task = c.load_tasks(self.wt / "planning" / "zip-7873" / "tasks.toml")[0]
        d = self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "1"; d.mkdir(parents=True)
        (d / "task.toml").write_text(attempt_mod.task_toml(task)); (d / "task.md").write_text(attempt_mod.task_md(task, d, self.wt))
        tdir = self.cfg.ticket_dir("ZIP-7873"); tt = state.load(tdir)
        tt.attempts["ZIP-7873/001"] = [{"n": 1, "outcome": "accepted", "rung": {"agent": "x", "tier": "cheap", "n": 1}}]; state.save(tdir, tt)
        self.assertTrue(lifecycle._task_accepted(self.cfg, "ZIP-7873", task))

    def test_operator_pause_file_stops_everything(self):
        (self.cfg.state_root / "PAUSE").touch()
        r, steps, t = self.run_once()
        self.assertEqual(r.calls, []); self.assertEqual(steps[0].action, "operator PAUSE")


if __name__ == "__main__":
    unittest.main()
