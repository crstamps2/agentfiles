import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import config
import drip
import state

BOARD = [
    {"key": "ZIP-4281", "summary": "Nav Tabs", "status": "Selected for Work", "assignee": None},
    {"key": "ZIP-4293", "summary": "Section", "status": "Selected for Work", "assignee": None},
    {"key": "ZIP-7872", "summary": "Nav Link", "status": "In Progress", "assignee": "cody@zipline.inc"},
    {"key": "ZIP-7874", "summary": "Dropzone", "status": "Selected for Work", "assignee": None},
    {"key": "ZIP-7875", "summary": "Stepper", "status": "In Progress", "assignee": "cody@zipline.inc"},
    {"key": "ZIP-8025", "summary": "Adopt forms", "status": "In Progress", "assignee": "martin@zipline.inc"},
    {"key": "ZIP-8079", "summary": "Migrate wells", "status": "Triage", "assignee": None},
    {"key": "ZIP-9001", "summary": "Someone else's ready ticket", "status": "Selected for Work", "assignee": "ann@zipline.inc"},
]


class DripFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = pathlib.Path(self.tmp.name)
        toml = (pathlib.Path(__file__).resolve().parent.parent / "hopper.toml").read_text()
        toml = toml.replace('state_root = "~/.local/state/agent-loop"', f'state_root = "{root}/state"').replace('pi_agents_dir = "~/.pi/agent/agents"', f'pi_agents_dir = "{root}"')
        (root / "hopper.toml").write_text(toml); self.cfg = config.load(root / "hopper.toml"); self.cfg.ensure_dirs()
        self.wts = root / "wts"; (self.wts / "zip-7875").mkdir(parents=True)          # 7875 in flight; 7872 at the gate
        state.save(self.cfg.ticket_dir("ZIP-7875"), state.Ticket(key="ZIP-7875", state="implement"))
        state.save(self.cfg.ticket_dir("ZIP-7872"), state.Ticket(key="ZIP-7872", state="human-gate-1"))
        self.patches = [patch.object(drip, "WORKTREES", self.wts), patch.object(drip, "board", return_value=list(BOARD)),
                        patch.object(drip, "done_in_jira", return_value=set())]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in self.patches: p.stop()
        self.tmp.cleanup()


class DripTests(DripFixture):
    def test_candidates_follow_board_order_with_the_rules(self):
        keys = [r["key"] for r in drip.candidates(self.cfg, log=lambda m: None)]
        # 4281 dep on 7872 not Done; 4293 excluded; 7872 gated; 7875 in flight; 8025 In Progress (Martin); 8079 Triage
        self.assertEqual(keys, ["ZIP-7874", "ZIP-9001"])

    def test_others_ready_tickets_are_allowed_unless_configured(self):
        self.cfg.drip["skip_assigned_to_others"] = True
        self.assertEqual([r["key"] for r in drip.candidates(self.cfg, log=lambda m: None)], ["ZIP-7874"])

    def test_dep_becomes_eligible_once_done_in_jira(self):
        with patch.object(drip, "done_in_jira", return_value={"ZIP-7872"}):
            keys = [r["key"] for r in drip.candidates(self.cfg, log=lambda m: None)]
        self.assertEqual(keys[0], "ZIP-4281")                                        # top of the board once unblocked

    def test_tick_fills_free_slots_only(self):
        self.cfg.drip["max_in_flight"] = 2
        self.assertEqual(drip.tick(self.cfg, log=lambda m: None, dry_run=True), ["ZIP-7874"])   # 1 in flight -> 1 free
        self.cfg.drip["max_in_flight"] = 1
        self.assertEqual(drip.tick(self.cfg, log=lambda m: None, dry_run=True), [])

    def test_an_override_cannot_raise_the_cap_past_seven(self):
        self.cfg.drip["max_in_flight"] = 50
        self.assertEqual(drip.tick(self.cfg, log=lambda m: None, dry_run=True), ["ZIP-7874", "ZIP-9001"])
        self.assertEqual(drip.limit(self.cfg), drip.HARD_MAX_IN_FLIGHT)

    def test_tick_waits_for_shared_services_before_spinup(self):
        msgs = []
        not_ready = drip.subprocess.CompletedProcess(["bin/dev-services"], 1, "", "elasticsearch not ready")
        import supervise
        with patch.object(drip.subprocess, "run", return_value=not_ready), \
             patch.object(drip, "spinup_with_panes", return_value=self.wts / "zip-7874") as spinup, \
             patch.object(supervise, "start") as start_supervisor:
            started = drip.tick(self.cfg, log=msgs.append)
        self.assertEqual(started, [])
        spinup.assert_not_called()
        start_supervisor.assert_not_called()
        self.assertTrue(any("shared services are not ready" in m for m in msgs), msgs)




class DripSpinupFailureTests(DripFixture):
    """2026-10-05: a setup failure left two tickets In Progress in Jira with no loop state and no
    supervisor, and the drip could not retry them without opening a duplicate workspace."""

    def setUp(self):
        super().setUp()
        import supervise
        self.cc, self.sh = MagicMock(), MagicMock()
        self.cc.worktree_name_for_ticket.side_effect = lambda key: key.lower()
        self.cc.workspace_ref_for_name.return_value = "workspace:19"
        self.sh.fetch_ticket.side_effect = lambda key: {"key": key, "issue_type": "Task", "title": "t", "status": "Selected for Work"}
        self.run = MagicMock(return_value=drip.subprocess.CompletedProcess([], 0, "", ""))
        self.rerun = MagicMock(return_value=(True, "setup log"))
        for p in (patch.object(drip, "_spinup_modules", return_value=(self.cc, self.sh)),
                  patch.object(drip, "shared_services_ready", return_value=True),
                  patch.object(drip.subprocess, "run", self.run),
                  patch.object(drip, "rerun_setup", self.rerun)):
            p.start(); self.addCleanup(p.stop)
        self.start = patch.object(supervise, "start", return_value="label").start(); self.addCleanup(patch.stopall)
        self.cfg.drip["max_in_flight"] = 2                                            # 7875 in flight -> one free slot

    def worktree(self, key):
        wt = self.wts / key.lower(); wt.mkdir(parents=True, exist_ok=True); return wt

    def jira_assignments(self):
        return [c for c in self.run.call_args_list if c.args and c.args[0][:3] == ["acli", "jira", "workitem"]]

    def failed(self, key, attempts):
        state.save(self.cfg.ticket_dir(key), state.Ticket(key=key, state="blocked", worktree=str(self.worktree(key)),
                   spinup_failures=attempts, reason=f"spinup failed (setup-failed), attempt {attempts}/3"))

    def test_a_failed_setup_leaves_jira_alone_and_is_recorded_for_retry(self):
        self.cfg.drip["max_in_flight"] = 3
        self.cc.run_chain.return_value = {"status": "setup-failed", "workspace": "workspace:19", "worktree": str(self.worktree("ZIP-7874"))}
        self.assertEqual(drip.tick(self.cfg, log=lambda m: None), [])
        self.sh.transition_to_in_progress.assert_not_called(); self.assertEqual(self.jira_assignments(), [])
        self.start.assert_not_called()
        self.assertEqual(self.cc.run_chain.call_count, 1)                           # the pass stops after a failure
        t = state.load(self.cfg.ticket_dir("ZIP-7874"))
        self.assertEqual((t.state, t.spinup_failures, t.worktree), ("blocked", 1, str(self.wts / "zip-7874")))
        self.assertEqual(t.attempts, {})                                             # runner-owned: per-task attempt lists
        self.assertTrue(t.reason.startswith("spinup failed"), t.reason)

    def test_jira_is_claimed_after_a_successful_setup(self):
        self.cc.run_chain.return_value = {"status": "ok", "workspace": "workspace:19", "worktree": str(self.worktree("ZIP-7874"))}
        self.assertEqual(drip.tick(self.cfg, log=lambda m: None), ["ZIP-7874"])
        self.sh.transition_to_in_progress.assert_called_once_with("ZIP-7874", "Selected for Work")
        self.assertEqual(len(self.jira_assignments()), 1)
        self.start.assert_called_once()

    def test_next_pass_reruns_setup_in_place_and_finishes_the_chain(self):
        self.failed("ZIP-7874", attempts=1)
        self.cc.finish_chain.return_value = {"status": "ok", "workspace": "workspace:19", "worktree": str(self.wts / "zip-7874")}
        self.assertEqual(drip.tick(self.cfg, log=lambda m: None), ["ZIP-7874"])
        self.rerun.assert_called_once_with(str(self.wts / "zip-7874"), "zip-7874")
        self.cc.run_chain.assert_not_called()
        self.assertEqual(self.cc.finish_chain.call_args.args[:3], ("zip-7874", "workspace:19", str(self.wts / "zip-7874")))
        self.sh.transition_to_in_progress.assert_called_once()
        self.start.assert_called_once_with("ZIP-7874", str(self.wts / "zip-7874"), self.cfg.state_root)
        t = state.load(self.cfg.ticket_dir("ZIP-7874"))
        self.assertNotEqual(t.state, "blocked"); state.transition(t, "spinup")
        self.assertEqual((self.wts / "zip-7874" / ".cmux-setup-status").read_text().strip(), "EXIT:0")

    def test_retries_stop_at_the_limit_and_ask_for_the_operator(self):
        self.failed("ZIP-7874", attempts=2)
        self.rerun.return_value = (False, "setup log")
        self.assertEqual(drip.tick(self.cfg, log=lambda m: None), [])
        self.cc.finish_chain.assert_not_called(); self.sh.transition_to_in_progress.assert_not_called()
        t = state.load(self.cfg.ticket_dir("ZIP-7874"))
        self.assertEqual((t.state, t.spinup_failures), ("blocked", 3))
        self.assertIn("needs operator", t.reason)
        self.rerun.reset_mock(); drip.tick(self.cfg, log=lambda m: None)
        self.rerun.assert_not_called()


class MultiEpicTests(DripFixture):
    """Cody, 2026-10-07: Phase 3 still comes first, then the later phase epics in order."""

    def test_board_walks_epics_in_order(self):
        self.patches[1].stop()                                       # the real board(), not the fixture's
        self.cfg.drip["epics"] = ["ZIP-6774", "ZIP-6775"]
        rows = {"ZIP-6774": [{"key": "ZIP-1", "fields": {"summary": "a", "status": {"name": "Selected for Work"}, "assignee": None}}],
                "ZIP-6775": [{"key": "ZIP-2", "fields": {"summary": "b", "status": {"name": "Selected for Work"}, "assignee": None}},
                             {"key": "ZIP-1", "fields": {"summary": "a", "status": {"name": "Selected for Work"}, "assignee": None}}]}
        seen = []
        def acli(args):
            jql = args[args.index("--jql") + 1]; epic = jql.split()[2]; seen.append(epic); return rows[epic]
        with patch.object(drip, "_acli_json", side_effect=acli):
            self.assertEqual([r["key"] for r in drip.board(self.cfg)], ["ZIP-1", "ZIP-2"])   # epic order, no duplicates
        self.assertEqual(seen, ["ZIP-6774", "ZIP-6775"])
        self.patches[1].start()


class DoneInJiraTests(unittest.TestCase):
    def test_reads_keys_of_done_dependencies_from_acli(self):
        def acli(argv, **kw):
            fields = argv[argv.index("--fields") + 1].split(",")
            # acli prints one null per match when only `key` (not a Jira field) is requested.
            rows = [None] if fields == ["key"] else [{"key": "ZIP-7872", "fields": {"status": {"name": "Closed"}}}]
            return drip.subprocess.CompletedProcess(argv, 0, drip.json.dumps(rows), "")
        with patch.object(drip.subprocess, "run", side_effect=acli):
            self.assertEqual(drip.done_in_jira(["ZIP-7872"]), {"ZIP-7872"})


class DripSerializationTests(unittest.TestCase):
    """Branch review 2026-09-23: selection -> Jira transition -> spinup ran with no lease, so a
    scheduled tick and an operator start could build two workspaces for one ticket."""

    def test_a_second_tick_skips_while_one_holds_the_lease(self):
        import tempfile, pathlib as _p, locks, config
        with tempfile.TemporaryDirectory() as d:
            root = _p.Path(d)
            toml = (_p.Path(__file__).resolve().parent.parent / "hopper.toml").read_text()
            toml = toml.replace('state_root = "~/.local/state/agent-loop"', f'state_root = "{root}/state"')
            (root / "hopper.toml").write_text(toml)
            cfg = config.load(root / "hopper.toml"); cfg.ensure_dirs()
            held = locks.Lease(_p.Path(cfg.state_root) / "locks" / "drip", "other-drip")
            self.assertTrue(held.acquire(hold=True))
            try:
                msgs = []
                started = drip.tick(cfg, log=msgs.append)
                self.assertEqual(started, [])
                self.assertTrue(any("lease" in m for m in msgs), msgs)
            finally:
                held.release()

    def test_selection_and_spinup_are_inside_the_lease(self):
        import inspect
        src = inspect.getsource(drip.tick)
        self.assertIn("locks.Lease", src)
        self.assertIn("_tick_locked", src)
        self.assertNotIn("in_flight(cfg)", src)      # the read happens under the lease, not before it


if __name__ == "__main__":
    unittest.main()