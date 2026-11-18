"""Проверки каркаса модели (не проверяют качество оптимизации)."""

import random
import unittest

import scenario
from batch_runs import make_config, run_one
from simulation import Simulation


class StarterChecks(unittest.TestCase):
    def test_gui_and_batch_signals_agree_on_baseline(self):
        config = make_config(7)
        for t in [0, 12, 17.99, 18, 20.99, 21, 38.99, 39, 41.99, 42]:
            for side in "NESW":
                for lane in range(3):
                    for turn in ("left", "straight", "right"):
                        self.assertEqual(scenario.green(side, lane, turn, t),
                                         config.green(side, lane, turn, t))

    def test_green_time_parameter_changes_signal(self):
        a, b = make_config(7, ns_green=12), make_config(7, ns_green=24)
        self.assertFalse(a.green("N", 1, "straight", 18))
        self.assertTrue(b.green("N", 1, "straight", 18))

    def test_demand_scale_is_rate_multiplier(self):
        a, b = make_config(7, demand_scale=1), make_config(7, demand_scale=1.25)
        first = a.ARRIVALS["N"](random.Random(11))
        second = b.ARRIVALS["N"](random.Random(11))
        self.assertAlmostEqual(first / 1.25, second)

    def test_conservation_and_reproducibility(self):
        config = make_config(7)
        simulation = Simulation(config)
        for _ in range(240):
            simulation.step()
        self.assertEqual(simulation.generated,
                         simulation.completed + len(simulation.cars)
                         + sum(map(len, simulation.pending.values())))
        self.assertEqual(run_one(7, 60, 10), run_one(7, 60, 10))


if __name__ == "__main__":
    unittest.main()
