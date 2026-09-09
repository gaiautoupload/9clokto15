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
        core = [{"net_lots": value, "inventory_lots": max(value, 0), "inventory_retention": 0.8 if value > 0 else 0, "capital_share_pct": 10} for value in (10, 9, 8, -2, -1)]
        status, evidence = radar.classify(core)
        self.assertEqual(status, "CHIP_SUPPORT")
        self.assertEqual(evidence["positive_top5"], 3)
        self.assertEqual(evidence["retained_top5"], 3)

    def test_first_breakout(self):
        setup = {"range_pct": 12, "range_high": 110, "median_volume_lots": 100, "prior_max_jump_pct": 4}
        self.assertTrue(radar.is_first_breakout(111, 200, setup))
        self.assertFalse(radar.is_first_breakout(109, 300, setup))

    def test_extreme_routes(self):
        stock = {"tail_features": {"qualified_brokers": 45, "largest_capital_share_pct": 20, "recent_builder_count": 1}}
        setup = {"median_volume_lots": 100, "range_pct": 8}
        self.assertIn("BROAD_IGNITION", radar.extreme_routes(stock, setup, 600))
        self.assertIn("EARLY_SEED", radar.extreme_routes(stock, setup, 600))


if __name__ == "__main__":
    unittest.main()
