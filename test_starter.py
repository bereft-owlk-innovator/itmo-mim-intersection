"""Проверки каркаса модели (не проверяют качество оптимизации)."""

import random
import unittest

import scenario
from batch_runs import make_config, run_once, run_one
from simulation import Simulation


class StarterChecks(unittest.TestCase):
    def test_run_once_returns_placeholder_for_every_result_field(self):
        result = run_once(plan=None, seed=7, T=300, W=60,
                          demand_scale=1.0, options={})
        expected_keys = set("""
            run_id plan_id seed T W demand_scale phase_scheme controller
            peak_enabled peak_approach peak_start peak_end peak_multiplier physics
            cycle clearance ns_green ew_green
            generated completed remaining mean_queue_after_warmup
            max_queue_after_warmup final_queue delay_completed_sec
            flow_completed_per_hour conflicts conflicts_per_100_completed
            accumulated_delay_machine_sec
            mean_queue_N mean_queue_E mean_queue_S mean_queue_W
            max_queue_N max_queue_E max_queue_S max_queue_W
            mean_delay_N_sec mean_delay_E_sec mean_delay_S_sec mean_delay_W_sec
            delay_p95_sec queue_mean_last_quarter queue_mean_second_quarter
            objective_value webster_cycle_sec webster_lost_time_sec
            webster_flow_ratio conflicts_by_phase peak_max_queue
            peak_recovery_time_sec adaptive_green_extensions
        """.split())

        self.assertEqual(set(result), expected_keys)
        self.assertTrue(all(value is None for value in result.values()))

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
