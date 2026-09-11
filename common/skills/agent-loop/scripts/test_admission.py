import unittest
from unittest.mock import patch
import math
import admission
from config import AdmissionThresholds

VM = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               15804.
Pages active:                            471559.
Pages occupied by compressor:            407334.
Pageins:                                6490006.
"""
UPTIME = "11:30  up 20:58, 12 users, load averages: 2.17 2.35 2.49"
BATT_AC = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1234)\t100%; charged; 0:00 remaining present: true"
BATT_BAT = "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1234)\t82%; discharging; 4:10 remaining present: true"
THERM_OK = "Note: No thermal warning level has been recorded\nCPU_Speed_Limit \t= 100\n"
THERM_HOT = "CPU_Speed_Limit \t= 62\n"
TH = AdmissionThresholds(compressor_pct_max=25.0, load_per_core_max=1.0, disk_free_gb_min=20.0, require_ac_power=True, defer_max_s=60)

class ParserTests(unittest.TestCase):
    def test_compressor_pct(self):
        memsize = 24 * 1024**3
        pct = admission.parse_vm_stat(VM, memsize)
        self.assertAlmostEqual(pct, 407334 * 16384 / memsize * 100, places=3)
    def test_load1(self):
        self.assertEqual(admission.parse_uptime(UPTIME), 2.17)
    def test_batt(self):
        self.assertTrue(admission.parse_batt(BATT_AC))
        self.assertFalse(admission.parse_batt(BATT_BAT))
    def test_therm(self):
        self.assertFalse(admission.parse_therm(THERM_OK))
        self.assertTrue(admission.parse_therm(THERM_HOT))
    def test_therm_missing_line_is_unknown_not_ok(self):
        self.assertIsNone(admission.parse_therm("Note: No thermal warning level has been recorded\n"))
        self.assertIsNone(admission.parse_therm(""))
        self.assertFalse(admission.parse_therm("CPU_Speed_Limit = 100"))
        self.assertTrue(admission.parse_therm("CPU_Speed_Limit = 60"))
    def test_parse_thermal_state(self):
        # Valid states 0-3
        self.assertEqual(admission.parse_thermal_state("0\n"), 0)
        self.assertEqual(admission.parse_thermal_state("1"), 1)
        self.assertEqual(admission.parse_thermal_state("2"), 2)
        self.assertEqual(admission.parse_thermal_state("3"), 3)
        # Invalid values
        self.assertIsNone(admission.parse_thermal_state(""))
        self.assertIsNone(admission.parse_thermal_state("garbage"))
        self.assertIsNone(admission.parse_thermal_state("7"))
        self.assertIsNone(admission.parse_thermal_state("-1"))
    def test_df_wrapped_device_name_parses_second_line(self):
        # Long device name wraps to next line; numbers are on the line after
        text = "Filesystem 1024-blocks Used Available Capacity Mounted\n" + \
               "very_long_device_name_that_wraps_filesystem\n" + \
               "100 50 52428800 50% /\n"
        result = admission.parse_df(text)
        self.assertAlmostEqual(result, 50.0, places=1)
    def test_df_unparseable_is_nan_and_distinct_reason(self):
        # Header only, no data lines
        text = "Filesystem 1024-blocks Used Available Capacity Mounted\n"
        result = admission.parse_df(text)
        self.assertTrue(math.isnan(result))

class DecideTests(unittest.TestCase):
    def r(self, **kw):
        base = dict(compressor_pct=10.0, load1=2.0, cores=10, on_ac=True, therm_limited=False, disk_free_gb=100.0, pressure_level="normal", thermal_level=0, thermal_state=0)
        base.update(kw)
        return admission.Reading(**base)
    def test_ok(self):
        d = admission.decide(self.r(), TH)
        self.assertTrue(d.ok); self.assertEqual(d.reasons, [])
    def test_each_red_reason(self):
        self.assertIn("compressor", admission.decide(self.r(compressor_pct=30.0), TH).reasons[0])
        self.assertIn("load", admission.decide(self.r(load1=11.0), TH).reasons[0])
        self.assertIn("battery", admission.decide(self.r(on_ac=False), TH).reasons[0])
        self.assertIn("thermal", admission.decide(self.r(therm_limited=True), TH).reasons[0])
        self.assertIn("disk", admission.decide(self.r(disk_free_gb=5.0), TH).reasons[0])
    def test_battery_ok_when_not_required(self):
        th = AdmissionThresholds(25.0, 1.0, 20.0, False, 60)
        self.assertTrue(admission.decide(self.r(on_ac=False), th).ok)
    def test_thermal_state_primary(self):
        # thermal_state is primary: 0-1 are OK, 2-3 are red
        r = self.r(thermal_state=0, therm_limited=None, thermal_level=None); self.assertTrue(admission.decide(r, TH).ok)
        r = self.r(thermal_state=1, therm_limited=None, thermal_level=None); self.assertTrue(admission.decide(r, TH).ok)
        r = self.r(thermal_state=2, therm_limited=None, thermal_level=None); self.assertFalse(admission.decide(r, TH).ok)
        self.assertIn("thermal: NSProcessInfo state 2", admission.decide(r, TH).reasons[0])
        r = self.r(thermal_state=3, therm_limited=None, thermal_level=None); self.assertFalse(admission.decide(r, TH).ok)
        self.assertIn("thermal: NSProcessInfo state 3", admission.decide(r, TH).reasons[0])
    def test_thermal_unknown_only_when_all_sources_missing(self):
        # When all three are None, fail closed
        r = self.r(thermal_state=None, thermal_level=None, therm_limited=None)
        d = admission.decide(r, TH)
        self.assertFalse(d.ok)
        self.assertIn("thermal: unknown", d.reasons[0])
        # thermal_state=0 with others None is OK
        r = self.r(thermal_state=0, thermal_level=None, therm_limited=None)
        self.assertTrue(admission.decide(r, TH).ok)
    def test_xcpm_thermal_level_used_when_pmset_silent(self):
        r = self.r(thermal_state=None, therm_limited=None, thermal_level=0); self.assertTrue(admission.decide(r, TH).ok)
        r = self.r(thermal_state=None, therm_limited=None, thermal_level=3); self.assertFalse(admission.decide(r, TH).ok)
        r = self.r(thermal_state=None, therm_limited=None, thermal_level=None)
        self.assertIn("thermal: unknown", admission.decide(r, TH).reasons[0])
    def test_df_unparseable_distinct_reason(self):
        # NaN disk_free_gb should produce "unparseable" reason, not numeric comparison
        d = admission.decide(self.r(disk_free_gb=float("nan")), TH)
        self.assertFalse(d.ok)
        self.assertIn("disk: unknown", d.reasons)

class ProbeTests(unittest.TestCase):
    @patch("admission.subprocess.run")
    def test_probe_assembles_reading(self, run):
        def fake(argv, **kw):
            out = {"vm_stat": VM, "uptime": UPTIME}.get(argv[0])
            if argv[:2] == ["pmset", "-g"] and len(argv) > 2 and argv[2] == "batt": out = BATT_AC
            if argv[:2] == ["pmset", "-g"] and len(argv) > 2 and argv[2] == "therm": out = THERM_OK
            if argv[0] == "sysctl" and len(argv) > 2 and "hw.ncpu" in argv: out = "10\n"
            if argv[0] == "sysctl" and len(argv) > 2 and "hw.memsize" in argv: out = str(24 * 1024**3) + "\n"
            if argv[0] == "sysctl" and len(argv) > 2 and any("xcpm" in s for s in argv): out = "2\n"
            if argv[0] == "osascript": out = "1\n"
            if argv[0] == "df": out = "Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/x 100 50 52428800 50% /\n"
            m = unittest.mock.MagicMock(); m.stdout = out; m.returncode = 0; return m
        run.side_effect = fake
        r = admission.probe("/tmp")
        self.assertEqual(r.cores, 10); self.assertTrue(r.on_ac); self.assertAlmostEqual(r.disk_free_gb, 50.0, places=1); self.assertEqual(r.thermal_level, 2); self.assertEqual(r.thermal_state, 1)

if __name__ == "__main__":
    unittest.main()
