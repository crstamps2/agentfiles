import json
import pathlib
import shutil
import tempfile
import unittest

import cohort
import config
import state


class CohortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        source = pathlib.Path(__file__).resolve().parent.parent / "hopper.toml"
        text = source.read_text().replace("\ncapacity = 2 ", "\ncapacity = 4 ")   # these tests exercise four slots
        text = text.replace('state_root = "~/.local/state/agent-loop"', f'state_root = "{root}/state"')
        text = text.replace('pi_agents_dir = "~/.pi/agent/agents"', f'pi_agents_dir = "{root}/agents"')
        path = root / "hopper.toml"
        path.write_text(text)
        self.cfg = config.load(path)
        self.cfg.ensure_dirs()

    def tearDown(self):
        self.tmp.cleanup()

    def test_initialize_writes_exact_four_slot_shape_atomically(self):
        value = cohort.initialize(self.cfg)
        raw = json.loads((self.cfg.state_root / "cohort.json").read_text())
        self.assertEqual(raw["schema_version"], 1)
        self.assertEqual((raw["capacity"], raw["hard_capacity"]), (4, 7))
        self.assertEqual(raw["generation"], 1)
        self.assertEqual(raw["next_admission_sequence"], 1)
        self.assertEqual([row["slot"] for row in raw["slots"]], [1, 2, 3, 4])
        self.assertEqual([row["ticket"] for row in raw["slots"]], [None] * 4)
        self.assertEqual(value.capacity, 4)
        self.assertFalse((self.cfg.state_root / "cohort.json.tmp").exists())

    def test_reservations_count_immediately_and_sequences_increase(self):
        cohort.initialize(self.cfg)
        first = cohort.reserve(self.cfg, "ZIP-1", "drip")
        second = cohort.reserve(self.cfg, "ZIP-2", "drip")
        same = cohort.reserve(self.cfg, "ZIP-1", "drip")
        self.assertEqual((first.phase, second.phase), ("reserved", "reserved"))
        self.assertEqual((first.admission_sequence, second.admission_sequence), (1, 2))
        self.assertEqual(same.admission_sequence, 1)
        self.assertEqual(cohort.members(self.cfg), ["ZIP-1", "ZIP-2"])
        self.assertEqual(cohort.available(self.cfg), 2)

    def test_activate_preserves_membership_and_sequence(self):
        cohort.initialize(self.cfg)
        before = cohort.reserve(self.cfg, "ZIP-1", "drip")
        after = cohort.activate(self.cfg, "ZIP-1")
        self.assertEqual(after.phase, "active")
        self.assertEqual(after.admission_sequence, before.admission_sequence)

    def test_fifth_reservation_is_refused_and_duplicate_rows_fail_validation(self):
        value = cohort.initialize(self.cfg)
        for number in range(1, 5):
            cohort.reserve(self.cfg, f"ZIP-{number}", "drip")
        with self.assertRaises(cohort.CohortError):
            cohort.reserve(self.cfg, "ZIP-5", "drip")
        value = cohort.load(self.cfg)
        value.slots[1].ticket = value.slots[0].ticket
        with self.assertRaises(cohort.CohortError):
            cohort.validate(value)

    def test_unknown_schema_and_malformed_slot_numbers_fail_closed(self):
        cohort.initialize(self.cfg)
        path = self.cfg.state_root / "cohort.json"
        raw = json.loads(path.read_text())
        for mutation in (
            lambda row: row.update(schema_version=2),
            lambda row: row["slots"][0].update(slot=2),
            lambda row: row.update(hard_capacity=8),
        ):
            with self.subTest(mutation=mutation):
                candidate = json.loads(json.dumps(raw))
                mutation(candidate)
                path.write_text(json.dumps(candidate))
                with self.assertRaises(cohort.CohortError):
                    cohort.load(self.cfg)
        path.write_text("not-json")
        with self.assertRaises(cohort.CohortError):
            cohort.load(self.cfg)

    def valid_review_ready(self, key="ZIP-1"):
        cohort.initialize(self.cfg)
        cohort.reserve(self.cfg, key, "drip")
        cohort.activate(self.cfg, key)
        wt = pathlib.Path(self.tmp.name) / "wt"
        shutil.rmtree(wt, ignore_errors=True)
        wt.mkdir()
        ticket = state.Ticket(
            key=key,
            state="human-gate-1",
            worktree=str(wt),
            head_sha="head-1",
            evidence_sha="head-1",
        )
        state.save(self.cfg.ticket_dir(key), ticket)
        (self.cfg.ticket_dir(key) / "ci.json").write_text(json.dumps({"pr": 123, "ready_marked_sha": "head-1"}))
        return ticket, wt

    def test_only_verified_review_ready_ticket_releases_its_slot(self):
        self.valid_review_ready()
        released = cohort.release_if_review_ready(
            self.cfg,
            "ZIP-1",
            pr_lookup=lambda pr: "ready",
            head_lookup=lambda wt: "head-1",
        )
        self.assertTrue(released)
        self.assertEqual(cohort.members(self.cfg), [])
        value = cohort.load(self.cfg)
        self.assertEqual(value.history[-1]["reason"], "verified-human-gate-1")
        self.assertEqual(value.history[-1]["pr"], 123)
        self.assertTrue(self.cfg.ticket_dir("ZIP-1").exists())

    def test_release_predicate_fails_closed_for_every_unready_condition(self):
        cases = {
            "draft-stage": lambda t, ci, wt: setattr(t, "state", "draft-pr"),
            "draft-pr": lambda t, ci, wt: ci.update(pr_state="draft"),
            "closed-pr": lambda t, ci, wt: ci.update(pr_state="closed"),
            "missing-pr": lambda t, ci, wt: ci.pop("pr"),
            "missing-worktree": lambda t, ci, wt: wt.rmdir(),
            "stale-sha": lambda t, ci, wt: (setattr(t, "evidence_sha", "old"), ci.update(ready_marked_sha="old")),
            "active-recovery": lambda t, ci, wt: t.recovery.update(active={"id": "r"}),
            "open-decision": lambda t, ci, wt: setattr(t, "decision_request", {"id": "d"}),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                self.cfg.state_root.joinpath("cohort.json").unlink(missing_ok=True)
                ticket_dir = self.cfg.ticket_dir("ZIP-1")
                if ticket_dir.exists():
                    for child in ticket_dir.iterdir():
                        child.unlink()
                ticket, wt = self.valid_review_ready()
                ci_path = ticket_dir / "ci.json"
                ci = json.loads(ci_path.read_text())
                mutate(ticket, ci, wt)
                state.save(ticket_dir, ticket)
                ci_path.write_text(json.dumps(ci))
                lookup_state = ci.pop("pr_state", "ready")
                self.assertFalse(cohort.release_if_review_ready(
                    self.cfg,
                    "ZIP-1",
                    pr_lookup=lambda pr, result=lookup_state: result,
                    head_lookup=lambda path: "head-2" if name == "stale-sha" else "head-1",
                ))
                self.assertEqual(cohort.members(self.cfg), ["ZIP-1"])

    def test_github_or_head_lookup_failure_retains_the_slot(self):
        self.valid_review_ready()
        for failing in ("pr", "head"):
            with self.subTest(failing=failing):
                def boom(_):
                    raise RuntimeError("offline")
                self.assertFalse(cohort.release_if_review_ready(
                    self.cfg,
                    "ZIP-1",
                    pr_lookup=boom if failing == "pr" else lambda pr: "ready",
                    head_lookup=boom if failing == "head" else lambda wt: "head-1",
                ))
                self.assertEqual(cohort.members(self.cfg), ["ZIP-1"])


class Supervisor:
    def __init__(self):
        self.running = set()
        self.started = []
        self.stopped = []

    def ensure_running(self, key, worktree, state_root):
        if key in self.running:
            return False
        self.running.add(key)
        self.started.append(key)
        return True

    def start(self, key, worktree, state_root):
        self.ensure_running(key, worktree, state_root)

    def stop(self, key):
        self.running.discard(key)
        self.stopped.append(key)


class ReconcileTests(unittest.TestCase):
    setUp = CohortTests.setUp
    tearDown = CohortTests.tearDown

    def member(self, key, stage="implement", *, reserved=False):
        cohort.initialize(self.cfg)
        cohort.reserve(self.cfg, key, "test")
        if not reserved:
            cohort.activate(self.cfg, key)
        wt = pathlib.Path(self.tmp.name) / key.lower()
        wt.mkdir(exist_ok=True)
        ticket = state.Ticket(key=key, state=stage, previous="implement", worktree=str(wt), head_sha="head-1", evidence_sha="head-1")
        state.save(self.cfg.ticket_dir(key), ticket)
        return ticket

    def run_reconcile(self, supervisor=None, **kwargs):
        from unittest.mock import patch
        with patch("drip.board", return_value=[]), patch("drip.done_in_jira", return_value=set()), patch("drip.shared_services_ready", return_value=True):
            return cohort.reconcile(self.cfg, supervisor=supervisor or Supervisor(), pr_lookup=lambda pr: "ready", head_lookup=lambda wt: "head-1", **kwargs)

    def test_missing_supervisor_is_ensured_once_without_restarting_live_work(self):
        for n in range(1, 5):
            self.member(f"ZIP-{n}")
        supervisor = Supervisor()
        first = self.run_reconcile(supervisor)
        second = self.run_reconcile(supervisor)
        self.assertEqual(first.retained, ["ZIP-1", "ZIP-2", "ZIP-3", "ZIP-4"])
        self.assertEqual(supervisor.started, ["ZIP-1", "ZIP-2", "ZIP-3", "ZIP-4"])
        self.assertEqual(second.admitted, [])

    def test_repair_event_resumes_contract_and_a_restart_does_not_repeat_it(self):
        import recovery
        ticket = self.member("ZIP-1", "blocked")
        contract = recovery.new_contract("ZIP-1", {"state": "gates", "head_sha": "head-1"}, {"kind": "resume-stage", "state": "gates"}, ("defect-1",))
        recovery.activate(self.cfg, "ZIP-1", contract)
        recovery.emit_event(self.cfg, {"id": "defect-1", "affected_tickets": [{"ticket": "ZIP-1", "recovery_id": contract["id"]}]}, "landed", "fix-1")
        supervisor = Supervisor()
        first = self.run_reconcile(supervisor)
        second = self.run_reconcile(supervisor)
        self.assertIn("ZIP-1", first.resumed)
        self.assertEqual(state.load(self.cfg.ticket_dir("ZIP-1")).state, "gates")
        self.assertEqual(supervisor.started, ["ZIP-1"])
        self.assertEqual(second.resumed, [])

    def test_ready_recovery_resumes_even_without_a_pending_event(self):
        import recovery
        self.member("ZIP-1", "paused")
        contract = recovery.new_contract("ZIP-1", {"state": "implement"}, {"kind": "resume-stage", "state": "implement"})
        recovery.activate(self.cfg, "ZIP-1", contract)
        recovery.mark_ready(self.cfg, "ZIP-1", "previous-event")
        report = self.run_reconcile()
        self.assertIn("ZIP-1", report.resumed)
        self.assertIsNone(state.load(self.cfg.ticket_dir("ZIP-1")).recovery["active"])

    def test_resuming_recovery_without_an_event_replays_after_supervisor_failure(self):
        from unittest.mock import patch
        import recovery
        self.member("ZIP-1", "paused")
        contract = recovery.new_contract("ZIP-1", {"state": "implement"}, {"kind": "resume-stage", "state": "implement"})
        recovery.activate(self.cfg, "ZIP-1", contract)
        recovery.mark_ready(self.cfg, "ZIP-1", "local-recovery")
        supervisor = Supervisor()
        with patch.object(supervisor, "ensure_running", side_effect=OSError("launchd unavailable")):
            self.run_reconcile(supervisor)
        self.run_reconcile(supervisor)
        self.assertEqual(supervisor.started, ["ZIP-1"])
        self.assertIsNone(state.load(self.cfg.ticket_dir("ZIP-1")).recovery["active"])

    def test_provider_wait_keeps_slot_until_a_primary_or_equivalent_is_available(self):
        import providers
        import recovery
        self.member("ZIP-1", "paused")
        contract = recovery.new_contract("ZIP-1", {"state": "implement", "model": "openai/gpt-5.6-terra"}, {"kind": "wait-provider", "state": "implement"})
        recovery.activate(self.cfg, "ZIP-1", contract)
        providers.mark_exhausted(self.cfg.state_root, "openai", "quota exceeded")
        providers.mark_exhausted(self.cfg.state_root, "anthropic", "quota exceeded")
        waiting = self.run_reconcile()
        self.assertEqual(waiting.resumed, [])
        self.assertEqual(cohort.members(self.cfg), ["ZIP-1"])
        providers.mark_healthy(self.cfg.state_root, "anthropic")
        ready = self.run_reconcile()
        self.assertIn("ZIP-1", ready.resumed)

    def test_human_takeover_and_product_decision_retain_slots_without_dispatch(self):
        import decision
        first = self.member("ZIP-1")
        (self.cfg.ticket_dir(first.key) / "HUMAN").touch()
        self.member("ZIP-2")
        payload = {"schema_version": 1, "summary": "Choose the public API", "sources": ["ticket", "repository"],
                   "why_unresolved": "The sources describe incompatible public names.",
                   "options": [{"id": "a", "label": "Keep name", "consequences": "Compatible"}, {"id": "b", "label": "Rename", "consequences": "Needs migration"}],
                   "recommendation": "a", "recommendation_reason": "Preserve compatibility", "question": "Which public name?",
                   "checkpoint": {"state": "implement"}, "resume": {"kind": "resume-stage", "state": "implement"}}
        decision.propose(self.cfg, "ZIP-2", payload)
        supervisor = Supervisor()
        report = self.run_reconcile(supervisor)
        self.assertEqual(cohort.members(self.cfg), ["ZIP-1", "ZIP-2"])
        self.assertEqual(supervisor.started, [])
        self.assertIn("HUMAN", report.waits["ZIP-1"])
        self.assertIn("decision", report.waits["ZIP-2"])

    def test_verified_release_refills_only_from_oldest_parked_ticket(self):
        self.member("ZIP-1", "human-gate-1")
        (self.cfg.ticket_dir("ZIP-1") / "ci.json").write_text(json.dumps({"pr": 123, "ready_marked_sha": "head-1"}))
        for n in range(2, 5):
            self.member(f"ZIP-{n}")
        for n, sequence in ((6, 20), (5, 10)):
            wt = pathlib.Path(self.tmp.name) / f"zip-{n}"; wt.mkdir()
            tdir = self.cfg.ticket_dir(f"ZIP-{n}")
            state.save(tdir, state.Ticket(key=f"ZIP-{n}", state="implement", worktree=str(wt)))
            (tdir / "PARKED").touch()
            (tdir / "PARKED.json").write_text(json.dumps({"admission_sequence": sequence}))
        supervisor = Supervisor()
        report = self.run_reconcile(supervisor)
        self.assertEqual(report.released, ["ZIP-1"])
        self.assertEqual(report.admitted, ["ZIP-5"])
        self.assertEqual(cohort.members(self.cfg), ["ZIP-5", "ZIP-2", "ZIP-3", "ZIP-4"])
        self.assertEqual(supervisor.stopped, ["ZIP-1"])
        self.assertTrue((self.cfg.ticket_dir("ZIP-6") / "PARKED").exists())

    def test_reserved_setup_failure_retries_same_ticket_and_retains_reservation(self):
        from unittest.mock import patch
        import drip
        ticket = self.member("ZIP-1", "blocked", reserved=True)
        with patch("drip.retry_spinup", side_effect=drip.SpinupFailed("setup-failed", ticket.worktree)):
            report = self.run_reconcile()
        self.assertEqual(cohort.members(self.cfg), ["ZIP-1"])
        self.assertEqual(cohort.load(self.cfg).slots[0].phase, "reserved")
        (self.cfg.ticket_dir("ZIP-1") / "spinup-backoff.json").write_text(json.dumps({"next_epoch": 0}))   # backoff elapsed
        with patch("drip.retry_spinup", return_value=pathlib.Path(ticket.worktree)), patch("drip.subprocess.run", return_value=type("Result", (), {"stdout": "internal/zip-1"})()):
            report = self.run_reconcile()
        self.assertIn("ZIP-1", report.admitted)
        self.assertEqual(cohort.load(self.cfg).slots[0].phase, "active")

    def test_malformed_ticket_or_recovery_is_diagnosed_without_releasing_capacity(self):
        for n in range(1, 5):
            self.member(f"ZIP-{n}")
        (self.cfg.ticket_dir("ZIP-1") / "state.json").write_text("not-json")
        ticket = state.load(self.cfg.ticket_dir("ZIP-2")); ticket.recovery["active"] = {"schema_version": 77}
        state.save(self.cfg.ticket_dir("ZIP-2"), ticket)
        report = self.run_reconcile()
        self.assertEqual(len(cohort.members(self.cfg)), 4)
        self.assertTrue(any("ZIP-1" in error for error in report.errors))
        self.assertTrue(any("ZIP-2" in error for error in report.errors))
        self.assertTrue(list((self.cfg.state_root / "defects" / "open").glob("*.json")))

    def test_dry_run_does_not_write_state_markers_locks_or_supervisors(self):
        self.member("ZIP-1")
        def files():
            return {str(p.relative_to(self.cfg.state_root)): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.cfg.state_root.rglob("*") if p.is_file()}
        before = files()
        supervisor = Supervisor()
        self.run_reconcile(supervisor, dry_run=True)
        self.assertEqual(files(), before)
        self.assertEqual(supervisor.started, [])
        self.assertEqual(supervisor.stopped, [])

    def test_crash_after_parked_reservation_converges_on_the_same_ticket(self):
        from unittest.mock import patch
        self.member("ZIP-1")
        tdir = self.cfg.ticket_dir("ZIP-5")
        wt = pathlib.Path(self.tmp.name) / "zip-5"; wt.mkdir()
        state.save(tdir, state.Ticket(key="ZIP-5", state="implement", worktree=str(wt)))
        (tdir / "PARKED").touch()
        supervisor = Supervisor()
        original = supervisor.ensure_running
        class Crash(BaseException):
            pass
        def fail_after_reserve(key, *args):
            if key == "ZIP-5":
                raise Crash()
            return original(key, *args)
        with patch.object(supervisor, "ensure_running", side_effect=fail_after_reserve), self.assertRaises(Crash):
            self.run_reconcile(supervisor)
        self.assertEqual(cohort.members(self.cfg), ["ZIP-1", "ZIP-5"])
        sequence = cohort.load(self.cfg).next_admission_sequence
        report = self.run_reconcile(supervisor)
        self.assertEqual(cohort.load(self.cfg).next_admission_sequence, sequence)
        self.assertEqual(supervisor.started, ["ZIP-1", "ZIP-5"])
        self.assertEqual(cohort.load(self.cfg).slots[1].phase, "active")

    def test_crash_after_release_history_still_stops_the_released_supervisor(self):
        from unittest.mock import patch
        self.member("ZIP-1", "human-gate-1")
        (self.cfg.ticket_dir("ZIP-1") / "ci.json").write_text(json.dumps({"pr": 123, "ready_marked_sha": "head-1"}))
        original = cohort._save_unlocked
        class Crash(BaseException):
            pass
        def save_then_crash(cfg, value):
            original(cfg, value)
            if value.history and value.history[-1].get("event") == "released":
                raise Crash()
        with patch.object(cohort, "_save_unlocked", side_effect=save_then_crash), self.assertRaises(Crash):
            self.run_reconcile()
        supervisor = Supervisor()
        self.run_reconcile(supervisor)
        self.assertEqual(cohort.members(self.cfg), [])
        self.assertEqual(supervisor.stopped, ["ZIP-1"])
        self.assertEqual(len(cohort.load(self.cfg).history), 1)

    def test_concurrent_drip_reconcile_and_manual_resume_never_duplicate_admission(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        import drip
        import runner
        import supervise
        self.member("ZIP-1")
        for n in range(2, 9):
            wt = pathlib.Path(self.tmp.name) / f"zip-{n}"; wt.mkdir()
            tdir = self.cfg.ticket_dir(f"ZIP-{n}")
            state.save(tdir, state.Ticket(key=f"ZIP-{n}", state="implement", worktree=str(wt)))
            (tdir / "PARKED").touch()
            (tdir / "PARKED.json").write_text(json.dumps({"admission_sequence": n}))
        supervisor = Supervisor()
        cfgpath = str(self.cfg.state_root.parent / "hopper.toml")
        with patch("drip.board", return_value=[]), patch("drip.done_in_jira", return_value=set()), patch("drip.shared_services_ready", return_value=True), patch.object(supervise, "ensure_running", side_effect=supervisor.ensure_running), patch.object(supervise, "status", return_value="state=running"):
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(cohort.reconcile, self.cfg, supervisor=supervisor), pool.submit(drip.tick, self.cfg, log=lambda message: None),
                           pool.submit(runner.main, ["--config", cfgpath, "resume", "--ticket", "ZIP-1", "--from", "implement"])]
                for future in futures:
                    future.result()
            self.run_reconcile(supervisor)
        self.assertEqual(cohort.members(self.cfg), ["ZIP-1", "ZIP-2", "ZIP-3", "ZIP-4"])
        self.assertEqual(len(supervisor.started), len(set(supervisor.started)))

    def test_one_launchctl_failure_does_not_abort_the_rest_of_the_pass(self):
        for n in range(1, 4):
            self.member(f"ZIP-{n}")
        supervisor = Supervisor()
        original = supervisor.ensure_running
        def flaky(key, *args):
            if key == "ZIP-1":
                raise RuntimeError("launchctl bootstrap failed: 5")
            return original(key, *args)
        supervisor.ensure_running = flaky
        from unittest.mock import patch
        with patch("drip.board", return_value=[]), patch("drip.done_in_jira", return_value=set()), patch("drip.shared_services_ready", return_value=True):
            report = cohort.reconcile(self.cfg, supervisor=supervisor, pr_lookup=lambda pr: "ready", head_lookup=lambda wt: "head-1")
        self.assertEqual(supervisor.started, ["ZIP-2", "ZIP-3"])
        self.assertTrue(any("ZIP-1" in error for error in report.errors))

    def test_a_member_that_passed_human_gate_one_is_released(self):
        for n, stage in ((1, "done"), (2, "human-gate-2"), (3, "colleague-loop")):
            self.member(f"ZIP-{n}", stage)
        self.member("ZIP-4", "human-gate-1")
        (self.cfg.ticket_dir("ZIP-4") / "ci.json").write_text(json.dumps({"pr": 9, "ready_marked_sha": "head-1"}))
        from unittest.mock import patch
        with patch("drip.board", return_value=[]), patch("drip.done_in_jira", return_value=set()), patch("drip.shared_services_ready", return_value=True):
            report = cohort.reconcile(self.cfg, supervisor=Supervisor(), pr_lookup=lambda pr: "merged", head_lookup=lambda wt: "head-1")
        self.assertEqual(sorted(report.released), ["ZIP-1", "ZIP-2", "ZIP-3", "ZIP-4"])
        reasons = {row["ticket"]: row["reason"] for row in cohort.load(self.cfg).history if row.get("event") == "released"}
        self.assertEqual(reasons["ZIP-1"], "passed-human-gate-1")

    def test_dry_run_before_migration_reconciles_a_virtual_cohort(self):
        for n in range(1, 6):
            wt = pathlib.Path(self.tmp.name) / f"zip-{n}"; wt.mkdir()
            (wt / "planning" / f"zip-{n}").mkdir(parents=True)
            (wt / "planning" / f"zip-{n}" / "transcript.md").write_text(f"## 2026-09-0{n}T00:00:00Z coordinator started\n")
            state.save(self.cfg.ticket_dir(f"ZIP-{n}"), state.Ticket(key=f"ZIP-{n}", state="implement", worktree=str(wt)))
        report = self.run_reconcile(dry_run=True)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.retained, ["ZIP-1", "ZIP-2", "ZIP-3", "ZIP-4"])
        self.assertFalse((self.cfg.state_root / "cohort.json").exists())

    def test_spinup_runs_outside_the_cohort_lease_and_backs_off_after_failures(self):
        from unittest.mock import patch
        import drip, locks
        cohort.initialize(self.cfg)
        board = [{"key": "ZIP-9", "summary": "x", "status": "Selected for Work", "assignee": None}]
        held = []
        def spinup(key, log=print):
            held.append(locks.is_held(self.cfg.state_root / "locks" / "cohort"))
            raise drip.SpinupFailed("setup-failed", None)
        with patch("drip.board", return_value=board), patch("drip.done_in_jira", return_value=set()), \
             patch("drip.shared_services_ready", return_value=True), patch("drip.spinup_with_panes", side_effect=spinup):
            for _ in range(6):
                cohort.reconcile(self.cfg, supervisor=Supervisor(), pr_lookup=lambda pr: "ready", head_lookup=lambda wt: "head-1")
        self.assertEqual(held[0], False, "spinup ran while holding the cohort lease")
        self.assertLessEqual(len(held), drip.MAX_SPINUP_ATTEMPTS, "retries were not backed off")
        self.assertEqual(cohort.members(self.cfg), ["ZIP-9"], "the reservation is kept")

    def test_restart_after_supervisor_failure_converges_without_readmission(self):
        from unittest.mock import patch
        self.member("ZIP-1")
        supervisor = Supervisor()
        class Crash(BaseException):
            pass
        with patch.object(supervisor, "ensure_running", side_effect=Crash), self.assertRaises(Crash):
            self.run_reconcile(supervisor)
        self.run_reconcile(supervisor)
        self.assertEqual(supervisor.started, ["ZIP-1"])
        self.assertEqual(cohort.members(self.cfg), ["ZIP-1"])
        self.assertEqual(cohort.load(self.cfg).next_admission_sequence, 2)


if __name__ == "__main__":
    unittest.main()
