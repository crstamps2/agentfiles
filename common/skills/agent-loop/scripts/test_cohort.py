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
        text = source.read_text()
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
            "merged-pr": lambda t, ci, wt: ci.update(pr_state="merged"),
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


if __name__ == "__main__":
    unittest.main()
