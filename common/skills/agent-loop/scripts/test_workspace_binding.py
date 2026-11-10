"""Durable bindings must not be lost or reinterpreted by old state writers."""
import dataclasses
import pathlib
import tempfile
import unittest

import state
import workspace_binding as wb


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tdir = pathlib.Path(self.tmp.name) / "ZIP-1"
        self.binding = wb.Binding(backend="orca", key="ZIP-1", worktree="/workspaces/native/zip-1",
                                  branch="internal/zip-1", workspace_id="repo::/workspaces/native/zip-1", repo_id="repo",
                                  agent={"kind": "chat", "handle": "chat-1", "session_id": "session-1"},
                                  terminals={"setup": "term-1", "dev": "term-2"}, views={"reference": "page-1"})

    def loaded(self) -> wb.Binding:
        binding = wb.read(self.tdir)
        if binding is None:
            self.fail("Expected a persisted binding")
        return binding

    def test_round_trip_retains_native_ids_and_operations(self):
        self.binding.operations["agent"] = {"status": "done", "operation_id": "operation-1"}
        wb.write(self.tdir, self.binding)
        loaded = self.loaded()
        self.assertEqual(dataclasses.asdict(loaded), dataclasses.asdict(self.binding))
        self.assertEqual((self.tdir / "workspace.json").stat().st_mode & 0o777, 0o600)

    def test_legacy_state_save_does_not_erase_binding(self):
        wb.write(self.tdir, self.binding)
        state.save(self.tdir, state.Ticket(key="ZIP-1", state="paused"))
        t = state.load(self.tdir)
        t.state = "implement"
        state.save(self.tdir, t)
        self.assertEqual(self.loaded().agent["session_id"], "session-1")
        self.assertEqual(state.load(self.tdir).state, "implement")

    def test_corrupt_or_unknown_binding_fails_closed(self):
        self.tdir.mkdir()
        for text in ("{", "[]", '{"version":2}', '{"version":1,"backend":"other","key":"ZIP-1"}'):
            with self.subTest(text=text):
                (self.tdir / "workspace.json").write_text(text)
                with self.assertRaises(wb.BindingError):
                    wb.read(self.tdir)

    def test_pending_mutation_survives_restart(self):
        self.binding.phase = "uncertain"
        self.binding.operations["agent"] = {"status": "pending", "operation_id": "original", "payload": ["agent", "launch"]}
        wb.write(self.tdir, self.binding)
        self.assertEqual(self.loaded().operations["agent"]["operation_id"], "original")
        self.assertEqual(self.loaded().phase, "uncertain")

    def test_missing_file_is_legacy_not_corruption(self):
        self.assertIsNone(wb.read(self.tdir))

    def test_cannot_rebind_ticket_or_replace_known_workspace(self):
        wb.write(self.tdir, self.binding)
        for changes in ({"backend": "cmux"}, {"key": "ZIP-2"}, {"workspace_id": "other"}, {"worktree": "/workspaces/other"}):
            with self.subTest(changes=changes), self.assertRaises(wb.BindingError):
                wb.write(self.tdir, dataclasses.replace(self.binding, **changes))
        self.assertEqual(self.loaded().workspace_id, "repo::/workspaces/native/zip-1")

    def test_chat_requires_actual_session_identity(self):
        self.binding.agent = {"kind": "chat", "handle": "chat-1"}
        with self.assertRaises(wb.BindingError):
            wb.write(self.tdir, self.binding)

    def test_wrong_ticket_and_symlink_records_are_rejected(self):
        other = self.tdir.with_name("ZIP-2")
        wb.write(other, dataclasses.replace(self.binding, key="ZIP-2"))
        self.tdir.mkdir()
        (self.tdir / "workspace.json").symlink_to(other / "workspace.json")
        with self.assertRaises(wb.BindingError):
            wb.read(self.tdir)

    def test_malformed_nested_resources_are_rejected(self):
        for changes in ({"terminals": []}, {"terminals": {"setup": 3}}, {"operations": {"setup": None}},
                        {"worktree": "relative/path"}, {"environment": "paired-host"}, {"version": True}):
            with self.subTest(changes=changes), self.assertRaises(wb.BindingError):
                wb.write(self.tdir, dataclasses.replace(self.binding, **changes))
