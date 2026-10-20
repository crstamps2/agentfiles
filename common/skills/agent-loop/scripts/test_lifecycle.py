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
import procs
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
        toml = (pathlib.Path(__file__).resolve().parent.parent / "hopper.toml").read_text().replace("\ncapacity = 2 ", "\ncapacity = 4 ")
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
            patch.object(lifecycle.ci, "pr_mergeable", return_value="MERGEABLE"),
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
        out = stage.parent if stage.name.startswith("fix-") else stage
        (out / "adjudication.json").write_text(json.dumps(self.adjudication or {"decisions": []}))

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

    def test_serial_execution_runs_a_later_prerequisite_before_its_dependent(self):
        manifest = self.wt / "planning/zip-7873/tasks.toml"
        manifest.write_text(MANIFEST.replace('slug = "b"', 'slug = "b"\nafter = ["001", "003"]') + '''
[[tasks]]
id = "003"
slug = "preview"
summary = "Build the preview required by task 002"
allowed_files = ["app/preview.rb"]
verification_commands = ["true"]
acceptance = ["AC-1"]
after = ["001"]
''')
        r, steps, t = self.run_once(outcomes=("accepted", "accepted", "accepted"), max_steps=3)
        self.assertEqual(r.calls, ["001", "003", "002"])

    def test_unsatisfied_dependencies_repair_the_manifest_instead_of_dispatching_or_passing_gates(self):
        manifest = self.wt / "planning/zip-7873/tasks.toml"
        for dependencies in (("002", "001"), ("missing", "001")):
            with self.subTest(dependencies=dependencies):
                manifest.write_text(MANIFEST.replace('slug = "a"', f'slug = "a"\nafter = ["{dependencies[0]}"]')
                                   .replace('slug = "b"', f'slug = "b"\nafter = ["{dependencies[1]}"]'))
                state.save(self.cfg.ticket_dir("ZIP-7873"), state.Ticket(key="ZIP-7873", state="implement"))
                r, steps, t = self.run_once(outcomes=("rejected",), max_steps=1)
                self.assertEqual(r.calls, [])
                self.assertEqual(t.state, "paused")
                self.assertTrue(t.reason.startswith("contract complaint: task dependencies"))
                self.assertIn(dependencies[0], (self.cfg.ticket_dir("ZIP-7873") / "bad-guards.md").read_text())

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

    def test_a_fix_task_the_schema_rejects_goes_back_to_the_adjudicator_before_any_reply(self):
        """ZIP-4281 round 1: the adjudicator's fix task used `ruby -e`, the schema rejected it after the
        reply loop, and every re-adjudication produced the same task until heal gave up (4 recurrences)."""
        self.comments = [{"kind": "inline", "id": 5, "path": "app/a.rb", "line": 3, "reply_to": None, "sha": "s", "body": "nil guard missing", "body_hash": "h5"}]
        bad = {"decisions": [{"id": 5, "decision": "fix", "reply": "Adding the guard; see follow-up commit.",
                               "task": {"allowed_files": ["app/a.rb"], "verification_commands": ["ruby -e 'abort unless File.read(\"app/a.rb\").include?(\"x\")'"], "summary": "Add nil guard"}}]}
        good = {"decisions": [{"id": 5, "decision": "fix", "reply": "Adding the guard; see follow-up commit.",
                                "task": {"allowed_files": ["app/a.rb"], "verification_commands": ["true"], "summary": "Add nil guard"}}]}
        prompts = []
        def adjudicate(cfg, agent, prompt, stage, wt, timeout, model=None):
            prompts.append(prompt)
            out = stage.parent if stage.name.startswith("fix-") else stage
            (out / "adjudication.json").write_text(json.dumps(bad if len(prompts) == 1 else good))
        with patch.object(lifecycle.plan_mod, "_run_agent", side_effect=adjudicate):
            r, steps, t = self.run_once(outcomes=("accepted", "accepted", "rejected"))
        self.assertEqual(len(prompts), 2)
        self.assertIn("inline interpreter code", prompts[1])          # the violation is fed back
        self.assertEqual(r.calls, ["001", "002", "901"]); self.assertEqual(t.state, "implement")
        self.assertEqual([c for c, _ in self.replies], [5])                # one reply, posted after a valid task existed

    def test_schema_fix_relaunch_keeps_the_original_adjudication_logs(self):
        """ZIP-8698: the schema-fix pass reused round-1's exclusive log paths and crashed."""
        self.comments = [{"kind": "inline", "id": 5, "path": "app/a.rb", "line": 3, "reply_to": None,
                          "sha": "s", "body": "nil guard missing", "body_hash": "h5"}]
        bad = {"decisions": [{"id": 5, "decision": "fix", "reply": "Adding the guard; see follow-up commit.",
                               "task": {"allowed_files": ["app/a.rb"], "verification_commands": ["ruby -e 'abort'"], "summary": "Add nil guard"}}]}
        good = {"decisions": [{"id": 5, "decision": "fix", "reply": "Adding the guard; see follow-up commit.",
                                "task": {"allowed_files": ["app/a.rb"], "verification_commands": ["true"], "summary": "Add nil guard"}}]}
        round_stage = self.cfg.state_root / "botreview" / "ZIP-7873" / "round-1"
        calls = 0

        def adjudicate(cfg, agent, prompt, stage, wt, timeout, model=None):
            nonlocal calls
            calls += 1
            stage.mkdir(parents=True, exist_ok=True)
            procs.run_stage(["/bin/echo", "initial" if calls == 1 else "repair"], wt, 10, None,
                            stage / "stdout.log", stage / "stderr.log")
            (round_stage / "adjudication.json").write_text(json.dumps(bad if calls == 1 else good))

        with patch.object(lifecycle.plan_mod, "_run_agent", side_effect=adjudicate):
            try:
                r, _steps, t = self.run_once(outcomes=("accepted", "accepted", "rejected"))
            except procs.LogPathExists as e:
                self.fail(f"schema-fix relaunch reused its log path: {e}")
        self.assertEqual(r.calls, ["001", "002", "901"])
        self.assertEqual(t.state, "implement")
        self.assertEqual((round_stage / "stdout.log").read_text(), "initial\n")
        retry_logs = [p for p in round_stage.parent.rglob("stdout.log") if p != round_stage / "stdout.log"]
        self.assertEqual(len(retry_logs), 1)
        self.assertEqual(retry_logs[0].read_text(), "repair\n")

    def test_a_cached_adjudication_whose_fix_task_breaks_the_schema_is_not_replayed(self):
        """ZIP-4281 after b0add05: the round-1 adjudication on disk was replayed and skipped the new check."""
        tdir = self.cfg.ticket_dir("ZIP-7873"); tdir.mkdir(parents=True, exist_ok=True)
        state.save(tdir, state.Ticket(key="ZIP-7873", state="bot-loop", previous="ready"))
        (tdir / "ci.json").write_text(json.dumps({"pr": 1, "bot_rounds": 1, "actions": []}))
        self.comments = [{"kind": "inline", "id": 5, "path": "app/a.rb", "line": 3, "reply_to": None, "sha": "s", "body": "x", "body_hash": "h5"}]
        cached = self.cfg.state_root / "botreview" / "ZIP-7873" / "round-1"; cached.mkdir(parents=True)
        (cached / "adjudication.json").write_text(json.dumps({"decisions": [{"id": 5, "decision": "fix", "reply": "Adding the guard.",
            "task": {"allowed_files": ["app/a.rb"], "verification_commands": ["ruby -e 'abort'"], "summary": "x"}}]}))
        self.adjudication = {"decisions": [{"id": 5, "decision": "fix", "reply": "Adding the guard.",
                             "task": {"allowed_files": ["app/a.rb"], "verification_commands": ["true"], "summary": "x"}}]}
        r, steps, t = self.run_once(max_steps=1)
        self.assertEqual(t.state, "implement"); self.assertIn("review-fix", (self.wt / "planning" / "zip-7873" / "tasks.toml").read_text())

    def test_reply_publish_error_pauses_and_keeps_ledger(self):
        self.comments = [{"kind": "inline", "id": 5, "path": "a.rb", "line": 3, "reply_to": None, "sha": "s", "body": "x", "body_hash": "h5"}]
        self.adjudication = {"decisions": [{"id": 5, "decision": "decline", "reply": "No."}]}
        with patch.object(lifecycle.botreview, "reply", side_effect=lifecycle.publish.PublishError("boom")):
            r, steps, t = self.run_once()
        self.assertEqual(t.state, "paused"); self.assertIn("reply failed", t.reason)

    def test_draft_pr_push_error_pauses_instead_of_crashing(self):
        """ZIP-4287: a failed push escaped the draft-PR stage and killed the supervised runner."""
        state.save(self.cfg.ticket_dir("ZIP-7873"), state.Ticket(key="ZIP-7873", state="draft-pr", previous="gates"))
        error = lifecycle.publish.PublishError("push-rebased failed rc=1: bash: command not found: atuin")
        with patch.object(lifecycle.publish, "push", side_effect=error):
            _r, steps, t = self.run_once(max_steps=1)
        self.assertEqual((t.state, t.previous), ("paused", "draft-pr"))
        self.assertIn("publish failed: push-rebased failed rc=1: bash: command not found: atuin", t.reason)
        self.assertEqual(steps[0].action, "paused"); self.assertTrue(steps[0].wait)
        import heal; self.assertEqual(heal.policy(t.reason)[0], "republish")

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

    def bot_loop_ticket(self, **ci):
        tdir = self.cfg.ticket_dir("ZIP-7873")
        state.save(tdir, state.Ticket(key="ZIP-7873", state="bot-loop", previous="ready", worktree=str(self.wt)))
        (tdir / "ci.json").write_text(json.dumps({"pr": 47888, "actions": [], "ready_marked_sha": "h" * 40, **ci}))
        return tdir

    def test_a_pr_that_conflicts_with_main_is_rebased_and_regated(self):
        # GitHub runs no workflows on a conflicting PR, so bot review never came (ZIP-4288 waited 54 h).
        tdir = self.bot_loop_ticket()
        with patch.object(lifecycle.ci, "pr_mergeable", return_value="CONFLICTING"), \
             patch.object(lifecycle, "_rebase_onto_main", return_value="") as rebase:
            step = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        rebase.assert_called_once()
        self.assertEqual(state.load(tdir).state, "gates")
        self.assertNotIn("ready_marked_sha", json.loads((tdir / "ci.json").read_text()))
        self.assertIn("rebased", step.action)

    def test_no_bot_review_retriggers_once_then_reaches_the_gate(self):
        import time as _time
        tdir = self.bot_loop_ticket(bot_wait_since=_time.time() - lifecycle.BOT_WAIT_S - 60)
        retriggers = []
        with patch.object(lifecycle.ci, "pr_mergeable", return_value="MERGEABLE"), \
             patch.object(lifecycle.botreview, "retrigger", side_effect=lambda pr, wt: retriggers.append(pr)):
            first = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
            self.assertTrue(first.wait); self.assertEqual(retriggers, [47888])
            cis = json.loads((tdir / "ci.json").read_text())
            cis["bot_wait_since"] = _time.time() - lifecycle.BOT_WAIT_S - 60
            (tdir / "ci.json").write_text(json.dumps(cis))
            lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        self.assertEqual(retriggers, [47888])
        self.assertEqual(state.load(tdir).state, "human-gate-1")

    def test_waiting_for_bot_review_inside_the_deadline_just_waits(self):
        tdir = self.bot_loop_ticket()
        with patch.object(lifecycle.ci, "pr_mergeable", return_value="MERGEABLE"), \
             patch.object(lifecycle.botreview, "retrigger") as retrigger:
            step = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        self.assertTrue(step.wait); retrigger.assert_not_called()
        self.assertIn("bot_wait_since", json.loads((tdir / "ci.json").read_text()))

    def test_a_hand_written_rebase_conflict_goes_to_a_worker_not_a_human(self):
        agents = []
        results = iter([subprocess.CompletedProcess([], 1, "", "CONFLICT (content)")])
        def sh(cmd, cwd, timeout):
            if cmd[:2] == ["git", "rebase"] and len(cmd) == 3:
                return next(results)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with patch.object(lifecycle, "_sh", side_effect=sh), \
             patch.object(lifecycle, "_resolve_generated_conflicts", return_value=False), \
             patch.object(lifecycle, "_rebase_finished", return_value=True), \
             patch.object(lifecycle.plan_mod, "_run_agent", side_effect=lambda cfg, name, *a, **k: agents.append(name)):
            err = lifecycle._rebase_onto_main(self.cfg, "ZIP-7873", self.wt)
        self.assertEqual(err, "")
        self.assertEqual(agents, ["premium-worker"])

    def test_a_rebase_that_moves_dependency_manifests_reinstalls_them(self):
        # main added a JS dependency; the rebased branch pushed with stale node_modules and the
        # pre-push typecheck failed (ZIP-4288, 2026-10-19).
        cmds = []
        def sh(cmd, cwd, timeout):
            cmds.append(cmd)
            if cmd[:3] == ["git", "diff", "--name-only"] and "ORIG_HEAD" in cmd[-1]:
                return subprocess.CompletedProcess(cmd, 0, "package.json\nyarn.lock\napp/x.rb\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with patch.object(lifecycle, "_sh", side_effect=sh):
            self.assertEqual(lifecycle._rebase_onto_main(self.cfg, "ZIP-7873", self.wt), "")
        self.assertIn(["bash", "-lc", "bin/wt prepare --for rails"], cmds)

    def test_a_rebase_with_unchanged_dependencies_skips_prepare(self):
        cmds = []
        def sh(cmd, cwd, timeout):
            cmds.append(cmd)
            if cmd[:3] == ["git", "diff", "--name-only"]:
                return subprocess.CompletedProcess(cmd, 0, "app/x.rb\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with patch.object(lifecycle, "_sh", side_effect=sh):
            lifecycle._rebase_onto_main(self.cfg, "ZIP-7873", self.wt)
        self.assertNotIn(["bash", "-lc", "bin/wt prepare --for rails"], cmds)

    def test_a_rejected_push_after_a_conflict_rebase_pauses_instead_of_crashing(self):
        tdir = self.bot_loop_ticket()
        with patch.object(lifecycle.ci, "pr_mergeable", return_value="CONFLICTING"), \
             patch.object(lifecycle, "_rebase_onto_main", return_value=""), \
             patch.object(lifecycle.publish, "push_rebased", side_effect=lifecycle.publish.PublishError("push-rebased failed rc=1")):
            step = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        self.assertTrue(step.wait)
        self.assertIn("publish failed", state.load(tdir).reason)

    def test_a_rejected_push_after_a_ci_rebase_pauses_instead_of_crashing(self):
        tdir = self.cfg.ticket_dir("ZIP-7873")
        state.save(tdir, state.Ticket(key="ZIP-7873", state="ready", previous="draft-pr", worktree=str(self.wt)))
        (tdir / "ci.json").write_text(json.dumps({"pr": 47888, "actions": []}))
        with patch.object(lifecycle.ci, "classify", return_value=type("V", (), {"action": "rebase", "reason": "behind", "failing": ()})()), \
             patch.object(lifecycle, "_rebase_onto_main", return_value=""), \
             patch.object(lifecycle.publish, "push_rebased", side_effect=lifecycle.publish.PublishError("push-rebased failed rc=1")):
            step = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        self.assertTrue(step.wait)
        self.assertIn("publish failed", state.load(tdir).reason)

    def test_an_unresolved_rebase_is_aborted_and_never_asks_a_human(self):
        aborted = []
        def sh(cmd, cwd, timeout):
            if cmd == ["git", "rebase", "--abort"]:
                aborted.append(True)
            rc = 1 if cmd[:2] == ["git", "rebase"] and len(cmd) == 3 else 0
            return subprocess.CompletedProcess(cmd, rc, "", "")
        with patch.object(lifecycle, "_sh", side_effect=sh), \
             patch.object(lifecycle, "_resolve_generated_conflicts", return_value=False), \
             patch.object(lifecycle, "_rebase_finished", return_value=False), \
             patch.object(lifecycle.plan_mod, "_run_agent"):
            err = lifecycle._rebase_onto_main(self.cfg, "ZIP-7873", self.wt)
        self.assertTrue(err); self.assertNotIn("human", err); self.assertEqual(aborted, [True])

    def test_a_question_cody_answered_is_not_asked_again(self):
        # ZIP-4275: the answered comment was never added to the ledger, so every resume replayed the
        # cached question and re-paused at the round cap (2026-10-17).
        tdir = self.bot_loop_ticket(bot_rounds=lifecycle.MAX_BOT_ROUNDS, owner_answered=["42"])
        self.comments = [{"kind": "inline", "id": 42, "path": "a.rb", "line": 1, "reply_to": None,
                          "sha": "s", "body": "filled or outlined?", "body_hash": "h42"}]
        step = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        self.assertEqual(state.load(tdir).state, "human-gate-1")
        self.assertEqual(json.loads((tdir / "bot-ledger.json").read_text())["42"], "h42")

    def test_a_capped_round_whose_agent_never_launched_is_reclaimed(self):
        tdir = self.cfg.ticket_dir("ZIP-7873")
        state.save(tdir, state.Ticket(key="ZIP-7873", state="bot-loop", previous="ready", critic_vendor="openai/gpt-6-astra"))
        (tdir / "ci.json").write_text(json.dumps({"pr": 1, "bot_rounds": lifecycle.MAX_BOT_ROUNDS, "actions": []}))
        failed = self.cfg.state_root / "botreview" / "ZIP-7873" / f"round-{lifecycle.MAX_BOT_ROUNDS}"
        failed.mkdir(parents=True)
        (failed / "stdout.log").write_text("")
        (failed / "stderr.log").write_text("gate: line 0: exec: pi: not found\n")
        self.comments = [{"kind": "inline", "id": 8, "path": "a.rb", "line": 1, "reply_to": None,
                          "sha": "s", "body": "x", "body_hash": "h8"}]
        _r, _steps, t = self.run_once(max_steps=1)
        self.assertEqual(t.state, "paused")
        self.assertIn("unavailable", t.reason)
        self.assertEqual(json.loads((tdir / "ci.json").read_text())["bot_rounds"], lifecycle.MAX_BOT_ROUNDS - 1)
        import providers
        self.assertFalse(providers.is_exhausted(self.cfg.state_root, "openai"), "a missing binary is not a quota limit")

    def test_capped_missing_credential_round_is_reclaimed_as_a_provider_transient(self):
        tdir = self.cfg.ticket_dir("ZIP-7873")
        state.save(tdir, state.Ticket(key="ZIP-7873", state="bot-loop", previous="ready",
                                      critic_vendor="openai-codex/gpt-6-astra"))
        (tdir / "ci.json").write_text(json.dumps({"pr": 1, "bot_rounds": lifecycle.MAX_BOT_ROUNDS, "actions": []}))
        failed = self.cfg.state_root / "botreview" / "ZIP-7873" / f"round-{lifecycle.MAX_BOT_ROUNDS}"
        failed.mkdir(parents=True)
        (failed / "stderr.log").write_text("No API key found for openai-codex.\n")
        self.comments = [{"kind": "inline", "id": 8, "path": "a.rb", "line": 1, "reply_to": None,
                          "sha": "s", "body": "x", "body_hash": "h8"}]

        _r, _steps, t = self.run_once(max_steps=1)

        self.assertEqual(t.state, "paused")
        self.assertIn("provider unavailable", t.reason)
        cis = json.loads((tdir / "ci.json").read_text())
        self.assertEqual(cis["bot_rounds"], lifecycle.MAX_BOT_ROUNDS - 1)
        self.assertFalse(failed.exists())
        self.assertTrue(list(failed.parent.glob(f"{failed.name}-provider-*")))
        import providers
        self.assertTrue(providers.is_exhausted(self.cfg.state_root, "openai-codex"))

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
        self.assertEqual(t.state, "paused"); self.assertIn("still violate the schema", t.reason)
        self.assertEqual(self.replies, [])                    # nothing promised publicly for a task that never queued
        import heal; self.assertNotEqual(heal.policy(t.reason)[0], "human")                            # bounded self-heal
        # Not the task-writer's bad-guards route: the rejected task never entered the manifest, so there was
        # nothing for it to repair and the same adjudication recurred until heal gave up (ZIP-4281).
        self.assertFalse((self.cfg.ticket_dir("ZIP-7873") / "bad-guards.md").exists())

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

    def test_restart_recovery_resolves_conflicts_the_same_way_the_normal_path_does(self):
        """Branch review 2026-09-23: recovery took `--theirs` for ANY config/locales/*.yml -- the
        replayed side, opposite to the hunk-by-hunk HEAD-wins rule, and broader than NORMALIZED."""
        import inspect
        src = inspect.getsource(lifecycle.recover_interrupted_git_state)
        self.assertNotIn('"--theirs"', src)
        self.assertIn("_resolve_normalized_conflict", src)
        self.assertIn("set(GENERATED) | set(NORMALIZED)", src)

    def test_an_unresolvable_interrupted_rebase_aborts_instead_of_guessing(self):
        gd = self.wt / ".git"; (gd / "rebase-merge").mkdir(parents=True, exist_ok=True)
        calls = []
        def sh(argv, timeout=120):
            calls.append(argv)
            if argv[:3] == ["git", "diff", "--name-only"]:
                return subprocess.CompletedProcess(argv, 0, "config/locales/am.yml\n", "")  # not NORMALIZED
            return subprocess.CompletedProcess(argv, 0, ".git" if "--git-dir" in argv else "", "")
        msg = lifecycle.recover_interrupted_git_state(self.wt, sh=sh)
        self.assertIn("aborted", msg)
        self.assertIn(["git", "rebase", "--abort"], calls)

    def test_interrupted_rebase_with_only_normalized_conflicts_is_finished(self):
        """en.yml is in NORMALIZED, so recovery resolves it with the same hunk-by-hunk resolver the
        normal rebase path uses, then continues."""
        gd = self.wt / ".git"; (gd / "rebase-merge").mkdir(parents=True, exist_ok=True)
        calls = []
        def sh(argv, timeout=120):
            calls.append(argv)
            if argv[:3] == ["git", "diff", "--name-only"]:
                return subprocess.CompletedProcess(argv, 0, "config/locales/en.yml\n", "")
            return subprocess.CompletedProcess(argv, 0, ".git" if "--git-dir" in argv else "", "")
        with patch.object(lifecycle, "_resolve_normalized_conflict", return_value=True) as resolver:
            msg = lifecycle.recover_interrupted_git_state(self.wt, sh=sh)
        resolver.assert_called_once()
        self.assertIn("finished an interrupted rebase", msg)
        self.assertNotIn(["git", "rebase", "--abort"], calls)

    def test_a_timed_out_git_probe_waits_a_tick_instead_of_crashing_the_runner(self):
        """ZIP-8697 (and ZIP-8257): `bash -lc 'git rev-parse --git-dir'` timed out after 30 s on a thrashing machine,
        the TimeoutExpired escaped step() and killed the supervised runner, and every restart hit the same first
        call again. An unanswered probe leaves the worktree's state UNKNOWN, not clean: no stage may run on it,
        the ticket is left as it was, and the next tick probes again."""
        def unresponsive(argv, cwd, timeout):
            if argv == ["git", "rev-parse", "--git-dir"]:
                raise subprocess.TimeoutExpired(["bash", "-lc", "git rev-parse --git-dir"], 30)
            return subprocess.CompletedProcess(argv, 0, "", "")
        with patch.object(lifecycle, "_sh", side_effect=unresponsive):
            r, steps, t = self.run_once()
        self.assertEqual(r.calls, [])                       # no task was dispatched into an unchecked worktree
        self.assertEqual([s.wait for s in steps], [True])
        self.assertIn("timed out after 30 seconds", steps[0].detail)
        self.assertEqual((t.state, t.reason), ("implement", ""))
        r, steps, t = self.run_once()                       # git answers again: the ticket carries on
        self.assertEqual(r.calls, ["001", "002"])

    def test_a_timeout_while_aborting_an_interrupted_rebase_is_retried_next_tick_not_skipped(self):
        """The case that matters: a restart left a rebase half-done and git stalls while it is being aborted.
        The tick must neither crash nor carry on as if the branch were fine; the abort is retried next tick."""
        rebase_dir = self.wt / ".git" / "rebase-merge"; rebase_dir.mkdir(parents=True)
        stalled = [True]
        def sh(argv, cwd, timeout):
            if argv[:3] == ["git", "diff", "--name-only"]:
                return subprocess.CompletedProcess(argv, 0, "app/models/thing.rb\n", "")     # a hand-written conflict
            if argv == ["git", "rebase", "--abort"]:
                if stalled[0]:
                    raise subprocess.TimeoutExpired(["bash", "-lc", "git rebase --abort"], 120)
                rebase_dir.rmdir()
            return subprocess.CompletedProcess(argv, 0, ".git" if argv == ["git", "rev-parse", "--git-dir"] else "", "")
        log = self.cfg.ticket_dir("ZIP-7873") / "git-recovery.jsonl"
        with patch.object(lifecycle, "_sh", side_effect=sh):
            r, steps, t = self.run_once()
            self.assertEqual(r.calls, [])
            self.assertTrue(steps[-1].wait)
            self.assertTrue(rebase_dir.exists())             # still interrupted, and nothing claims otherwise
            self.assertFalse(log.exists())
            stalled[0] = False
            r, steps, t = self.run_once()                    # git answers again: the abort is finished, then the ticket carries on
        self.assertFalse(rebase_dir.exists())
        self.assertIn("aborted an interrupted rebase", log.read_text())
        self.assertEqual(r.calls, ["001", "002"])

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

    def test_failed_rails_gate_becomes_a_bounded_code_fix_task(self):
        """ZIP-8698: re-preparing a deterministic assertion failure never changes the code that failed it."""
        tdir = self.cfg.ticket_dir("ZIP-7873")
        reason = "gates failed: rails test rc=1, rubocop rc=0"
        (tdir / "gates.out").write_text("expected to find css \"label[for='feedback_details']\"\n")
        tt = state.transition(state.load(tdir), "gates")
        tt = state.transition(tt, "paused", reason=reason); state.save(tdir, tt)
        step = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        self.assertEqual(state.load(tdir).state, "implement")
        self.assertIn("queued gate fix task", step.action)
        import contracts
        fixes = [task for task in contracts.load_tasks(self.wt / "planning" / "zip-7873" / "tasks.toml")
                 if task.slug.startswith("gate-fix-")]
        self.assertEqual(len(fixes), 1)
        fix = fixes[0]
        self.assertEqual(fix.allowed_files, ["app/a.rb", "app/b.rb"])
        self.assertTrue(fix.may_edit_tests)
        self.assertEqual(fix.verification_commands, ["bin/rails test test/views/components/zui/"])
        self.assertTrue(any("Do not weaken any test or lint rule" in invariant for invariant in fix.invariants))
        feedback = self.cfg.state_root / "attempts" / "ZIP-7873" / fix.id / "feedback.md"
        self.assertIn("label[for='feedback_details']", feedback.read_text())

    def test_lint_fix_task_permits_the_offending_test_file(self):
        """ZIP-4275: generated lint fixes for test paths must clear the worker allowlist gate."""
        tdir = self.cfg.ticket_dir("ZIP-7873")
        reason = ("gates failed: pre-commit lint rejected the commit: "
                  "test/system/components/zui/tophat_test.rb:12:3: C: Style/Foo: fix it.")
        tid = lifecycle._lint_fix_task(tdir, reason, self.wt / "planning" / "zip-7873" / "tasks.toml",
                                       cfg=self.cfg, key="ZIP-7873")
        import contracts, worktree
        fix = [t for t in contracts.load_tasks(self.wt / "planning" / "zip-7873" / "tasks.toml") if t.id == tid][0]
        self.assertEqual(worktree.check_allowlist(
            ["test/system/components/zui/tophat_test.rb"], fix,
            self.cfg.protected_paths, self.cfg.test_path_globs), [])

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

    def test_a_reek_hook_rejection_keeps_its_warnings_and_verifies_reek(self):
        """ZIP-4287: Reek reports a file heading and bracketed warning locations, not RuboCop locations."""
        tdir = self.cfg.ticket_dir("ZIP-7873"); tdir.mkdir(parents=True, exist_ok=True)
        source = "app/views/components/previews/zui/list_preview.rb"
        (tdir / "hook-reject.out").write_text(
            "bash: command not found: atuin\n"
            "┃ reek ❯\n\n"
            "Inspecting 1 file(s):\nS\n\n"
            f"{source} -- 5 warnings:\n"
            "  [47, 48, 49]:DuplicateMethodCall: ZUI::ListPreview#flush_list calls 'list.with_item' 3 times\n"
            "exit status 2\n"
            "┃ rubocop ❯\n\n"
            "1 file inspected, no offenses detected\n")
        tid = lifecycle._lint_fix_task(tdir, "gates failed: pre-commit lint rejected the commit",
                                       self.wt / "planning" / "zip-7873" / "tasks.toml")
        import contracts
        fix = [t for t in contracts.load_tasks(self.wt / "planning" / "zip-7873" / "tasks.toml") if t.id == tid][0]
        self.assertEqual(fix.allowed_files, [source])
        self.assertEqual(fix.verification_commands,
                         [f"bin/agent_run bundle exec reek --force-exclusion {source}"])
        self.assertTrue(any("DuplicateMethodCall" in invariant for invariant in fix.invariants))

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

    def test_bot_loop_waits_while_no_checks_are_registered(self):
        # Right after mark-ready the review workflows have not registered yet; an empty list is not "done".
        tdir = self.cfg.ticket_dir("ZIP-7873"); tdir.mkdir(parents=True, exist_ok=True)
        state.save(tdir, state.Ticket(key="ZIP-7873", state="bot-loop", previous="ready"))
        (tdir / "ci.json").write_text(json.dumps({"pr": 1, "bot_rounds": 1, "actions": []}))
        self.checks, self.comments = [], []
        r, steps, t = self.run_once(max_steps=1)
        self.assertTrue(steps[0].wait); self.assertEqual(t.state, "bot-loop")
        self.assertIn("still running", steps[0].action)

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

    def test_a_cached_adjudication_whose_replies_cannot_be_posted_is_rerun(self):
        """ZIP-7874: the reuse cache kept serving a reply citing the plan, which the reply lint rejects,
        so the ticket paused on the same error every cycle instead of re-adjudicating."""
        import botreview
        bad = [{"id": 1, "decision": "decline", "reply": "See planning/zip-7874/plan.md:713.", "task": None}]
        self.assertTrue(botreview.workflow_vocabulary(bad[0]["reply"]))
        good = [{"id": 1, "decision": "decline", "reply": "dropzone.rb:150 renders only the surface.", "task": None}]
        self.assertEqual(botreview.workflow_vocabulary(good[0]["reply"]), [])

    def test_a_posted_fix_reply_is_a_promise_even_when_no_task_is_queued(self):
        """ZIP-7874: replaying a cached adjudication re-posted four "I'll fix this" replies and queued
        no tasks, so the promise ledger stayed empty and the gate opened over four unimplemented fixes."""
        tdir = self.cfg.ticket_dir("ZIP-7873"); tdir.mkdir(parents=True, exist_ok=True)
        lifecycle._record_promise(self.cfg, "ZIP-7873", "4066921564")          # reply posted, no task
        unmet = lifecycle._unmet_review_promises(self.cfg, "ZIP-7873", self.wt)
        self.assertEqual(unmet, {"4066921564": "<no task queued>"})
        state.save(tdir, state.Ticket(key="ZIP-7873", state="bot-loop", previous="ready"))
        (tdir / "ci.json").write_text(json.dumps({"pr": 1, "bot_rounds": 1, "actions": []}))
        self.comments = []
        r, steps, t = self.run_once(max_steps=2)
        self.assertEqual(t.state, "paused")                                     # gate refuses
        self.assertIn("never implemented", t.reason)

    def test_a_promise_is_cleared_only_by_accepted_work(self):
        tdir = self.cfg.ticket_dir("ZIP-7873"); tdir.mkdir(parents=True, exist_ok=True)
        mf = self.wt / "planning" / "zip-7873" / "tasks.toml"
        lifecycle._record_promise(self.cfg, "ZIP-7873", "42")
        lifecycle._append_task(mf, {"id": "902", "slug": "review-fix-1", "size": "small", "summary": "s",
                                    "allowed_files": ["app/a.rb"], "acceptance": ["AC-1: x"],
                                    "verification_commands": ["bin/agent_run rubocop --cache false --force-exclusion app/a.rb"]})
        lifecycle._record_promise(self.cfg, "ZIP-7873", "42", "902")            # bound to the task
        with patch.object(lifecycle, "_task_accepted", return_value=False):
            self.assertEqual(lifecycle._unmet_review_promises(self.cfg, "ZIP-7873", self.wt), {"42": "902"})
        with patch.object(lifecycle, "_task_accepted", return_value=True):
            self.assertEqual(lifecycle._unmet_review_promises(self.cfg, "ZIP-7873", self.wt), {})

    def test_prettier_fix_task_names_only_the_files_prettier_warned_about(self):
        """ZIP-4281/901: prettier's non-TTY progress output glues checked file names together and the
        command line lists every staged file; the allowlist got a concatenated non-path and the gate
        exited 2 on every rung even after the real file was fixed."""
        tdir = self.cfg.ticket_dir("ZIP-7873"); tdir.mkdir(parents=True, exist_ok=True)
        a, b, c = ("app/assets/stylesheets/x/_nav_base.scss", "app/assets/stylesheets/x/_nav_variables.scss",
                   "app/views/components/zui/nav_tabs/nav_tabs.scss")
        (tdir / "hook-reject.out").write_text(
            f"$ node_modules/.bin/prettier --check {a} {b} {c}\nChecking formatting...\n"
            f"{a}[warn] {a}\n{b}{c}[warn] Code style issues found in the above file. Run Prettier with --write to fix.\n")
        tid = lifecycle._lint_fix_task(tdir, "gates failed: pre-commit lint rejected the commit", self.wt / "planning" / "zip-7873" / "tasks.toml")
        import contracts
        fix = [t for t in contracts.load_tasks(self.wt / "planning" / "zip-7873" / "tasks.toml") if t.id == tid][0]
        self.assertEqual(fix.allowed_files, [a])
        self.assertEqual(fix.verification_commands, [f"bin/agent_run yarn prettier --check {a}"])

    def test_a_jquery_hook_fix_task_explains_that_only_added_lines_are_checked(self):
        """ZIP-7874: a worker reformatted an EXISTING jQuery line and the hook counted it as new."""
        tdir = self.cfg.ticket_dir("ZIP-7873"); tdir.mkdir(parents=True, exist_ok=True)
        reason = ("gates failed: pre-commit lint rejected the commit: jquery-limit: "
                  "jQuery is depreciated. Use modern JavaScript. "
                  "app/assets/javascripts/application/uploaders/dropzone_uploader.js")
        tid = lifecycle._lint_fix_task(tdir, reason, self.wt / "planning" / "zip-7873" / "tasks.toml")
        import contracts
        fix = [t for t in contracts.load_tasks(self.wt / "planning" / "zip-7873" / "tasks.toml") if t.id == tid][0]
        self.assertTrue(any("ADDED lines" in i for i in fix.invariants))
        self.assertTrue(any("byte-for-byte" in i for i in fix.invariants))

    def test_an_identical_lint_fix_task_is_not_queued_twice(self):
        """ZIP-7874 spawned lint-fix 908, 909 and 910 for the same file in nine minutes."""
        tdir = self.cfg.ticket_dir("ZIP-7873"); tdir.mkdir(parents=True, exist_ok=True)
        mf = self.wt / "planning" / "zip-7873" / "tasks.toml"
        reason = "gates failed: pre-commit lint rejected the commit: app/assets/javascripts/x.js jQuery is depreciated"
        first = lifecycle._lint_fix_task(tdir, reason, mf, self.cfg, "ZIP-7873")
        with patch.object(lifecycle, "_task_accepted", return_value=False):
            second = lifecycle._lint_fix_task(tdir, reason, mf, self.cfg, "ZIP-7873")
        self.assertEqual(first, second)
        import contracts
        ids = [x.id for x in contracts.load_tasks(mf) if x.slug.startswith("lint-fix-")]
        self.assertEqual(len(ids), 1)

    def test_a_pending_lint_fix_runs_before_republishing_the_work_it_unblocks(self):
        """Republishing first re-hits the same hook and heals into another identical task."""
        import inspect
        src = inspect.getsource(lifecycle.step)
        self.assertIn("pending_lint_fix", src)
        self.assertIn("if pending_lint_fix:", src)

    def test_a_stale_approve_cannot_skip_the_opposite_vendor_review(self):
        """Branch review 2026-09-23: an `approve` older than the plan/manifest advanced straight to
        implement, bypassing the review the whole two-vendor design rests on."""
        import os, time as _t
        tdir = self.cfg.ticket_dir("ZIP-7873")
        state.save(tdir, state.Ticket(key="ZIP-7873", state="plan", previous="spinup"))
        pd = self.wt / "planning" / "zip-7873"
        pd.joinpath("plan-review.md").write_text("VERDICT: approve\n")
        old = _t.time() - 3 * 86400
        os.utime(pd / "plan-review.md", (old, old))
        pd.joinpath("plan.md").write_text("# a newly rewritten plan")          # newer than the verdict
        with patch.object(lifecycle.plan_mod, "plan_ticket", return_value={"result": "approved", "rounds": [],
                                                                           "author_model": "A", "critic_model": "C"}) as pl:
            self.run_once(max_steps=1)
        pl.assert_called_once()                                                 # re-reviewed, not waved through

    def test_unrunnable_guards_are_caught_at_plan_approval_not_by_spending_worker_rungs(self):
        """Branch review 2026-09-23: guards.precheck was documented as a plan-approval step and had
        no caller, so a guard that cannot run at all was discovered only after paying for attempts."""
        tdir = self.cfg.ticket_dir("ZIP-7873")
        state.save(tdir, state.Ticket(key="ZIP-7873", state="plan", previous="spinup"))
        pd = self.wt / "planning" / "zip-7873"
        pd.joinpath("plan.md").write_text("# plan")
        pd.joinpath("plan-review.md").write_text("VERDICT: approve\n")
        with patch.object(lifecycle.guards, "precheck", return_value=["task 001: guard is not runnable on the base tree"]):
            r, steps, t = self.run_once(max_steps=1)
        self.assertEqual(t.state, "paused")
        self.assertIn("unrunnable guard", t.reason)
        self.assertTrue((tdir / "bad-guards.md").exists())
        import heal; self.assertEqual(heal.policy(t.reason)[0], "repair")     # task-writer fixes it

    def test_a_clean_precheck_lets_the_approved_plan_through(self):
        tdir = self.cfg.ticket_dir("ZIP-7873")
        state.save(tdir, state.Ticket(key="ZIP-7873", state="plan", previous="spinup"))
        pd = self.wt / "planning" / "zip-7873"
        pd.joinpath("plan.md").write_text("# plan")
        pd.joinpath("plan-review.md").write_text("VERDICT: approve\n")
        with patch.object(lifecycle.guards, "precheck", return_value=[]):
            r, steps, t = self.run_once(max_steps=1)
        self.assertEqual(t.state, "implement")

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

    def test_state_slug_survives_a_tampered_task_toml(self):
        task = lifecycle.contracts.load_tasks(self.wt / "planning" / "zip-7873" / "tasks.toml")[0]
        tdir = self.cfg.ticket_dir("ZIP-7873"); tt = state.load(tdir)
        tt.attempts["ZIP-7873/001"] = [{"n": 1, "outcome": "accepted", "slug": task.slug,
                                          "rung": {"agent": "x", "tier": "cheap", "n": 1}}]
        state.save(tdir, tt)
        d = self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "1"; d.mkdir(parents=True)
        outside = self.cfg.state_root.parent / "other-task.toml"; outside.write_text('id = "001"\nslug = "other"\n')
        (d / "task.toml").symlink_to(outside)
        self.assertTrue(lifecycle._task_accepted(self.cfg, "ZIP-7873", task))

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

    def test_a_preview_exception_becomes_a_worker_fix_task_not_a_question(self):
        # ZIP-4287: the List preview raised `uninitialized constant ZUI::List::List` -- the ticket's own
        # code. A worker fixes that; asking Cody stalled the ticket (2026-10-19).
        import heal
        tdir = self.cfg.ticket_dir("ZIP-7873")
        reason = ("visual gate failed: https://admin.zip-7873.test/lookbook/preview/zui/list/playground -> 500 "
                  "(ActionView::Template::Error: uninitialized constant ZUI::List::List)")
        self.assertEqual(heal.policy(reason)[0], "preview-fix")
        tt = state.transition(state.load(tdir), "gates")
        tt = state.transition(tt, "paused", reason=reason); state.save(tdir, tt)
        (tdir / "preview-error.txt").write_text("ActionView::Template::Error: uninitialized constant ZUI::List::List\n"
                                                "app/views/components/zui/list/list.rb:60:in 'block in <class:List>'\n")
        with patch.object(lifecycle, "_sh", side_effect=lambda cmd, cwd, timeout: subprocess.CompletedProcess(
                cmd, 0, "app/views/components/zui/list/list.rb\ntest/views/components/zui/list/list_test.rb\n" if "origin/main...HEAD" in cmd else "", "")):
            step = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        t = state.load(tdir)
        self.assertEqual(t.state, "implement"); self.assertNotIn("Cody", step.action)
        manifest = (self.wt / "planning" / "zip-7873" / "tasks.toml").read_text()
        self.assertIn("preview-fix-", manifest)
        self.assertIn("uninitialized constant ZUI::List::List", manifest)
        self.assertIn("app/views/components/zui/list/list.rb", manifest)

    def test_the_visual_gate_reads_lookbooks_own_error_page(self):
        page = ('<!-- views/lookbook/errors/default.html.erb--><header class="px-8 py-6">\n'
                '<h2 class="text-xl font-bold text-red-700">ActionView::Template::Error</h2>\n</header>\n'
                '<div class="px-8"><pre class="whitespace-pre-wrap font-ui">uninitialized constant ZUI::List::List</pre></div>')
        self.assertEqual(lifecycle.screenshots.exception_summary(page),
                         "ActionView::Template::Error: uninitialized constant ZUI::List::List")

    def test_the_visual_gate_names_the_exception_a_preview_raised(self):
        page = ("<html><h1>ActionView::Template::Error</h1>"
                "<div class=\"exception-message\"><div class=\"message\">uninitialized constant ZUI::List::List</div></div></html>")
        self.assertEqual(lifecycle.screenshots.exception_summary(page),
                         "ActionView::Template::Error: uninitialized constant ZUI::List::List")

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

    def test_a_guard_repair_that_breaks_the_schema_gets_the_violations_and_a_second_pass(self):
        # ZIP-4288: the task-writer kept writing `ruby -e` guards the schema forbids, never told why,
        # and the ticket cycled on "guard repair left the manifest invalid" (2026-10-19).
        tdir = self.cfg.ticket_dir("ZIP-7873"); tt = state.load(tdir)
        tt = state.transition(tt, "paused", reason="guard failed on the BASE tree too: grep -q x app/a.rb"); state.save(tdir, tt)
        (tdir / "bad-guards.md").write_text("bad: grep -q x app/a.rb\n")
        prompts = []
        verdicts = iter([["tasks[0].verification_commands: inline interpreter code in 'ruby -e ...'"], []])
        with patch.object(lifecycle.plan_mod, "_run_agent", side_effect=lambda cfg, agent, prompt, *a, **k: prompts.append(prompt)), \
             patch.object(lifecycle.plan_mod, "validate_manifest", side_effect=lambda path: next(verdicts)):
            step = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        self.assertEqual(len(prompts), 2)
        self.assertIn("inline interpreter code", prompts[1])
        self.assertIn("bad verification guard repaired", step.action)

    def test_bad_guard_repair_from_plan_rechecks_plan_approval(self):
        tdir = self.cfg.ticket_dir("ZIP-7873")
        paused = state.Ticket(key="ZIP-7873", state="plan", previous="spinup", worktree=str(self.wt))
        paused = state.transition(paused, "paused", reason="guard failed on the BASE tree too (independent of the work): sort -c config/en.yml")
        state.save(tdir, paused)
        (tdir / "bad-guards.md").write_text("## task 001\n```\nsort -c config/en.yml\n```\n")
        def writer(cfg, agent, prompt, stage, wt, timeout, model=None):
            self.assertEqual(agent, "task-writer")
        with patch.object(lifecycle.plan_mod, "_run_agent", side_effect=writer):
            repaired = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        self.assertEqual(repaired.stage, "plan")
        self.assertEqual(state.load(tdir).state, "plan")
        (self.wt / "planning" / "zip-7873" / "plan-review.md").write_text("VERDICT: approve\n")
        with patch.object(lifecycle.guards, "precheck", return_value=["task 001: guard is still unrunnable"]):
            approval = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        self.assertTrue(approval.wait)
        self.assertEqual(state.load(tdir).state, "paused")
        self.assertIn("unrunnable guard", state.load(tdir).reason)

    def test_bad_guard_repair_fallthrough_keeps_plan_review_stage(self):
        tdir = self.cfg.ticket_dir("ZIP-7873")
        manifest = self.wt / "planning" / "zip-7873" / "tasks.toml"
        manifest.write_text("not valid TOML")
        paused = state.Ticket(key="ZIP-7873", state="plan-review", previous="plan", worktree=str(self.wt))
        paused = state.transition(paused, "paused", reason="guard repair left the manifest invalid: ['tasks.toml is not valid TOML']")
        state.save(tdir, paused)
        (tdir / "bad-guards.md").write_text("## task 001\n```\nsort -c config/en.yml\n```\n")
        def writer(cfg, agent, prompt, stage, wt, timeout, model=None):
            manifest.write_text(MANIFEST)
        with patch.object(lifecycle.plan_mod, "_run_agent", side_effect=writer):
            repaired = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        self.assertEqual(repaired.stage, "plan-review")
        self.assertEqual(state.load(tdir).state, "plan-review")

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
        with patch.object(lifecycle.ci, "behind_base", return_value=2), patch.object(lifecycle, "_sh", side_effect=sh), \
             patch.object(lifecycle.plan_mod, "_run_agent") as worker:
            r, steps, t = self.run_once(outcomes=("rejected",), max_steps=1)
        worker.assert_called_once()                                      # a worker tries first
        self.assertEqual(t.state, "paused"); self.assertNotIn("human", t.reason); self.assertEqual(r.calls, [])

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

    def test_spent_heal_budget_routes_to_structured_recovery(self):
        import heal
        tdir = self.cfg.ticket_dir("ZIP-7873"); tt = state.load(tdir)
        for _ in range(3): heal.record(tdir, "gate-fix", "gates failed: rails test rc=1", "gates")
        tt = state.transition(tt, "gates"); tt = state.transition(tt, "paused", reason="gates failed: rails test rc=1"); state.save(tdir, tt)
        r, steps, t = self.run_once()
        self.assertTrue(steps[0].wait); self.assertIn("waiting for automatic recovery", steps[0].action)
        self.assertIsNotNone(state.load(tdir).recovery["active"])
        self.assertEqual(r.calls, [])
        self.assertEqual(self.notified, [])

    def test_rebase_conflict_routes_to_structured_recovery(self):
        tdir = self.cfg.ticket_dir("ZIP-7873"); tt = state.load(tdir)
        tt = state.transition(tt, "paused", reason="rebase onto origin/main conflicted before task 003; needs a human"); state.save(tdir, tt)
        r, steps, t = self.run_once()
        self.assertTrue(steps[0].wait); self.assertIn("automatic recovery", steps[0].action)
        self.assertIsNotNone(state.load(tdir).recovery["active"])
        self.assertEqual(r.calls, [])

    def test_blocked_failures_and_bare_owner_claims_are_recovery_not_decisions(self):
        reasons = [
            "missing file app/views/components/x.rb",
            "dependency complaint: task 002 needs 001",
            "verification failed: rails test rc=1",
            "rebase conflict in config/locales/en.yml",
            "provider quota exhausted until tomorrow",
            "repair budget exhausted",
            "owner",
        ]
        for reason in reasons:
            with self.subTest(reason):
                tdir = self.cfg.ticket_dir("ZIP-7873")
                tt = state.Ticket(key="ZIP-7873", state="blocked", previous="implement", reason=reason, worktree=str(self.wt))
                state.save(tdir, tt)
                step = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
                current = state.load(tdir)
                self.assertTrue(step.wait)
                self.assertIsNotNone(current.recovery["active"])
                self.assertNotEqual(current.state, "decision-required")

    def test_valid_staged_product_ambiguity_enters_decision_required_once(self):
        import decision
        tdir = self.cfg.ticket_dir("ZIP-7873")
        tt = state.Ticket(key="ZIP-7873", state="blocked", previous="implement", reason="product decision candidate", worktree=str(self.wt))
        state.save(tdir, tt)
        payload = {
            "schema_version": 1,
            "summary": "Choose the public component API.",
            "sources": ["Jira AC", "existing API"],
            "options": [
                {"id": "records", "label": "Records", "consequences": "Callers can mutate records."},
                {"id": "values", "label": "Values", "consequences": "Callers use explicit commands."},
            ],
            "recommendation": "values",
            "recommendation_reason": "Explicit commands preserve the shipped API.",
            "why_unresolved": "The ticket contradicts the documented public API.",
            "question": "Which API should be public?",
            "checkpoint": {"state": "implement", "head_sha": "abc"},
            "resume": {"kind": "resume-stage", "state": "implement"},
        }
        (tdir / "decision-candidate.json").write_text(json.dumps(payload))
        first = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        second = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        current = state.load(tdir)
        self.assertEqual(current.state, "decision-required")
        self.assertEqual(first.action, "product decision required")
        self.assertTrue(second.wait)
        self.assertEqual(len([title for title in self.notified if "decision" in title]), 2)  # fixture records calls; real notifier dedupes

    def test_accepted_but_uncommitted_task_is_republished_before_new_work(self):
        import contracts as c, attempt as attempt_mod
        tasks = c.load_tasks(self.wt / "planning" / "zip-7873" / "tasks.toml")
        tdir = self.cfg.ticket_dir("ZIP-7873"); tt = state.load(tdir)
        tt.attempts["ZIP-7873/001"] = [{"n": 1, "outcome": "accepted", "rung": {"agent": "x", "tier": "cheap", "n": 1}}]; state.save(tdir, tt)
        d = self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "1"; d.mkdir(parents=True); (d / "task.toml").write_text(attempt_mod.task_toml(tasks[0]))
        (self.wt / "app").mkdir(exist_ok=True); (self.wt / "app" / "a.rb").write_text("accepted but never committed\n")
        published = []
        with patch("runner.publish_accepted", side_effect=lambda cfg, key, wt, tk, ensure_pr=True: published.append(tk.id)), \
             patch("runner._uncommitted_allowlisted", side_effect=lambda wt, tk, harness_globs=(): ["app/a.rb"] if tk.id == "001" else []):
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

    def test_contract_complaint_from_implement_replans_without_an_illegal_transition(self):
        """ZIP-4285: a contract complaint pauses implement, but repair must re-enter plan."""
        tdir = self.cfg.ticket_dir("ZIP-7873")
        tt = state.load(tdir)
        tt = state.transition(tt, "paused", reason="contract complaint (dependency): task 001 must be repaired")
        state.save(tdir, tt)
        step = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        self.assertEqual(step.action, "self-heal: repair")
        self.assertEqual(state.load(tdir).state, "plan")

    def test_contract_complaint_with_evidence_replans_instead_of_repairing_guards(self):
        """ZIP-4285: a complaint file is evidence about a task contract, not a failed guard."""
        tdir = self.cfg.ticket_dir("ZIP-7873")
        review = self.wt / "planning" / "zip-7873" / "plan-review.md"
        (review.with_name("plan.md")).write_text("# plan\n")
        tt = state.load(tdir)
        tt = state.transition(tt, "paused", reason="contract complaint (dependency): restore task 001 before task 002")
        state.save(tdir, tt)
        evidence = tdir / "bad-guards.md"
        evidence.write_text("## task 002 -- worker contract complaint (dependency)\n")
        with patch.object(lifecycle.plan_mod, "_run_agent") as writer:
            step = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        self.assertEqual(step.action, "self-heal: repair")
        self.assertEqual(state.load(tdir).state, "plan")
        writer.assert_not_called()
        self.assertIn("VERDICT: revise", review.read_text())
        self.assertIn(str(evidence), review.read_text())
        with patch.object(lifecycle.plan_mod, "repair_plan", return_value={"result": "approved", "rounds": [], "author_model": "A", "critic_model": "C"}) as repair_plan:
            step = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        repair_plan.assert_called_once()
        self.assertEqual(step.action, "approved")

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

    def test_planning_ceiling_asks_cody_and_never_becomes_a_runner_defect(self):
        """ZIP-8882: the ceiling says the plan needs an owner ruling, but the supervisor's next tick filed that
        pause as a runner defect with a replan contract: a repair agent was spent on it, `resume` refused
        Cody's ruling, and the landed repair would only have re-planned into the same ceiling."""
        import repair
        tdir = self.cfg.ticket_dir("ZIP-7873"); state.save(tdir, state.Ticket(key="ZIP-7873", state="plan", previous="spinup", worktree=str(self.wt)))
        (self.wt / "planning" / "zip-7873" / "plan.md").write_text("plan")
        (self.wt / "planning" / "zip-7873" / "plan-review.md").write_text("VERDICT: revise\nBLOCKERS:\n1. x")
        with patch.object(lifecycle, "_task_accepted", return_value=False), \
             patch.object(lifecycle.ledger, "collect", return_value=[{"ticket": "ZIP-7873", "billed_usd": 30.3}]):
            lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)     # hits the ceiling
            self.assertIn("planning ceiling", state.load(tdir).reason)
            step = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)   # the next tick
        self.assertTrue(step.wait)
        self.assertEqual(state.load(tdir).state, "paused")
        self.assertIsNone(state.load(tdir).recovery["active"])
        self.assertEqual(repair.open_defects(self.cfg), [])
        self.assertTrue(any("needs Cody" in title for title in self.notified), self.notified)

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

    def test_an_invalid_manifest_left_by_a_failed_plan_is_replanned_not_a_crash(self):
        """2026-10-05: a blocked plan left zero tasks, a task-writer left a rejected command; the plan
        stage raised ManifestError on every supervisor restart for hours instead of re-planning."""
        manifests = {"empty": "",
                     "rejected command": '[[tasks]]\nid = "001"\nslug = "s"\nsummary = "x"\nallowed_files = ["app/a.rb"]\n'
                                         'verification_commands = ["curl http://x"]\nacceptance = ["AC-1"]\n'}
        for name, body in manifests.items():
            with self.subTest(name):
                tdir = self.cfg.ticket_dir("ZIP-7873"); state.save(tdir, state.Ticket(key="ZIP-7873", state="plan", previous="paused"))
                pd = self.wt / "planning" / "zip-7873"; (pd / "plan.md").write_text("plan"); (pd / "tasks.toml").write_text(body)
                (pd / "plan-review.md").unlink(missing_ok=True)
                with patch.object(lifecycle.plan_mod, "plan_ticket", return_value={"result": "approved", "rounds": [], "author_model": "A", "critic_model": "C"}) as pl:
                    lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
                pl.assert_called_once(); self.assertEqual(state.load(tdir).state, "implement")

    def test_planning_ceiling_does_not_apply_once_work_is_accepted(self):
        tdir = self.cfg.ticket_dir("ZIP-7873"); state.save(tdir, state.Ticket(key="ZIP-7873", state="plan", previous="spinup"))
        (self.wt / "planning" / "zip-7873" / "plan.md").write_text("plan")
        (self.wt / "planning" / "zip-7873" / "plan-review.md").write_text("VERDICT: revise\nBLOCKERS:\n1. x")
        with patch.object(lifecycle, "_task_accepted", return_value=True), \
             patch.object(lifecycle.ledger, "collect", return_value=[{"ticket": "ZIP-7873", "billed_usd": 99.0}]), \
             patch.object(lifecycle.plan_mod, "repair_plan", return_value={"result": "approved", "rounds": [], "author_model": "A", "critic_model": "C"}):
            step = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        self.assertNotIn("ceiling", step.action); self.assertNotIn("ceiling", state.load(tdir).reason)

    def test_cohort_mode_does_not_dispatch_a_nonmember_ticket(self):
        import cohort
        cohort.initialize(self.cfg)
        runner = FakeRunner(self.cfg, ("accepted",))
        step = lifecycle.step(self.cfg, runner, None, "ZIP-7873", self.wt, hopper_index=2)
        self.assertTrue(step.wait)
        self.assertEqual(step.action, "not admitted to cohort")
        self.assertEqual(runner.calls, [])

    def test_active_recovery_prevents_self_heal_and_worker_dispatch(self):
        import recovery
        tdir = self.cfg.ticket_dir("ZIP-7873")
        ticket = state.load(tdir)
        ticket = state.transition(ticket, "paused", reason="heavy lane held")
        state.save(tdir, ticket)
        contract = recovery.new_contract("ZIP-7873", {"state": "implement"}, {"kind": "resume-stage", "state": "implement"}, ("defect-1",))
        recovery.activate(self.cfg, "ZIP-7873", contract)
        linked = self.cfg.state_root / "defects" / "open" / "defect-1.json"
        linked.parent.mkdir(parents=True, exist_ok=True)
        linked.write_text(json.dumps({"id": "defect-1", "affected_tickets": [{"ticket": "ZIP-7873", "recovery_id": contract["id"]}]}))
        with patch("heal.decide", side_effect=AssertionError("recovery wait must precede self-heal")):
            _, steps, ticket = self.run_once()
        self.assertTrue(steps[0].wait)
        self.assertEqual(ticket.state, "paused")

    def test_orphaned_recovery_without_any_defect_record_is_relinked(self):
        import recovery, repair
        tdir = self.cfg.ticket_dir("ZIP-7873")
        state.save(tdir, state.Ticket(key="ZIP-7873", state="blocked", previous="implement", reason="owner", worktree=str(self.wt)))
        orphan = recovery.new_contract("ZIP-7873", {"state": "implement"}, {"kind": "resume-stage", "state": "implement"}, ("missing-defect",))
        recovery.activate(self.cfg, "ZIP-7873", orphan)
        step = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        active = state.load(tdir).recovery["active"]
        self.assertTrue(step.wait)
        self.assertNotEqual(active["id"], orphan["id"])
        opened = repair.open_defects(self.cfg)
        self.assertEqual(active["defect_ids"], [opened[0]["id"]])
        self.assertEqual(opened[0]["affected_tickets"][0]["recovery_id"], active["id"])

    def test_owner_questions_ask_cody_and_never_become_runner_defects(self):
        import repair
        for reason, stage in (("reviewer questions need Cody: comment 6: is the slot API public?", "paused"),
                              ("plan: critic: tier-4 question for the owner (see plan-review.md)", "blocked")):
            with self.subTest(reason=reason):
                self.notified.clear()
                tdir = self.cfg.ticket_dir("ZIP-7873")
                state.save(tdir, state.Ticket(key="ZIP-7873", state=stage, previous="plan", reason=reason, worktree=str(self.wt)))
                step = lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
                self.assertTrue(step.wait)
                self.assertIsNone(state.load(tdir).recovery["active"])
                self.assertEqual(repair.open_defects(self.cfg), [])
                self.assertTrue(any("needs Cody" in title for title in self.notified), self.notified)

    def test_exhausted_ladder_block_gets_an_exact_retry_task_contract(self):
        tdir = self.cfg.ticket_dir("ZIP-7873")
        state.save(tdir, state.Ticket(key="ZIP-7873", state="blocked", previous="implement", reason="task 002: ladder exhausted", worktree=str(self.wt)))
        lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        active = state.load(tdir).recovery["active"]
        self.assertEqual(active["resume"], {"kind": "retry-task", "state": "implement", "task_id": "002", "task_slug": "b"})

    def attempt_entries(self, *outcomes):
        """state.json history of task 001 (slug a): one row per outcome, each on the next rung; `reset` is a recovery's operator-reset."""
        import ladder
        rungs = [("cloud-worker", "cheap", 1), ("cloud-worker", "cheap", 2), ("premium-worker", "premium", 1), ("premium-worker", "premium", 2)]
        return [{"outcome": ladder.OPERATOR_RESET, "note": "automatic recovery x"} if outcome == "reset" else
                {"n": n, "rung": dict(zip(("agent", "tier", "n"), rungs[(n - 1) % 4])), "outcome": outcome, "arm": "cloud", "slug": "a"}
                for n, outcome in enumerate(outcomes, 1)]

    def test_a_ladder_spent_by_its_final_attempt_is_retried_when_the_repair_resolves(self):
        # ZIP-8697/8698/8699 (defect 2c968553b258): the yarn.lock rejections were fixed in 525b1c2, but the fourth one had spent task
        # 001's ladder and blocked each ticket with ITS OWN reason, never `task 001: ladder exhausted`. Their resume-stage contracts
        # re-entered implement, which re-blocked at once without running a worker and filed a second defect; the task was only
        # retried after that one resolved too.
        import ladder, recovery, repair, runner
        tdir = self.cfg.ticket_dir("ZIP-7873")
        reason = "verification introduced forbidden change: yarn.lock: not in allowed_files for task 001"
        history = self.attempt_entries("rejected", "rejected", "blocked", "rejected", "environment", "rejected", "interrupted")   # ZIP-8697's
        state.save(tdir, state.Ticket(key="ZIP-7873", state="blocked", previous="implement", reason=reason, worktree=str(self.wt),
                                      attempts={"ZIP-7873/001": history}))
        lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        self.assertEqual(state.load(tdir).recovery["active"]["resume"], {"kind": "retry-task", "state": "implement", "task_id": "001", "task_slug": "a"})
        defect = repair.open_defects(self.cfg)[0]
        self.assertEqual(defect["summary"], reason)                  # the repair agent still sees the real cause
        recovery.emit_event(self.cfg, defect, "not-needed")
        recovery.consume_events(self.cfg, retry_task=runner.retry_task, ensure_supervisor=lambda *args: None)
        resumed = state.load(tdir)
        self.assertEqual(resumed.state, "implement")
        self.assertEqual(ladder.since_reset(resumed.attempts["ZIP-7873/001"]), [])     # a rung is left: implement runs a worker

    def test_a_block_with_rungs_left_keeps_its_resume_stage_contract(self):
        for label, entries in (("two rejections, then a premium owner block", self.attempt_entries("rejected", "rejected", "blocked")),
                               ("a ladder an earlier recovery already reset", self.attempt_entries("rejected", "rejected", "rejected", "rejected", "reset", "rejected", "blocked")),
                               ("legacy rows that never recorded a rung", [{"n": n, "outcome": "rejected", "slug": "a"} for n in range(1, 5)])):
            with self.subTest(label):
                tdir = self.cfg.ticket_dir("ZIP-7873")
                state.save(tdir, state.Ticket(key="ZIP-7873", state="blocked", previous="implement", reason="owner", worktree=str(self.wt),
                                              attempts={"ZIP-7873/001": entries}))
                lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
                self.assertEqual(state.load(tdir).recovery["active"]["resume"], {"kind": "resume-stage", "state": "implement"})

    def accept_task_001(self, *observed_files):
        """Task 001 accepted in attempt 1. The tree that attempt observed held `observed_files`; the working copy has none of them."""
        import attempt as attempt_mod, contracts
        git = lambda *a: subprocess.run(["git", "-C", str(self.wt), *a], capture_output=True, text=True, check=True).stdout.strip()
        git("init", "-q")
        for path in observed_files:
            (self.wt / path).parent.mkdir(parents=True, exist_ok=True); (self.wt / path).write_text("work")
            git("add", "--", path)
        tree = git("write-tree")
        for path in observed_files:
            (self.wt / path).unlink()
        task = contracts.load_tasks(self.wt / "planning" / "zip-7873" / "tasks.toml")[0]
        d = self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "1"; d.mkdir(parents=True, exist_ok=True)
        (d / "task.toml").write_text(attempt_mod.task_toml(task))
        (d / "attempt.json").write_text(json.dumps({"outcome": "accepted", "observed_tree": tree}))

    def block_on_task_002(self, tdir):
        """Blocked `owner` after task 002's cheap rungs were spent and its premium rung blocked (ZIP-4285's shape)."""
        state.save(tdir, state.Ticket(key="ZIP-7873", state="blocked", previous="implement", reason="owner", worktree=str(self.wt),
                                      attempts={"ZIP-7873/001": self.attempt_entries("accepted"),
                                                "ZIP-7873/002": [{**e, "slug": "b"} for e in self.attempt_entries("rejected", "rejected", "blocked")]}))

    def test_accepted_output_missing_from_the_worktree_is_restored_before_the_dependent_task_runs_again(self):
        # ZIP-4285 (defect 2ed39f7f3799): task 001 was accepted but its output was never committed and later vanished from the worktree.
        # Task 002 then ran over a tree without it: every worker reported the missing prerequisite and the premium report was `blocked/owner`,
        # so the ticket blocked with the bare reason `owner`. Its resume-stage contract resumes implement, which dispatches task 002's
        # premium rung over the same tree again, and the repair agent sees nothing but `owner`.
        import contracts, recovery, repair, runner
        tdir = self.cfg.ticket_dir("ZIP-7873")
        self.accept_task_001("app/a.rb")
        self.block_on_task_002(tdir)
        lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        self.assertEqual(state.load(tdir).recovery["active"]["resume"], {"kind": "retry-task", "state": "implement", "task_id": "001", "task_slug": "a"})
        defect = repair.open_defects(self.cfg)[0]
        self.assertEqual(defect["summary"], "owner")
        self.assertIn("app/a.rb", defect["evidence"])                 # the repair agent sees the real cause, not just `owner`
        recovery.emit_event(self.cfg, defect, "not-needed")
        recovery.consume_events(self.cfg, retry_task=runner.retry_task, ensure_supervisor=lambda *args: None)
        tasks = contracts.load_tasks(self.wt / "planning" / "zip-7873" / "tasks.toml")
        self.assertEqual(state.load(tdir).state, "implement")
        self.assertEqual(lifecycle._next_task(self.cfg, "ZIP-7873", tasks).id, "001")      # the prerequisite runs again, not task 002

    def test_a_lost_prerequisite_is_retried_before_the_ladder_that_ran_out_over_it(self):
        # Retrying the dependent task first would spend a fresh ladder over the same missing output and block again.
        spent = [{**e, "slug": "b"} for e in self.attempt_entries("rejected", "rejected", "rejected", "rejected")]
        for reason in ("task 002: ladder exhausted", "owner"):
            with self.subTest(reason):
                tdir = self.cfg.ticket_dir("ZIP-7873")
                self.accept_task_001("app/a.rb")
                state.save(tdir, state.Ticket(key="ZIP-7873", state="blocked", previous="implement", reason=reason, worktree=str(self.wt),
                                              attempts={"ZIP-7873/001": self.attempt_entries("accepted"), "ZIP-7873/002": spent}))
                lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
                self.assertEqual(state.load(tdir).recovery["active"]["resume"], {"kind": "retry-task", "state": "implement", "task_id": "001", "task_slug": "a"})

    def test_accepted_output_still_in_the_worktree_keeps_the_resume_stage_contract(self):
        tdir = self.cfg.ticket_dir("ZIP-7873")
        self.accept_task_001("app/a.rb")
        (self.wt / "app" / "a.rb").write_text("work")
        self.block_on_task_002(tdir)
        lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        self.assertEqual(state.load(tdir).recovery["active"]["resume"], {"kind": "resume-stage", "state": "implement"})

    def test_a_task_that_deleted_its_file_is_not_treated_as_lost_output(self):
        tdir = self.cfg.ticket_dir("ZIP-7873")
        self.accept_task_001()                       # the tree it observed has no app/a.rb: the task removed it
        self.block_on_task_002(tdir)
        lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        self.assertEqual(state.load(tdir).recovery["active"]["resume"], {"kind": "resume-stage", "state": "implement"})

    def test_generic_blocks_on_different_tickets_get_separate_defects(self):
        import repair
        for key in ("ZIP-7873", "ZIP-7874"):
            state.save(self.cfg.ticket_dir(key), state.Ticket(key=key, state="blocked", previous="implement", reason="owner", worktree=str(self.wt)))
            lifecycle._decision_or_recovery(self.cfg, state.load(self.cfg.ticket_dir(key)), self.wt)
        self.assertEqual(len(repair.open_defects(self.cfg)), 2)

    def test_defect_recording_failure_leaves_no_orphaned_contract(self):
        tdir = self.cfg.ticket_dir("ZIP-7873")
        state.save(tdir, state.Ticket(key="ZIP-7873", state="blocked", previous="implement", reason="owner", worktree=str(self.wt)))
        with patch("repair.record_defect", side_effect=OSError("disk full")), self.assertRaises(OSError):
            lifecycle.step(self.cfg, FakeRunner(self.cfg, []), None, "ZIP-7873", self.wt, hopper_index=2)
        self.assertIsNone(state.load(tdir).recovery["active"])

    def test_operator_reset_after_acceptance_reopens_the_task(self):
        import contracts as c, attempt as attempt_mod, ladder
        tasks = c.load_tasks(self.wt / "planning" / "zip-7873" / "tasks.toml")
        tdir = self.cfg.ticket_dir("ZIP-7873"); tt = state.load(tdir)
        tt.attempts["ZIP-7873/001"] = [{"n": 1, "outcome": "accepted", "rung": {"agent": "x", "tier": "cheap", "n": 1}}]
        state.save(tdir, tt)
        d = self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "1"; d.mkdir(parents=True)
        (d / "task.toml").write_text(attempt_mod.task_toml(tasks[0]))
        self.assertTrue(lifecycle._task_accepted(self.cfg, "ZIP-7873", tasks[0]))
        tt = state.load(tdir); tt.attempts["ZIP-7873/001"].append({"outcome": ladder.OPERATOR_RESET, "note": "automatic recovery x"}); state.save(tdir, tt)
        self.assertFalse(lifecycle._task_accepted(self.cfg, "ZIP-7873", tasks[0]))

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

