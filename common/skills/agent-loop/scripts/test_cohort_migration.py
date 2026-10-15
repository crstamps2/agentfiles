import hashlib
import json
import pathlib
import subprocess
import tempfile
import unittest

import cohort
import cohort_migration as migration
import config
import state


class FakeSupervisor:
    def __init__(self, active=()):
        self._active = list(active)
        self.stopped, self.started = [], []
        self.observed = []

    def active(self):
        return list(self._active)

    def stop(self, key):
        self.observed.append(self.on_stop(key) if hasattr(self, "on_stop") else None)
        self.stopped.append(key)

    def start(self, key, worktree, state_root):
        self.started.append(key)


class MigrationFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        text = (pathlib.Path(__file__).resolve().parent.parent / "hopper.toml").read_text()
        text = text.replace('state_root = "~/.local/state/agent-loop"', f'state_root = "{self.root}/state"')
        text = text.replace('pi_agents_dir = "~/.pi/agent/agents"', f'pi_agents_dir = "{self.root}/agents"')
        (self.root / "hopper.toml").write_text(text)
        self.cfg = config.load(self.root / "hopper.toml")
        self.cfg.ensure_dirs()

    def tearDown(self):
        self.tmp.cleanup()

    def ticket(self, key, stage="implement", *, previous="implement", reason="", transcript=None, attempt=None, worktree=True, pr=None):
        wt = self.root / "wts" / key.lower()
        if worktree:
            (wt / "planning" / key.lower()).mkdir(parents=True, exist_ok=True)
        t = state.Ticket(key=key, state=stage, previous=previous, reason=reason, worktree=str(wt))
        state.save(self.cfg.ticket_dir(key), t)
        if transcript:
            (wt / "planning" / key.lower() / "transcript.md").write_text(f"\n## {transcript} coordinator started\n")
        if attempt:
            adir = self.cfg.state_root / "attempts" / key / "001" / "1"
            adir.mkdir(parents=True, exist_ok=True)
            (adir / "attempt.json").write_text(json.dumps({"started_utc": attempt, "status": "PROJECTED"}))
        if pr:
            (self.cfg.ticket_dir(key) / "ci.json").write_text(json.dumps({"pr": pr}))
        return wt

    def observed_tree(self, key, files):
        """Commit `files` in the ticket worktree, record that tree, then remove them from the working copy."""
        wt = self.root / "wts" / key.lower()
        git = lambda *a: subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True, check=True).stdout.strip()
        if not (wt / ".git").exists():
            git("init", "-q"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
        for path in files:
            (wt / path).parent.mkdir(parents=True, exist_ok=True); (wt / path).write_text("work")
        git("add", "-A"); git("commit", "-qm", "work", "--allow-empty")
        tree = git("rev-parse", "HEAD^{tree}")
        for path in files:
            (wt / path).unlink()
        return tree

    def accepted_task(self, key, files, *, slug="component", observed=None):
        tree = self.observed_tree(key, files if observed is None else observed)
        t = state.load(self.cfg.ticket_dir(key))
        t.attempts[f"{key}/001"] = [{"n": 1, "outcome": "accepted"}]
        state.save(self.cfg.ticket_dir(key), t)
        adir = self.cfg.state_root / "attempts" / key / "001" / "1"
        adir.mkdir(parents=True, exist_ok=True)
        allowed = ", ".join(json.dumps(path) for path in files)
        (adir / "task.toml").write_text(f'id = "001"\nslug = "{slug}"\nallowed_files = [{allowed}]\n'
                                        f'verification_commands = ["bin/rails test {files[0]}"]\n')
        (adir / "attempt.json").write_text(json.dumps({"started_utc": "2026-10-01T00:00:00Z", "status": "PROJECTED", "outcome": "accepted",
                                                       "changed_paths": [], "observed_tree": tree}))

    def landed(self, key, sig="fix1"):
        path = self.cfg.state_root / "defects" / "landed" / f"{sig}-1.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"id": sig, "key": key, "summary": "repaired", "sha": "abc"}))

    def digest(self, *, exclude=()):
        out = {}
        for p in sorted(self.root.rglob("*")):
            rel = str(p.relative_to(self.root))
            if p.is_file() and not any(part in rel for part in exclude):
                out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        return out


class ChronologyTests(MigrationFixture):
    def test_precedence_cohort_then_parked_then_timestamps_then_configured_order(self):
        self.ticket("ZIP-E", transcript="2026-09-01T00:00:00Z")
        self.ticket("ZIP-A", transcript="2026-08-01T00:00:00Z")
        parked = self.cfg.ticket_dir("ZIP-A")
        (parked / "PARKED").touch(); (parked / "PARKED.json").write_text(json.dumps({"admission_sequence": 3}))
        self.ticket("ZIP-B", transcript="2026-10-01T00:00:00Z", attempt="2026-09-02T00:00:00Z")
        self.ticket("ZIP-C", attempt="2026-09-30T00:00:00Z")
        self.ticket("ZIP-D")
        cohort.initialize(self.cfg)
        cohort.reserve(self.cfg, "ZIP-E", "drip")
        plan = migration.build_plan(self.cfg)
        self.assertEqual([e.ticket for e in plan.candidates], ["ZIP-E", "ZIP-A", "ZIP-C", "ZIP-B", "ZIP-D"])
        sources = {e.ticket: e.source for e in plan.candidates}
        self.assertEqual(sources, {"ZIP-E": "cohort", "ZIP-A": "parked", "ZIP-C": "attempt", "ZIP-B": "transcript", "ZIP-D": "order"})
        self.assertTrue(any("ZIP-D" in note for note in plan.ambiguities))

    def test_review_ready_and_done_tickets_are_excluded_and_reported(self):
        self.ticket("ZIP-1", transcript="2026-09-01T00:00:00Z")
        self.ticket("ZIP-2", "human-gate-1", previous="bot-loop", pr=48650)
        plan = migration.build_plan(self.cfg)
        self.assertEqual([e.ticket for e in plan.candidates], ["ZIP-1"])
        self.assertEqual(plan.excluded, [{"ticket": "ZIP-2", "state": "human-gate-1", "pr": 48650}])

    def test_dry_run_report_names_every_section_and_mutates_nothing(self):
        for n in range(1, 7):
            self.ticket(f"ZIP-{n}", transcript=f"2026-09-0{n}T00:00:00Z")
        before = self.digest()
        supervisor = FakeSupervisor(active=["ZIP-5"])
        report = migration.render(migration.build_plan(self.cfg))
        self.assertEqual(self.digest(), before)
        self.assertEqual((supervisor.stopped, supervisor.started), ([], []))
        for heading in ("CHRONOLOGY", "PROPOSED COHORT", "PARK", "EXCLUDED", "RECOVERY", "AMBIGUITIES"):
            self.assertIn(heading, report)
        self.assertIn("1  ZIP-1", report)
        self.assertIn("ZIP-5 #5", report)


class ApplyTests(MigrationFixture):
    def test_apply_admits_four_oldest_and_parks_the_rest_after_cohort_is_durable(self):
        for n in range(1, 7):
            self.ticket(f"ZIP-{n}", transcript=f"2026-09-0{n}T00:00:00Z")
        self.ticket("ZIP-9", "human-gate-1", previous="bot-loop", pr=1)
        defect = self.cfg.state_root / "defects" / "open" / "keep.json"
        defect.parent.mkdir(parents=True, exist_ok=True); defect.write_text("{}")
        before = self.digest(exclude=("cohort", "PARKED"))
        supervisor = FakeSupervisor(active=["ZIP-1", "ZIP-5", "ZIP-6"])
        supervisor.on_stop = lambda key: ((self.cfg.state_root / "cohort.json").exists(), (self.cfg.ticket_dir(key) / "PARKED").exists())
        snapshot = migration.apply(self.cfg, migration.build_plan(self.cfg), supervisor=supervisor)
        self.assertTrue(snapshot.is_dir())
        self.assertEqual(cohort.members(self.cfg), ["ZIP-1", "ZIP-2", "ZIP-3", "ZIP-4"])
        self.assertEqual([row.admission_sequence for row in cohort.load(self.cfg).slots], [1, 2, 3, 4])
        self.assertEqual(supervisor.stopped, ["ZIP-5", "ZIP-6"])
        self.assertEqual(supervisor.observed, [(True, True), (True, True)])
        self.assertEqual(json.loads((self.cfg.ticket_dir("ZIP-6") / "PARKED.json").read_text())["admission_sequence"], 6)
        self.assertFalse((self.cfg.ticket_dir("ZIP-9") / "PARKED").exists())
        self.assertEqual(self.digest(exclude=("cohort", "PARKED")), before)

    def test_apply_refuses_when_a_cohort_already_exists(self):
        self.ticket("ZIP-1", transcript="2026-09-01T00:00:00Z")
        cohort.initialize(self.cfg)
        with self.assertRaises(migration.MigrationError):
            migration.apply(self.cfg, migration.build_plan(self.cfg), supervisor=FakeSupervisor())


class RecoverySynthesisTests(MigrationFixture):
    def apply_one(self, key):
        migration.apply(self.cfg, migration.build_plan(self.cfg), supervisor=FakeSupervisor())
        return state.load(self.cfg.ticket_dir(key)).recovery["active"]

    def test_missing_accepted_output_becomes_exact_retry_task(self):
        self.ticket("ZIP-1", "blocked", reason="owner", transcript="2026-09-01T00:00:00Z")
        self.accepted_task("ZIP-1", ["app/views/components/zui/pagination/pagination.rb"], slug="pagination-component")
        active = self.apply_one("ZIP-1")
        self.assertEqual(active["resume"], {"kind": "retry-task", "state": "implement", "task_id": "001", "task_slug": "pagination-component"})
        self.assertEqual(active["status"], "ready-to-resume")

    def test_a_task_that_deleted_its_file_is_not_treated_as_lost_output(self):
        self.ticket("ZIP-1", "blocked", reason="owner", transcript="2026-09-01T00:00:00Z")
        self.accepted_task("ZIP-1", ["app/assets/stylesheets/_list-group.scss"], observed=[])
        active = self.apply_one("ZIP-1")
        self.assertEqual(active["resume"]["kind"], "resume-stage")
        self.assertEqual(active["status"], "waiting-repair")

    def test_repaired_gate_defect_with_unchanged_checkpoint_resumes_gates(self):
        self.ticket("ZIP-1", "blocked", previous="gates", reason="owner", transcript="2026-09-01T00:00:00Z")
        self.landed("ZIP-1")
        active = self.apply_one("ZIP-1")
        self.assertEqual(active["resume"], {"kind": "resume-stage", "state": "gates"})
        self.assertEqual(active["status"], "ready-to-resume")

    def test_repaired_publication_with_accepted_work_republishes(self):
        wt = self.ticket("ZIP-1", "blocked", reason="publish failed: PublishError: stale index.lock", transcript="2026-09-01T00:00:00Z")
        self.accepted_task("ZIP-1", ["app/a.rb"])
        (wt / "app" / "a.rb").write_text("present")
        self.landed("ZIP-1")
        active = self.apply_one("ZIP-1")
        self.assertEqual(active["resume"], {"kind": "republish", "state": "implement"})

    def test_generic_owner_without_evidence_gets_diagnostic_recovery_not_a_decision(self):
        self.ticket("ZIP-1", "blocked", reason="owner", transcript="2026-09-01T00:00:00Z")
        active = self.apply_one("ZIP-1")
        ticket = state.load(self.cfg.ticket_dir("ZIP-1"))
        self.assertEqual(active["resume"], {"kind": "resume-stage", "state": "implement"})
        self.assertEqual(active["status"], "waiting-repair")
        self.assertNotEqual(ticket.state, "decision-required")
        opened = [json.loads(p.read_text()) for p in (self.cfg.state_root / "defects" / "open").glob("*.json")]
        self.assertEqual([row["affected_tickets"][0]["recovery_id"] for row in opened], [active["id"]])

    def test_unevented_diagnostic_contract_is_superseded_by_direct_evidence_and_restored_on_rollback(self):
        import recovery
        self.ticket("ZIP-1", "blocked", reason="owner", transcript="2026-09-01T00:00:00Z")
        self.accepted_task("ZIP-1", ["app/views/components/zui/pagination/pagination.rb"], slug="pagination-component")
        diagnostic = recovery.new_contract("ZIP-1", {"state": "implement"}, {"kind": "resume-stage", "state": "implement"}, ("lost-defect",))
        recovery.activate(self.cfg, "ZIP-1", diagnostic)
        plan = migration.build_plan(self.cfg)
        self.assertEqual(plan.recoveries["ZIP-1"]["supersedes"], diagnostic["id"])
        snapshot = migration.apply(self.cfg, plan, supervisor=FakeSupervisor())
        self.assertEqual(state.load(self.cfg.ticket_dir("ZIP-1")).recovery["active"]["resume"]["kind"], "retry-task")
        migration.rollback(self.cfg, snapshot.name, supervisor=FakeSupervisor())
        self.assertEqual(state.load(self.cfg.ticket_dir("ZIP-1")).recovery["active"]["id"], diagnostic["id"])

    def test_exhausted_task_ladder_with_a_landed_repair_retries_that_task(self):
        self.ticket("ZIP-1", "blocked", reason="verification introduced forbidden change: yarn.lock", transcript="2026-09-01T00:00:00Z")
        t = state.load(self.cfg.ticket_dir("ZIP-1"))
        t.attempts["ZIP-1/001"] = [{"n": 1, "outcome": "rejected"}, {"n": 2, "outcome": "rejected"}]
        state.save(self.cfg.ticket_dir("ZIP-1"), t)
        adir = self.cfg.state_root / "attempts" / "ZIP-1" / "001" / "2"; adir.mkdir(parents=True)
        (adir / "task.toml").write_text('id = "001"\nslug = "list"\nallowed_files = ["app/a.rb"]\n')
        self.landed("ZIP-1")
        active = self.apply_one("ZIP-1")
        self.assertEqual(active["resume"], {"kind": "retry-task", "state": "implement", "task_id": "001", "task_slug": "list"})

    def test_diagnostic_contract_is_linked_to_the_defect_it_records(self):
        import recovery
        self.ticket("ZIP-1", "blocked", reason="owner", transcript="2026-09-01T00:00:00Z")
        active = self.apply_one("ZIP-1")
        opened = json.loads(next((self.cfg.state_root / "defects" / "open").glob("*.json")).read_text())
        recovery.emit_event(self.cfg, opened, "not-needed")
        results = recovery.consume_events(self.cfg, retry_task=lambda *a, **k: None, ensure_supervisor=lambda *a: None)
        self.assertEqual(results, ["ZIP-1:resumed"])

    def test_diagnostic_tickets_get_their_own_defects(self):
        self.ticket("ZIP-1", "blocked", reason="owner", transcript="2026-09-01T00:00:00Z")
        self.ticket("ZIP-2", "blocked", reason="owner", transcript="2026-09-02T00:00:00Z")
        migration.apply(self.cfg, migration.build_plan(self.cfg), supervisor=FakeSupervisor())
        self.assertEqual(len(list((self.cfg.state_root / "defects" / "open").glob("*.json"))), 2)

    def test_plan_reports_synthesis_evidence_and_confidence(self):
        self.ticket("ZIP-1", "blocked", reason="owner", transcript="2026-09-01T00:00:00Z")
        self.accepted_task("ZIP-1", ["app/views/components/zui/pagination/pagination.rb"])
        plan = migration.build_plan(self.cfg)
        self.assertEqual(plan.recoveries["ZIP-1"]["confidence"], "direct")
        self.assertIn("pagination.rb", plan.recoveries["ZIP-1"]["evidence"])


class RollbackTests(MigrationFixture):
    def migrate(self):
        for n in range(1, 7):
            self.ticket(f"ZIP-{n}", transcript=f"2026-09-0{n}T00:00:00Z")
        self.ticket("ZIP-2", "blocked", reason="owner", transcript="2026-09-02T00:00:00Z")
        self.supervisor = FakeSupervisor(active=["ZIP-5", "ZIP-6"])
        return migration.apply(self.cfg, migration.build_plan(self.cfg), supervisor=self.supervisor)

    def test_rollback_restores_metadata_and_preserves_later_artifacts(self):
        snapshot = self.migrate()
        later = self.cfg.state_root / "attempts" / "ZIP-1" / "002" / "1" / "attempt.json"
        later.parent.mkdir(parents=True); later.write_text("{}")
        migration.rollback(self.cfg, snapshot.name, supervisor=self.supervisor)
        self.assertFalse((self.cfg.state_root / "cohort.json").exists())
        self.assertFalse((self.cfg.ticket_dir("ZIP-5") / "PARKED").exists())
        self.assertEqual(sorted(self.supervisor.started), ["ZIP-5", "ZIP-6"])
        self.assertTrue(later.exists())
        self.assertIsNone(state.load(self.cfg.ticket_dir("ZIP-2")).recovery["active"])

    def test_rollback_refuses_after_later_generation_unless_forced_with_audit_snapshot(self):
        snapshot = self.migrate()
        cohort.park_member(self.cfg, "ZIP-4")
        with self.assertRaises(migration.MigrationError):
            migration.rollback(self.cfg, snapshot.name, supervisor=self.supervisor)
        self.assertTrue((self.cfg.state_root / "cohort.json").exists())
        snapshots = set((self.cfg.state_root / "cohort-migrations").iterdir())
        migration.rollback(self.cfg, snapshot.name, force=True, supervisor=self.supervisor)
        self.assertEqual(len(set((self.cfg.state_root / "cohort-migrations").iterdir()) - snapshots), 1)
        self.assertFalse((self.cfg.state_root / "cohort.json").exists())

    def test_rollback_refuses_after_a_recovery_event_was_applied(self):
        snapshot = self.migrate()
        processed = self.cfg.state_root / "defects" / "events" / "processed"
        processed.mkdir(parents=True, exist_ok=True); (processed / "repair-x.json").write_text("{}")
        with self.assertRaises(migration.MigrationError):
            migration.rollback(self.cfg, snapshot.name, supervisor=self.supervisor)


if __name__ == "__main__":
    unittest.main()
