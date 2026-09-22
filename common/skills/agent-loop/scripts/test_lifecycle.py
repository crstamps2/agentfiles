import json
import os
import pathlib
import subprocess
import tempfile
import time
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
            patch.object(lifecycle.publish, "push_rebased"),
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
            patch.object(lifecycle, "_notify_once", side_effect=lambda tdir, key, title, body: self.notified.append(title)),
            patch.object(lifecycle, "_pr_house_style", side_effect=lambda cfg, key, wt, t: ("INTERNAL: Add ZUI Well component ZIP-7873", self.fake_body())),
        ]
        for p in self.patches: p.start()
        self.gh_writes = []; self.notified = []; self.checks = [ci.Check("zipline", "pass")]; self.comments = []; self.marked = []; self.replies = []; self.adjudication = None

    def tearDown(self):
        for p in self.patches: p.stop()
        self.tmp.cleanup()

    def fake_body(self):
        import prbody
        shots = self.cfg.ticket_dir("ZIP-7873") / "screenshots.json"
        table = "|Scenario|AFTER|\n|----|----|\n|`default`|x|" if shots.exists() else None
        return prbody.fallback_body("ZIP-7873", "Well", ["app/a.rb"], table)

    def fake_sh(self, cmd, cwd, timeout):
        import subprocess
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def fake_adjudicator(self, cfg, agent, prompt, stage, wt, timeout, model=None):
        self.assertEqual(agent, "review-adjudicator")            # never the plan critic's definition
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
        self.assertEqual([v for v, a in self.gh_writes], ["pr-edit-body"])
        lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)   # one more step at the gate
        self.assertIn("ZIP-7873 ready for review", self.notified)      # existing PR gets the plan-derived title/body
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

    def test_tier3_flag_records_questions_but_does_not_block_mark_ready(self):
        """Cody (2026-09-14): he injects himself only at Human Gate 1, after the bot review. The loop never
        assigns reviewers, so marking ready is not colleague exposure; tier-3 questions travel to the report."""
        p = self.wt / "planning" / "zip-7873" / "tasks.toml"; p.write_text("human_confirm_before_ready = true\n# Owner/design questions: confirm single-tone scope, div-only tag enum.\n" + MANIFEST)
        r, steps, t = self.run_once()
        self.assertEqual(self.marked, [47888])
        cis = json.loads((self.cfg.ticket_dir("ZIP-7873") / "ci.json").read_text())
        self.assertIn("div-only tag enum", cis["tier3_pending"])

    def test_review_fix_decision_queues_task_and_replies(self):
        self.comments = [{"kind": "inline", "id": 5, "path": "app/a.rb", "line": 3, "reply_to": None, "sha": "s", "body": "nil guard missing", "body_hash": "h5"}]
        self.adjudication = {"decisions": [{"id": 5, "decision": "fix", "reply": "Adding the guard; see follow-up commit.",
                                            "task": {"allowed_files": ["app/a.rb"], "verification_commands": ["true"], "summary": "Add nil guard"}}]}
        r, steps, t = self.run_once(outcomes=("accepted", "accepted", "rejected"))
        self.assertEqual(r.calls, ["001", "002", "901"])          # the review fix task was dispatched to the ladder
        self.assertEqual(t.state, "implement"); self.assertIn("review-fix", (self.wt / "planning" / "zip-7873" / "tasks.toml").read_text())
        self.assertEqual(self.replies[0][0], 5)
        ledger = json.loads((self.cfg.ticket_dir("ZIP-7873") / "bot-ledger.json").read_text()); self.assertEqual(ledger["5"], "h5")

    def test_reply_publish_error_pauses_and_keeps_ledger(self):
        self.comments = [{"kind": "inline", "id": 5, "path": "a.rb", "line": 3, "reply_to": None, "sha": "s", "body": "x", "body_hash": "h5"}]
        self.adjudication = {"decisions": [{"id": 5, "decision": "decline", "reply": "No."}]}
        with patch.object(lifecycle.botreview, "reply", side_effect=lifecycle.publish.PublishError("boom")):
            r, steps, t = self.run_once()
        self.assertEqual(t.state, "paused"); self.assertIn("reply failed", t.reason)

    def test_existing_adjudication_is_reused_not_repaid(self):
        self.comments = [{"kind": "inline", "id": 5, "path": "a.rb", "line": 3, "reply_to": None, "sha": "s", "body": "x", "body_hash": "h5"}]
        stage = self.cfg.state_root / "botreview" / "ZIP-7873" / "round-1"; stage.mkdir(parents=True)
        (stage / "adjudication.json").write_text(json.dumps({"decisions": [{"id": 5, "decision": "decline", "reply": "Already decided."}]}))
        (self.cfg.ticket_dir("ZIP-7873") / "ci.json").write_text(json.dumps({"pr": 47888, "bot_rounds": 1, "actions": []}))
        calls = []
        with patch.object(lifecycle.plan_mod, "_run_agent", side_effect=lambda *a, **k: calls.append(1)):
            r, steps, t = self.run_once()
        self.assertEqual(calls, []); self.assertEqual(self.replies[0][0], 5); self.assertEqual(t.state, "human-gate-1")

    def test_provider_usage_limit_in_adjudication_is_a_transient_not_a_defect(self):
        """ZIP-7875: Codex returned 'usage limit has been reached'; the loop recorded 'adjudication unusable'
        and stopped. It must pause as a provider transient, not consume the bot round."""
        self.comments = [{"kind": "inline", "id": 7, "path": "a.rb", "line": 1, "reply_to": None, "sha": "s", "body": "x", "body_hash": "h7"}]
        def dead_agent(cfg, agent, prompt, stage, wt, timeout, model=None):
            stage.mkdir(parents=True, exist_ok=True); (stage / "stderr.log").write_text("Codex error: The usage limit has been reached\n")
        with patch.object(lifecycle.plan_mod, "_run_agent", side_effect=dead_agent):
            r, steps, t = self.run_once()
        self.assertEqual(t.state, "paused"); self.assertIn("provider unavailable", t.reason)
        cis = json.loads((self.cfg.ticket_dir("ZIP-7873") / "ci.json").read_text()); self.assertEqual(cis["bot_rounds"], 0)   # round not consumed
        import heal; self.assertEqual(heal.policy(t.reason)[0], "retry")

    def test_invalid_generated_fix_task_is_rejected_and_repairable_not_a_crash_loop(self):
        """ZIP-7875: adjudicator fix tasks lacked --force-exclusion; the manifest became unloadable and the
        runner raised ManifestError on every launchd restart. The append must roll back and pause repairably."""
        self.comments = [{"kind": "inline", "id": 9, "path": "a.rb", "line": 1, "reply_to": None, "sha": "s", "body": "x", "body_hash": "h9"}]
        self.adjudication = {"decisions": [{"id": 9, "decision": "fix", "reply": "ok", "task": {
            "allowed_files": ["app/a.rb"], "verification_commands": ["bin/agent_run rubocop --cache false app/a.rb", "x", "y", "z", "w"], "summary": "fix"}}]}
        before = (self.wt / "planning" / "zip-7873" / "tasks.toml").read_text()
        r, steps, t = self.run_once()
        self.assertEqual((self.wt / "planning" / "zip-7873" / "tasks.toml").read_text(), before)      # manifest untouched
        import contracts as c; c.load_tasks(self.wt / "planning" / "zip-7873" / "tasks.toml")          # still loadable
        self.assertEqual(t.state, "paused"); self.assertTrue((self.cfg.ticket_dir("ZIP-7873") / "bad-guards.md").exists())
        import heal; self.assertEqual(heal.policy(t.reason)[0], "repair")                              # self-heals

    def test_interrupted_rebase_is_recovered_before_any_stage_reads_the_branch(self):
        """ZIP-7875: a crash loop killed the runner mid-rebase; HEAD was detached, so publish reported
        'refusing to publish from branch \'\''. Every step first finishes or aborts the interrupted rebase."""
        gd = self.wt / ".git"; (gd / "rebase-merge").mkdir(parents=True, exist_ok=True)
        calls = []
        def fake_sh(argv, timeout=120):
            calls.append(argv)
            out = {("git", "rev-parse", "--git-dir"): ".git"}.get(tuple(argv), "")
            return subprocess.CompletedProcess(argv, 0, out, "")
        # a hand-written conflict cannot be finished: abort, restoring the branch
        with patch.object(lifecycle, "_sh", side_effect=lambda argv, w, t=120: fake_sh(argv, t)):
            def unmerged(argv, timeout=120):
                calls.append(argv)
                if argv[:3] == ["git", "diff", "--name-only"]:
                    return subprocess.CompletedProcess(argv, 0, "app/models/thing.rb\n", "")
                return subprocess.CompletedProcess(argv, 0, ".git" if "--git-dir" in argv else "", "")
                
            msg = lifecycle.recover_interrupted_git_state(self.wt, sh=unmerged)
        self.assertIn("aborted", msg)
        self.assertIn(["git", "rebase", "--abort"], calls)

    def test_interrupted_rebase_with_only_locale_conflicts_is_finished(self):
        gd = self.wt / ".git"; (gd / "rebase-merge").mkdir(parents=True, exist_ok=True)
        calls = []
        def sh(argv, timeout=120):
            calls.append(argv)
            if argv[:3] == ["git", "diff", "--name-only"]:
                return subprocess.CompletedProcess(argv, 0, "config/locales/en.yml\n", "")
            return subprocess.CompletedProcess(argv, 0, ".git" if "--git-dir" in argv else "", "")
        msg = lifecycle.recover_interrupted_git_state(self.wt, sh=sh)
        self.assertIn("finished an interrupted rebase", msg)
        self.assertTrue(any("i18n-tasks normalize" in " ".join(c) for c in calls))
        self.assertNotIn(["git", "rebase", "--abort"], calls)

    def test_pre_commit_lint_rejection_becomes_a_bounded_fix_task(self):
        """ZIP-7875: the hook rejected the commit on Rails/FindEach + Metrics/AbcSize. Healing must send a
        worker to fix the offenses (allowlist = the offending files), not republish the same commit."""
        tdir = self.cfg.ticket_dir("ZIP-7873")
        reason = ("gates failed: pre-commit lint rejected the commit: "
                  "test/system/x_test.rb:161:48: C: [Correctable] Rails/FindEach: Use find_each.\n"
                  "test/system/x_test.rb:160:3: C: Metrics/AbcSize: too high.")
        state.save(tdir, state.Ticket(key="ZIP-7873", state="paused", previous="implement", reason=reason))
        r, steps, t = self.run_once(max_steps=2)                    # heal step, then the fix task is queued
        self.assertIn(t.state, ("implement", "paused"))
        self.assertTrue(any("queued lint fix task" in x.action for x in steps))
        import contracts
        tasks = contracts.load_tasks(self.wt / "planning" / "zip-7873" / "tasks.toml")
        fix = [x for x in tasks if x.id.startswith("9")][-1]
        self.assertEqual(fix.allowed_files, ["test/system/x_test.rb"])
        self.assertTrue(any("--force-exclusion" in c for c in fix.verification_commands))
        self.assertTrue(any("Rails/FindEach" in i for i in fix.invariants))

    def test_waiting_duration_is_reported_in_the_reminder(self):
        tdir = self.cfg.ticket_dir("ZIP-7873"); tdir.mkdir(parents=True, exist_ok=True)
        state.save(tdir, state.Ticket(key="ZIP-7873", state="paused", previous="plan", reason="x"))
        import os, time as _t
        old = _t.time() - 3 * 86400
        os.utime(tdir / "state.json", (old, old))
        self.assertEqual(lifecycle._waiting_since(tdir, None), " (waiting 3d)")

    def test_a_prettier_hook_rejection_also_becomes_a_fix_task(self):
        """ZIP-7874: the pre-commit prettier check rejected a .ts controller. The classifier only knew
        rubocop-style cop names, so it fell through to republish and retried the same rejected commit."""
        import publish, heal
        reason = ("gates failed: pre-commit lint rejected the commit: Checking formatting... "
                  "[warn] app/assets/javascripts/controllers/zui_dropzone_controller.ts\n"
                  "Code style issues found in the above file(s).")
        self.assertEqual(heal.policy(reason)[0], "fix-task")
        tdir = self.cfg.ticket_dir("ZIP-7873")
        tid = lifecycle._lint_fix_task(tdir, reason, self.wt / "planning" / "zip-7873" / "tasks.toml")
        import contracts
        fix = [t for t in contracts.load_tasks(self.wt / "planning" / "zip-7873" / "tasks.toml") if t.id == tid][0]
        self.assertEqual(fix.allowed_files, ["app/assets/javascripts/controllers/zui_dropzone_controller.ts"])
        self.assertTrue(any("prettier" in c for c in fix.verification_commands))   # the tool that rejected it

    def test_fix_task_reads_the_full_hook_output_not_the_truncated_reason(self):
        """The reason field is capped at 400 chars, which kept only lefthook's trailing summary."""
        tdir = self.cfg.ticket_dir("ZIP-7873"); tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "hook-reject.out").write_text(
            "[warn] app/assets/javascripts/controllers/zui_dropzone_controller.ts\n"
            "Code style issues found in the above file(s).\n" + "OK hook passed\n" * 100)
        reason = "gates failed: pre-commit lint rejected the commit: No new instances of lodash."
        tid = lifecycle._lint_fix_task(tdir, reason, self.wt / "planning" / "zip-7873" / "tasks.toml")
        import contracts
        fix = [t for t in contracts.load_tasks(self.wt / "planning" / "zip-7873" / "tasks.toml") if t.id == tid][0]
        self.assertEqual(fix.allowed_files, ["app/assets/javascripts/controllers/zui_dropzone_controller.ts"])

    def test_review_fix_ids_do_not_collide_with_ids_already_in_the_manifest(self):
        """ZIP-7874: an operator-queued 901 was already in the plan, so the bot's first fix task was also
        numbered 901 and the whole batch was rejected as a duplicate."""
        mf = self.wt / "planning" / "zip-7873" / "tasks.toml"
        lifecycle._append_task(mf, {"id": "901", "slug": "operator-followup", "size": "small", "summary": "s",
                                    "allowed_files": ["app/a.rb"], "acceptance": ["AC-1: x"],
                                    "verification_commands": ["bin/agent_run rubocop --cache false --force-exclusion app/a.rb"]})
        self.comments = [{"kind": "inline", "id": 11, "path": "app/a.rb", "line": 1, "reply_to": None, "sha": "s", "body": "b", "body_hash": "h11"}]
        self.adjudication = {"decisions": [{"id": 11, "decision": "fix", "reply": "ok", "task": {
            "allowed_files": ["app/a.rb"], "summary": "fix it",
            "verification_commands": ["bin/agent_run rubocop --cache false --force-exclusion app/a.rb"]}}]}
        r, steps, t = self.run_once(outcomes=("accepted",) * 6, max_steps=12)
        import contracts
        ids = [x.id for x in contracts.load_tasks(mf)]
        self.assertEqual(len(ids), len(set(ids)))                     # no duplicate
        self.assertIn("902", ids)                                     # numbered past the existing 901
        self.assertNotIn("rejected by the schema", state.load(self.cfg.ticket_dir("ZIP-7873")).reason)

    def test_gate_refuses_to_open_while_a_promised_review_fix_is_unimplemented(self):
        """ZIP-7874 replied "fixed" to 6 comments, the fix batch was rejected, and the loop still reached
        Human Gate 1 with zero commits. A reply is a public promise; the gate must refuse."""
        tdir = self.cfg.ticket_dir("ZIP-7873"); tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "pending-fixes.json").write_text(json.dumps({"4066916935": "902"}))
        state.save(tdir, state.Ticket(key="ZIP-7873", state="bot-loop", previous="ready"))
        (tdir / "ci.json").write_text(json.dumps({"pr": 1, "bot_rounds": 1, "actions": []}))
        self.comments = []                                        # every comment already answered
        r, steps, t = self.run_once(max_steps=2)
        self.assertEqual(t.state, "paused")
        self.assertIn("promised in replies but never implemented", t.reason)
        self.assertIn("comment 4066916935 -> task 902", t.reason)

    def test_gate_opens_once_the_promised_fix_is_accepted(self):
        tdir = self.cfg.ticket_dir("ZIP-7873"); tdir.mkdir(parents=True, exist_ok=True)
        mf = self.wt / "planning" / "zip-7873" / "tasks.toml"
        lifecycle._append_task(mf, {"id": "902", "slug": "review-fix-1", "size": "small", "summary": "s",
                                    "allowed_files": ["app/a.rb"], "acceptance": ["AC-1: x"],
                                    "verification_commands": ["bin/agent_run rubocop --cache false --force-exclusion app/a.rb"]})
        (tdir / "pending-fixes.json").write_text(json.dumps({"4066916935": "902"}))
        with patch.object(lifecycle, "_task_accepted", return_value=True):
            unmet = lifecycle._unmet_review_promises(self.cfg, "ZIP-7873", self.wt)
        self.assertEqual(unmet, {})
        self.assertEqual(json.loads((tdir / "pending-fixes.json").read_text()), {})   # cleared

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
        self.assertIn("|Scenario|AFTER|", body); self.assertNotIn("planning/", body)
        import prbody; self.assertEqual(prbody.lint(body), [])

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

    def test_insert_task_before_places_fix_ahead_of_target(self):
        import contracts as c
        m = self.wt / "planning" / "zip-7873" / "tasks.toml"
        lifecycle.insert_task_before(m, {"id": "001a", "slug": "fix", "summary": "fix", "allowed_files": ["a"], "verification_commands": ["true"], "acceptance": ["z"]}, "002")
        self.assertEqual([x.id for x in c.load_tasks(m)], ["001", "001a", "002"])

    def test_bad_guard_pause_is_self_repaired_by_task_writer_and_resumes(self):
        tdir = self.cfg.ticket_dir("ZIP-7873"); tt = state.load(tdir); tt.worktree = str(self.wt)
        tt = state.transition(tt, "paused", reason="guard failed on the BASE tree too (independent of the work): sort -c config/en.yml"); state.save(tdir, tt)
        (tdir / "bad-guards.md").write_text("## task 001\n```\nsort -c config/en.yml\n```\n")
        def writer(cfg, agent, prompt, stage, wt, timeout, model=None):
            self.assertEqual(agent, "task-writer"); self.assertIn("bad-guards.md", prompt)
        with patch.object(lifecycle.plan_mod, "_run_agent", side_effect=writer):
            step = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        self.assertIn("repaired", step.action); self.assertEqual(state.load(tdir).state, "implement")
        self.assertFalse((tdir / "bad-guards.md").exists())

    def test_behind_main_rebases_before_each_task(self):
        rebased = []
        def sh(cmd, cwd, timeout):
            import subprocess
            if cmd[:2] == ["git", "rebase"]: rebased.append(cmd)
            if cmd[:2] == ["git", "status"]: return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with patch.object(lifecycle.ci, "behind_base", return_value=3), patch.object(lifecycle, "_sh", side_effect=sh):
            r, steps, t = self.run_once(outcomes=("rejected",))
        self.assertEqual(rebased, [["git", "rebase", "origin/main"]]); self.assertEqual(r.calls, ["001"])

    def test_gates_rubocop_honours_repo_exclusions_and_builds_assets_first(self):
        """d1eff6d: explicit file args bypass AllCops Exclude unless --force-exclusion is passed (13 false offenses on linters/**).
        Also asserts `yarn build` precedes any test/browser step (critic finding: stale app/assets/builds)."""
        cmds = []
        def sh(cmd, cwd, timeout):
            import subprocess
            cmds.append(" ".join(cmd) if isinstance(cmd, list) else cmd)
            if cmd[:3] == ["git", "diff", "--name-only"]: return subprocess.CompletedProcess(cmd, 0, "linters/x.rb\napp/y.rb\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with patch.object(lifecycle, "_sh", side_effect=sh):
            r, steps, t = self.run_once(outcomes=("accepted", "accepted"))
        rubocop = [c for c in cmds if "rubocop" in c]; self.assertTrue(rubocop); self.assertIn("--force-exclusion", rubocop[0])
        build_i = next(i for i, c in enumerate(cmds) if "yarn build" in c); test_i = next(i for i, c in enumerate(cmds) if "rails test" in c)
        self.assertLess(build_i, test_i)

    def test_rebase_conflict_in_generated_file_is_regenerated_not_escalated(self):
        seq = {"n": 0}
        def sh(cmd, cwd, timeout):
            import subprocess
            key = " ".join(cmd)
            if cmd[:2] == ["git", "rebase"] and "--continue" not in cmd: return subprocess.CompletedProcess(cmd, 1, "", "CONFLICT")
            if "--diff-filter=U" in key:
                seq["n"] += 1; return subprocess.CompletedProcess(cmd, 0, ".rubocop/zui_todo.yml\n" if seq["n"] == 1 else "", "")
            if cmd[:2] == ["git", "status"]: return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with patch.object(lifecycle.ci, "behind_base", return_value=2), patch.object(lifecycle, "_sh", side_effect=sh):
            r, steps, t = self.run_once(outcomes=("rejected",))
        self.assertEqual(r.calls, ["001"]); self.assertNotEqual(t.state, "paused")      # resolved, task dispatched

    def test_rebase_conflict_in_hand_written_file_pauses(self):
        def sh(cmd, cwd, timeout):
            import subprocess
            if cmd[:2] == ["git", "rebase"] and "--continue" not in cmd: return subprocess.CompletedProcess(cmd, 1, "", "CONFLICT")
            if "--diff-filter=U" in " ".join(cmd): return subprocess.CompletedProcess(cmd, 0, "app/views/components/zui/x.rb\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with patch.object(lifecycle.ci, "behind_base", return_value=2), patch.object(lifecycle, "_sh", side_effect=sh):
            r, steps, t = self.run_once(outcomes=("rejected",))
        self.assertEqual(t.state, "paused"); self.assertIn("needs a human", t.reason); self.assertEqual(r.calls, [])

    def test_rebase_conflict_in_en_yml_resolved_by_taking_head_and_normalizing(self):
        """config/locales/en.yml conflicts during rebase are resolved by keeping HEAD for each
        conflicting hunk (preserving non-conflicting additions) and running i18n-tasks normalize.
        The ticket must NOT be paused. (ZIP-7875)"""
        import os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            en_yml = os.path.join(tmp, "config", "locales", "en.yml")
            os.makedirs(os.path.dirname(en_yml))
            # Simulate en.yml with a conflict marker; HEAD keeps its content, theirs adds stale keys.
            # Non-conflicting stepper keys added earlier in the rebase must be preserved.
            open(en_yml, "w").write(
                "en:\n"
                "  components:\n"
                "    zui:\n"
                "      stepper:\n"
                "        aria_label: Progress\n"  # non-conflicting addition -- must survive
                "<<<<<<< HEAD\n"
                "  zipassist:\n"
                "=======\n"
                "  zipassist:\n"
                "    stale_key: old value\n"
                ">>>>>>> abc123 (some commit)\n"
                "  other: value\n"
            )
            # normalize_cmd is "true" -- a no-op that exits 0, simulating a clean normalize
            with patch.object(lifecycle, "_sh", return_value=__import__("subprocess").CompletedProcess([], 0, "", "")):
                result = lifecycle._resolve_normalized_conflict("config/locales/en.yml", tmp, "true")
            self.assertTrue(result)
            resolved = open(en_yml).read()
            self.assertIn("stepper:", resolved)        # non-conflicting addition preserved
            self.assertIn("aria_label: Progress", resolved)
            self.assertNotIn("stale_key", resolved)    # theirs (stale) side dropped
            self.assertNotIn("<<<<<<<", resolved)      # no conflict markers remain

    def test_rebase_conflict_in_en_yml_is_not_escalated_to_human(self):
        """A rebase with only en.yml conflicting must not pause the ticket."""
        seq = {"n": 0}
        def sh(cmd, cwd, timeout):
            import subprocess
            if cmd[:2] == ["git", "rebase"] and "--continue" not in cmd:
                return subprocess.CompletedProcess(cmd, 1, "", "CONFLICT")
            if "--diff-filter=U" in " ".join(cmd):
                seq["n"] += 1
                return subprocess.CompletedProcess(cmd, 0, "config/locales/en.yml\n" if seq["n"] == 1 else "", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with patch.object(lifecycle.ci, "behind_base", return_value=2), \
             patch.object(lifecycle, "_sh", side_effect=sh), \
             patch.object(lifecycle, "_resolve_normalized_conflict", return_value=True):
            r, steps, t = self.run_once(outcomes=("rejected",))
        self.assertNotEqual(t.state, "paused")  # resolved without human intervention
        self.assertEqual(r.calls, ["001"])       # implement resumed, worker ran

    def test_paused_ticket_self_heals_by_policy_and_records_it(self):
        """A transient pause (heavy lane) re-enters its previous stage without a human; heal.jsonl records it."""
        tdir = self.cfg.ticket_dir("ZIP-7873"); tt = state.load(tdir)
        tt = state.transition(tt, "paused", reason="heavy lane held by a live owner"); state.save(tdir, tt)
        r, steps, t = self.run_once(outcomes=("rejected",))
        self.assertEqual(steps[0].action, "self-heal: retry"); self.assertEqual(r.calls, ["001"])   # implement resumed, worker ran
        import heal; self.assertEqual(heal.history(tdir)[0]["action"], "retry")

    def test_spent_heal_budget_waits_for_operator(self):
        import heal
        tdir = self.cfg.ticket_dir("ZIP-7873"); tt = state.load(tdir)
        for _ in range(2): heal.record(tdir, "prepare-retry", "gates failed: rails test rc=1", "gates")
        tt = state.transition(tt, "gates"); tt = state.transition(tt, "paused", reason="gates failed: rails test rc=1"); state.save(tdir, tt)
        r, steps, t = self.run_once()
        self.assertTrue(steps[0].wait); self.assertIn("needs operator", steps[0].action); self.assertEqual(r.calls, [])
        self.assertIn("ZIP-7873 needs an operator", self.notified)

    def test_human_pause_reasons_wait(self):
        tdir = self.cfg.ticket_dir("ZIP-7873"); tt = state.load(tdir)
        tt = state.transition(tt, "paused", reason="rebase onto origin/main conflicted before task 003; needs a human"); state.save(tdir, tt)
        r, steps, t = self.run_once()
        self.assertTrue(steps[0].wait); self.assertEqual(r.calls, [])

    def test_accepted_but_uncommitted_task_is_republished_before_new_work(self):
        import contracts as c, attempt as attempt_mod
        tasks = c.load_tasks(self.wt / "planning" / "zip-7873" / "tasks.toml")
        tdir = self.cfg.ticket_dir("ZIP-7873"); tt = state.load(tdir)
        tt.attempts["ZIP-7873/001"] = [{"n": 1, "outcome": "accepted", "rung": {"agent": "x", "tier": "cheap", "n": 1}}]; state.save(tdir, tt)
        d = self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "1"; d.mkdir(parents=True); (d / "task.toml").write_text(attempt_mod.task_toml(tasks[0]))
        (self.wt / "app").mkdir(exist_ok=True); (self.wt / "app" / "a.rb").write_text("accepted but never committed\n")
        published = []
        with patch("runner.publish_accepted", side_effect=lambda cfg, key, wt, tk, ensure_pr=True: published.append(tk.id)), \
             patch("runner._uncommitted_allowlisted", side_effect=lambda wt, tk: ["app/a.rb"] if tk.id == "001" else []):
            step = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        self.assertEqual(published, ["001"]); self.assertIn("published previously accepted work", step.action)

    def test_failed_plan_with_revise_review_is_repaired_not_replanned(self):
        tdir = self.cfg.ticket_dir("ZIP-7873"); tt = state.Ticket(key="ZIP-7873", state="plan", previous="spinup"); state.save(tdir, tt)
        (self.wt / "planning" / "zip-7873" / "plan.md").write_text("plan")
        (self.wt / "planning" / "zip-7873" / "plan-review.md").write_text("VERDICT: revise\nBLOCKERS:\n1. x")
        calls = []
        with patch.object(lifecycle.plan_mod, "repair_plan", side_effect=lambda *a, **k: calls.append("repair") or {"result": "approved", "rounds": [], "author_model": "A", "critic_model": "C"}), \
             patch.object(lifecycle.plan_mod, "plan_ticket", side_effect=lambda *a, **k: calls.append("replan") or {"result": "approved", "rounds": []}):
            step = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        self.assertEqual(calls, ["repair"]); self.assertEqual(state.load(tdir).state, "implement")
        self.assertEqual((tdir / "plan-repairs.n").read_text(), "1")

    def test_plan_failed_pause_heals_back_to_plan_stage(self):
        tdir = self.cfg.ticket_dir("ZIP-7873"); tt = state.load(tdir); tt.state = "plan"; tt.previous = "spinup"; state.save(tdir, tt)
        tt = state.transition(tt, "paused", reason="plan failed: critic verdict 'revise' after 2 rounds"); state.save(tdir, tt)
        with patch.object(lifecycle.plan_mod, "repair_plan", return_value={"result": "approved", "rounds": [], "author_model": "A", "critic_model": "C"}):
            steps = lifecycle.run_once(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2, max_steps=2)
        self.assertEqual(steps[0].action, "self-heal: replan"); self.assertIn("approved", steps[1].action)

    def test_planning_ceiling_stops_a_ticket_that_never_produced_code(self):
        """ZIP-7874: $55 across 7 plan runs and 3 heal replans, no accepted task, nobody notified."""
        tdir = self.cfg.ticket_dir("ZIP-7873"); state.save(tdir, state.Ticket(key="ZIP-7873", state="plan", previous="spinup"))
        (self.wt / "planning" / "zip-7873" / "plan.md").write_text("plan")
        (self.wt / "planning" / "zip-7873" / "plan-review.md").write_text("VERDICT: revise\nBLOCKERS:\n1. x")
        with patch.object(lifecycle, "_task_accepted", return_value=False), \
             patch.object(lifecycle.ledger, "collect", return_value=[{"ticket": "ZIP-7873", "billed_usd": 31.0}]), \
             patch.object(lifecycle.plan_mod, "repair_plan") as rep, patch.object(lifecycle.plan_mod, "plan_ticket") as pl:
            step = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        rep.assert_not_called(); pl.assert_not_called()
        self.assertTrue(step.wait); self.assertIn("planning ceiling", state.load(tdir).reason)
        self.assertIn("ZIP-7873 planning ceiling", self.notified)
        import heal; self.assertEqual(heal.policy(state.load(tdir).reason)[0], "human")   # never auto-retried

    def test_owner_ruling_resets_the_planning_ceiling_baseline(self):
        """A ruling is new evidence: the budget that stops repeated arguments must not block the round the
        ruling authorises. Spend BEFORE the ruling no longer counts."""
        tdir = self.cfg.ticket_dir("ZIP-7873"); state.save(tdir, state.Ticket(key="ZIP-7873", state="plan", previous="spinup"))
        pd = self.wt / "planning" / "zip-7873"
        pd.joinpath("plan.md").write_text("# plan\n\n## Owner rulings\n\n- do the thing\n")
        pd.joinpath("plan-review.md").write_text("VERDICT: revise\nBLOCKERS:\n1. x")
        old = [{"ticket": "ZIP-7873", "billed_usd": 55.0, "first_ts": "2026-09-17T10:00:00Z"}]
        with patch.object(lifecycle, "_task_accepted", return_value=False), patch.object(lifecycle.ledger, "collect", return_value=old), \
             patch.object(lifecycle.plan_mod, "repair_plan", return_value={"result": "approved", "rounds": [], "author_model": "A", "critic_model": "C"}) as rep:
            step = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        rep.assert_called_once(); self.assertNotIn("ceiling", state.load(tdir).reason)
        self.assertTrue((tdir / "ruling-baseline.json").exists())

    def test_planning_ceiling_does_not_apply_once_work_is_accepted(self):
        tdir = self.cfg.ticket_dir("ZIP-7873"); state.save(tdir, state.Ticket(key="ZIP-7873", state="plan", previous="spinup"))
        (self.wt / "planning" / "zip-7873" / "plan.md").write_text("plan")
        (self.wt / "planning" / "zip-7873" / "plan-review.md").write_text("VERDICT: revise\nBLOCKERS:\n1. x")
        with patch.object(lifecycle, "_task_accepted", return_value=True), \
             patch.object(lifecycle.ledger, "collect", return_value=[{"ticket": "ZIP-7873", "billed_usd": 99.0}]), \
             patch.object(lifecycle.plan_mod, "repair_plan", return_value={"result": "approved", "rounds": [], "author_model": "A", "critic_model": "C"}):
            step = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        self.assertNotIn("ceiling", step.action); self.assertNotIn("ceiling", state.load(tdir).reason)

    def test_operator_pause_file_stops_everything(self):
        (self.cfg.state_root / "PAUSE").touch()
        r, steps, t = self.run_once()
        self.assertEqual(r.calls, []); self.assertEqual(steps[0].action, "operator PAUSE")


if __name__ == "__main__":
    unittest.main()


class NotificationTests(unittest.TestCase):
    """Uses the REAL _notify_once: LifecycleTests stubs it, so a test of its own behaviour must not
    live in that fixture (ZIP-7874 re-notification)."""

    def test_a_standing_human_block_re_notifies_every_six_hours(self):
        sent = []
        with tempfile.TemporaryDirectory() as d:
            tdir = pathlib.Path(d)
            def fake(*a, **k):
                sent.append(a[0][0]); return subprocess.CompletedProcess(a[0], 0, "", "")
            with patch("subprocess.run", side_effect=fake):
                lifecycle._notify_once(tdir, "ZIP-7874", "needs an operator", "plan blocked")
                lifecycle._notify_once(tdir, "ZIP-7874", "needs an operator", "plan blocked")
                self.assertEqual(sent.count("cmux"), 1)          # not a stream
                marker = next(tdir.glob(".notified-*"))
                old = time.time() - lifecycle.RENOTIFY_S - 60
                os.utime(marker, (old, old))
                lifecycle._notify_once(tdir, "ZIP-7874", "needs an operator", "plan blocked")
        self.assertEqual(sent.count("cmux"), 2)                  # ...but not silent either

