import json, pathlib, tempfile, unittest
import metrics

class TornTailTests(unittest.TestCase):
    def setUp(self): self.tmp = tempfile.TemporaryDirectory(); self.root = pathlib.Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()
    def test_append_after_torn_tail_repairs_and_stays_readable(self):
        p = self.root / "metrics.jsonl"
        p.write_text(json.dumps({"a": 1}) + "\n" + '{"torn": tr')
        metrics.append(self.root, {"b": 2})
        rows = metrics.read_all(self.root)
        self.assertEqual([r.get("a", r.get("b")) for r in rows], [1, 2])
        metrics.append(self.root, {"c": 3})
        self.assertEqual(len(metrics.read_all(self.root)), 3)
    def test_read_all_skips_only_final_torn_line(self):
        p = self.root / "metrics.jsonl"; p.write_text('{"a":1}\n{"bad\n{"c":3}\n')
        with self.assertRaises(json.JSONDecodeError): metrics.read_all(self.root)

if __name__ == "__main__":
    unittest.main()
