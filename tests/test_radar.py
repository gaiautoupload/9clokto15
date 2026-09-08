import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import radar


class RadarTests(unittest.TestCase):
    def test_trigger_boundary_is_strict(self):
        prior = 100
        self.assertFalse((115 / prior - 1) * 100 > radar.CONFIG["trigger_pct"])
        self.assertTrue((115.01 / prior - 1) * 100 > radar.CONFIG["trigger_pct"])

    def test_core_support(self):
        core = [{"net_lots": value, "capital_share_pct": 10} for value in (10, 9, 8, -2, -1)]
        status, evidence = radar.classify(core)
        self.assertEqual(status, "CHIP_SUPPORT")
        self.assertEqual(evidence["positive_top5"], 3)


if __name__ == "__main__":
    unittest.main()
