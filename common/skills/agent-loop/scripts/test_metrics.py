import json, pathlib, tempfile, unittest
import metrics


class TornTailTests(unittest.TestCase):
    def setUp(self): self.tmp = tempfile.TemporaryDirectory(); self.root = pathlib.Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()

    def test_append_after_torn_tail_repairs_and_keeps_prior_bytes_identical(self):
        p = self.root / "metrics.jsonl"
        prior = json.dumps({"a": 1}) + "\n"
        p.write_text(prior + '{"torn": tr')
        metrics.append(self.root, {"b": 2})
        data = p.read_text()
        self.assertTrue(data.startswith(prior))
        rows = metrics.read_all(self.root)
        self.assertEqual([r.get("a", r.get("b")) for r in rows], [1, 2])
        metrics.append(self.root, {"c": 3})
        self.assertEqual(len(metrics.read_all(self.root)), 3)

    def test_read_all_skips_only_final_torn_line(self):
        p = self.root / "metrics.jsonl"; p.write_text('{"a":1}\n{"bad\n{"c":3}\n')
        with self.assertRaises(json.JSONDecodeError): metrics.read_all(self.root)

    def test_unterminated_but_valid_final_line_is_not_committed(self):
        """A syntactically valid but unterminated final object was never durably committed
        (no trailing '\\n' means the write never completed): both read_all and append's
        repair must treat it as absent, not as a row."""
        p = self.root / "metrics.jsonl"
        prior = json.dumps({"a": 1}) + "\n"
        unterminated = json.dumps({"b": 2})  # valid JSON, but no trailing newline
        p.write_text(prior + unterminated)
        rows = metrics.read_all(self.root)
        self.assertEqual(rows, [{"a": 1}])
        metrics.append(self.root, {"c": 3})
        data = p.read_text()
        self.assertTrue(data.startswith(prior))
        self.assertNotIn('"b"', data)
        rows = metrics.read_all(self.root)
        self.assertEqual([r.get("a", r.get("c")) for r in rows], [1, 3])

    def test_interior_corruption_raises_and_leaves_file_untouched(self):
        p = self.root / "metrics.jsonl"
        original = json.dumps({"a": 1}) + "\n" + "not json at all\n" + json.dumps({"c": 3}) + "\n"
        p.write_text(original)
        with self.assertRaises(metrics.MetricsCorrupt):
            metrics.append(self.root, {"d": 4})
        self.assertEqual(p.read_text(), original)


class PerStageKeyTests(unittest.TestCase):
    def setUp(self): self.tmp = tempfile.TemporaryDirectory(); self.root = pathlib.Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()

    def test_has_is_false_until_row_appended_then_true(self):
        self.assertFalse(metrics.has(self.root, "T/1@T/1/1", "worker", 0))
        metrics.append(self.root, {"attempt_id": "T/1@T/1/1", "stage_kind": "worker", "idx": 0})
        self.assertTrue(metrics.has(self.root, "T/1@T/1/1", "worker", 0))

    def test_has_distinguishes_stage_kind_and_idx(self):
        metrics.append(self.root, {"attempt_id": "T/1@T/1/1", "stage_kind": "verify", "idx": 0})
        self.assertFalse(metrics.has(self.root, "T/1@T/1/1", "verify", 1))
        self.assertFalse(metrics.has(self.root, "T/1@T/1/1", "worker", 0))
        self.assertTrue(metrics.has(self.root, "T/1@T/1/1", "verify", 0))

    def test_rows_round_trip_attempt_id_stage_kind_idx(self):
        metrics.append(self.root, {"attempt_id": "T/1@T/1/1", "stage_kind": "worker", "idx": 0})
        rows = metrics.read_all(self.root)
        self.assertEqual(rows[0]["attempt_id"], "T/1@T/1/1")
        self.assertEqual(rows[0]["stage_kind"], "worker")
        self.assertEqual(rows[0]["idx"], 0)


if __name__ == "__main__":
    unittest.main()
