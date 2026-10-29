import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import config
import sidebar
import state

WORKSPACES = "  workspace:11  zip-1\n  workspace:12  zip-2\n* workspace:13  zip-3  [selected]\n  workspace:14  other\n"


class FakeCmux:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __call__(self, argv, **kwargs):
        self.calls.append(argv[1:])
        if self.fail:
            raise self.fail
        out = WORKSPACES if argv[1:3] == ["workspace", "list"] else "OK\n"
        if argv[1:3] == ["workspace-group", "list"]:
            out = json.dumps({"groups": []})
        return subprocess.CompletedProcess(argv, 0, out, "")


class SidebarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = pathlib.Path(self.tmp.name)
        toml = (pathlib.Path(__file__).resolve().parent.parent / "hopper.toml").read_text()
        toml = toml.replace('state_root = "~/.local/state/agent-loop"', f'state_root = "{root}/state"')
        toml = toml.replace('pi_agents_dir = "~/.pi/agent/agents"', f'pi_agents_dir = "{root}"')
        (root / "hopper.toml").write_text(toml)
        self.cfg = config.load(root / "hopper.toml"); self.cfg.ensure_dirs()
        production = patch("sidebar.PRODUCTION_ROOT", self.cfg.state_root)
        production.start()
        self.addCleanup(production.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def ticket(self, key, stage, reason="", **extra):
        t = state.Ticket(key=key, state=stage, reason=reason, **extra)
        state.save(self.cfg.ticket_dir(key), t)
        return t

    def test_each_state_maps_to_its_spinner_and_pill(self):
        cases = [
            (dict(stage="implement"), (True, "implementing", "#34C759")),
            (dict(stage="plan"), (True, "planning", "#34C759")),
            (dict(stage="bot-loop"), (True, "bot review", "#34C759")),
            (dict(stage="paused", reason="heavy lane held"), (True, "self-healing", "#FFCC00")),
            (dict(stage="paused", reason="reviewer questions need Cody: comment 1"), (False, "needs Cody", "#FF9500")),
            (dict(stage="decision-required"), (False, "needs Cody", "#FF9500")),
        ]
        for spec, (spinner, text, color) in cases:
            with self.subTest(spec=spec):
                got = sidebar.label(self.cfg, self.ticket("ZIP-1", spec["stage"], spec.get("reason", "")))
                self.assertEqual((got.spinner, got.text, got.color), (spinner, text, color))

    def test_recovery_and_review_ready_and_queued_labels(self):
        t = self.ticket("ZIP-1", "blocked", recovery={"active": {"status": "waiting-provider"}, "queued": []})
        self.assertEqual(sidebar.label(self.cfg, t)[:2], (False, "quota wait"))
        t = self.ticket("ZIP-1", "blocked", recovery={"active": {"status": "waiting-repair"}, "queued": []})
        self.assertEqual(sidebar.label(self.cfg, t)[:2], (True, "self-healing"))
        t = self.ticket("ZIP-1", "human-gate-1")
        (self.cfg.ticket_dir("ZIP-1") / "ci.json").write_text(json.dumps({"pr": 48731}))
        self.assertEqual(sidebar.label(self.cfg, t)[:2], (False, "ready · PR #48731"))
        t = self.ticket("ZIP-1", "implement")
        self.assertEqual(sidebar.label(self.cfg, t, queued=3)[:2], (False, "queued #3"))

    def test_sync_drives_spinner_and_pill_for_known_workspaces_only(self):
        self.ticket("ZIP-1", "implement"); self.ticket("ZIP-9", "implement")
        cmux = FakeCmux()
        sidebar.sync(self.cfg, run=cmux, force=True)
        self.assertIn(["workspace", "loading", "on", "--id", "agent-loop", "--workspace", "workspace:11"], cmux.calls)
        self.assertTrue(any(c[:3] == ["set-status", "agent-loop", "implementing"] and c[-1] == "workspace:11" for c in cmux.calls))
        self.assertFalse(any("ZIP-9" in " ".join(c) or "zip-9" in " ".join(c) for c in cmux.calls))

    def test_the_selected_workspace_is_still_found(self):
        self.ticket("ZIP-3", "implement")
        cmux = FakeCmux()
        sidebar.sync(self.cfg, run=cmux, force=True)
        self.assertTrue(any(c[-1] == "workspace:13" for c in cmux.calls))

    def test_unchanged_state_sends_nothing_on_the_next_pass(self):
        self.ticket("ZIP-1", "implement")
        first, second = FakeCmux(), FakeCmux()
        sidebar.sync(self.cfg, run=first, force=True)
        sidebar.sync(self.cfg, run=second, force=True)
        reads = (["workspace", "list"], ["workspace-group", "list"])
        self.assertEqual([c for c in second.calls if c[:2] not in reads], [])

    def test_cmux_failures_never_raise(self):
        self.ticket("ZIP-1", "implement")
        for failure in (FileNotFoundError("cmux"), subprocess.TimeoutExpired("cmux", 5), RuntimeError("socket refused")):
            with self.subTest(failure=failure):
                sidebar.sync(self.cfg, run=FakeCmux(fail=failure), force=True)   # no exception
        self.assertFalse((self.cfg.state_root / "sidebar.json").exists() and
                         json.loads((self.cfg.state_root / "sidebar.json").read_text()), "failed sends are not cached")

    def test_a_test_state_root_never_touches_the_real_sidebar(self):
        self.ticket("ZIP-1", "implement")
        cmux = FakeCmux()
        with patch("sidebar.PRODUCTION_ROOT", self.cfg.state_root / "not-production"):
            sidebar.sync(self.cfg, run=cmux)
        self.assertEqual(cmux.calls, [])


class StallTests(SidebarTests):
    def stale(self, key, stage="paused", reason="waiting for automatic recovery", hours=6):
        import datetime as dt
        t = self.ticket(key, stage, reason)
        raw = json.loads((self.cfg.ticket_dir(key) / "state.json").read_text())
        raw["updated_utc"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        (self.cfg.ticket_dir(key) / "state.json").write_text(json.dumps(raw))
        return state.load(self.cfg.ticket_dir(key))

    def test_a_member_with_no_progress_for_hours_is_labelled_stalled(self):
        got = sidebar.label(self.cfg, self.stale("ZIP-1", hours=54))
        self.assertEqual((got.spinner, got.text, got.color), (False, "stalled 54h", sidebar.RED))

    def test_waiting_on_cody_or_review_is_not_a_stall(self):
        self.assertEqual(sidebar.label(self.cfg, self.stale("ZIP-1", reason="reviewer questions need Cody: x")).text, "needs Cody")
        self.assertTrue(sidebar.label(self.cfg, self.stale("ZIP-2", stage="human-gate-1")).text.startswith("ready"))

    def test_the_watchdog_notifies_once_per_stall(self):
        import cohort
        cohort.initialize(self.cfg); cohort.reserve(self.cfg, "ZIP-1", "t"); cohort.activate(self.cfg, "ZIP-1")
        self.stale("ZIP-1", hours=6)
        sent = []
        with patch("lifecycle._notify_once", side_effect=lambda tdir, key, title, body: sent.append(title)):
            sidebar.watchdog(self.cfg)
            sidebar.watchdog(self.cfg)
        self.assertEqual(len(sent), 2)              # _notify_once itself dedupes; the watchdog calls it each pass
        self.assertIn("stalled", sent[0])

    def test_a_fresh_member_raises_nothing(self):
        import cohort
        cohort.initialize(self.cfg); cohort.reserve(self.cfg, "ZIP-1", "t"); cohort.activate(self.cfg, "ZIP-1")
        self.ticket("ZIP-1", "implement")
        with patch("lifecycle._notify_once") as notify:
            sidebar.watchdog(self.cfg)
        notify.assert_not_called()


class ReconcileIsolationTests(unittest.TestCase):
    def test_a_sidebar_crash_does_not_change_the_reconcile_result(self):
        import cohort
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            toml = (pathlib.Path(__file__).resolve().parent.parent / "hopper.toml").read_text()
            toml = toml.replace('state_root = "~/.local/state/agent-loop"', f'state_root = "{root}/state"')
            (root / "hopper.toml").write_text(toml)
            cfg = config.load(root / "hopper.toml"); cfg.ensure_dirs()
            cohort.initialize(cfg)
            with patch("sidebar.sync", side_effect=RuntimeError("cmux exploded")), \
                 patch("drip.board", return_value=[]), patch("drip.done_in_jira", return_value=set()), \
                 patch("drip.shared_services_ready", return_value=True):
                report = cohort.reconcile(cfg, supervisor=type("S", (), {"ensure_running": lambda *a: False, "stop": lambda *a: None})(),
                                          pr_lookup=lambda pr: "ready", head_lookup=lambda wt: "h")
            self.assertEqual(report.errors, [])


if __name__ == "__main__":
    unittest.main()
