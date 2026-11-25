"""Проверки каркаса модели (не проверяют качество оптимизации)."""

import argparse
import csv
import random
import tempfile
import unittest
from pathlib import Path

import scenario
import batch_runs
from batch_runs import (
    Plan, evaluate, evaluate_grid, grid_plans, make_config, make_plan,
    mean_ci95, parse_seeds, run_once, simulate, summarize, t_critical,
)
from simulation import Simulation


class StarterChecks(unittest.TestCase):
    def test_run_once_returns_every_result_field(self):
        result = run_once(plan=None, seed=7, T=60, W=10,
                          demand_config=1.0, options={})
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
        self.assertEqual(list(result), list(batch_runs._RUN_ONCE_RESULT_KEYS))
        implemented = """run_id plan_id seed T W demand_scale cycle clearance ns_green
            ew_green generated completed remaining mean_queue_after_warmup
            max_queue_after_warmup final_queue delay_completed_sec
            flow_completed_per_hour conflicts conflicts_per_100_completed""".split()
        for key in implemented:
            self.assertIsNotNone(result[key], key)
        for key in expected_keys - set(implemented):
            self.assertIsNone(result[key], key)

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
        a, b = make_config(7, demand_scale=1), make_config(7, demand_config={"demand_scale": 1.25})
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
        self.assertEqual(run_once(None, 7, 60, 10, {"demand_scale": 1.0}, {}), run_once(None, 7, 60, 10, 1.0, {}))

    def test_arrival_journal_is_independent_of_signal_plan(self):
        journals = []
        for ns_green in (14, 22):
            simulation = Simulation(make_config(7, ns_green=ns_green))
            for _ in range(260):
                simulation.step()
            journals.append([
                (row["t_born"], row["entry_side"], row["turn"])
                for row in simulation.demand_log
            ])
        self.assertEqual(journals[0], journals[1])

    def test_rate_multiplier_scales_interarrival_time(self):
        config = make_config(7)
        simulation = Simulation(config)
        for index, side in enumerate("NESW"):
            multiplier = config.rate_multiplier(side, 0)
            self.assertEqual(multiplier, 0.95)
            raw_interval = config.ARRIVALS[side](
                random.Random(config.SEED * 1000 + index)
            )
            self.assertAlmostEqual(
                max(0.05, raw_interval / multiplier),
                simulation.next_arrival[side],
            )


class BatchRunChecks(unittest.TestCase):
    def test_plan_resolution(self):
        base = make_plan()
        self.assertEqual((base.cycle, base.ns_green, base.ew_green, base.clearance), (42, 18, 18, 3))
        self.assertEqual(base, Plan(42, 18, 18, 3))
        self.assertEqual(make_plan(ew_green=24).ns_green, 12)               # цикл остаётся 42
        self.assertEqual(make_plan(ns_green=20, ew_green=24, clearance=4).cycle, 52)
        self.assertEqual(make_plan(cycle=60, ew_green=20, clearance=4).ns_green, 32)
        self.assertEqual(make_plan(clearance=5).ew_green, 14)
        self.assertEqual(make_plan(cycle=42, ns_green=18, ew_green=18, clearance=3), base)
        with self.assertRaises(ValueError):
            make_plan(cycle=42, ns_green=18, ew_green=20)                    # противоречие
        with self.assertRaises(ValueError):
            make_plan(ns_green=5)                                            # зелёный < 6 с
        with self.assertRaises(ValueError):
            Plan(42, 18, 18, -3)

    def test_config_takes_all_plan_parameters(self):
        config = make_config(7, cycle=60, ew_green=20, clearance=4)
        self.assertEqual((config.CYCLE, config.NS_GREEN, config.EW_GREEN, config.CLEARANCE),
                         (60, 32, 20, 4))
        self.assertTrue(config.green("N", 1, "straight", 31.9))
        self.assertFalse(config.green("N", 1, "straight", 32))
        self.assertFalse(config.green("E", 1, "straight", 35.9))             # 32 + clearance 4
        self.assertTrue(config.green("E", 1, "straight", 36))
        self.assertFalse(config.green("E", 1, "straight", 56))

    def test_grid_matches_assignment(self):
        plans = grid_plans()
        self.assertEqual(len(plans), 90)
        self.assertEqual(len({p.plan_id for p in plans}), 90)
        for plan in plans:
            self.assertAlmostEqual(plan.cycle, plan.ns_green + plan.ew_green + 6)
        self.assertEqual((plans[0].cycle, plans[0].ns_green), (30, 7.2))
        self.assertEqual((plans[-1].cycle, plans[-1].ns_green), (120, 79.8))

    def test_seed_parsing(self):
        self.assertEqual(parse_seeds("7"), [7])
        self.assertEqual(parse_seeds("201-204"), [201, 202, 203, 204])
        self.assertEqual(parse_seeds("1,5,9-10"), [1, 5, 9, 10])
        for bad in ("10-5", "a", "1-", "-3", ""):
            with self.assertRaises(argparse.ArgumentTypeError, msg=bad):
                parse_seeds(bad)

    def test_warmup_is_applied_to_every_indicator(self):
        full = simulate(None, 7, 120, 0, 1.0, demand_log=True)
        warm = simulate(None, 7, 120, 40, 1.0, demand_log=True)
        before_w = sum(r["t_born"] <= 40 for r in full.demand_log)
        self.assertEqual(full.row["generated"] - warm.row["generated"], before_w)
        self.assertGreater(before_w, 0)
        self.assertLess(warm.row["completed"], full.row["completed"])
        self.assertLess(warm.row["conflicts"], full.row["conflicts"])
        self.assertAlmostEqual(warm.row["flow_completed_per_hour"], warm.row["completed"] * 3600 / 80, 4)
        # Без прогрева показатели совпадают с итогами самой модели.
        config = make_config(7)
        sim = Simulation(config)
        for _ in range(120 * 20):
            sim.step(0.05)
        self.assertEqual(full.row["generated"], sim.generated)
        self.assertEqual(full.row["completed"], sim.completed)
        self.assertEqual(full.row["conflicts"], sim.conflicts)
        self.assertAlmostEqual(full.row["delay_completed_sec"], sim.total_delay / sim.completed, 5)

    def test_timeseries_is_consistent_with_row(self):
        out = simulate(None, 7, 90, 30, {"demand_scale": 1.0}, timeseries=True)
        series = out.timeseries
        self.assertEqual([r["t"] for r in series], list(range(1, 91)))
        for r in series:
            self.assertEqual(r["queue"], sum(r[f"queue_{s}"] for s in "NESW"))
        observed = [r["queue"] for r in series if r["t"] >= 30]
        self.assertAlmostEqual(out.row["mean_queue_after_warmup"], sum(observed) / len(observed), 5)
        self.assertEqual(out.row["max_queue_after_warmup"], max(observed))
        self.assertEqual(out.row["final_queue"], series[-1]["queue"])
        self.assertIsNone(simulate(None, 7, 60, 10, 1.0).timeseries)

    def test_run_arguments_are_validated(self):
        for kwargs in ({"T": 60, "W": 60}, {"T": 60, "W": -1}, {"T": 3601, "W": 0},
                       {"T": 60.5, "W": 0}):
            with self.assertRaises(ValueError, msg=kwargs):
                run_once(None, 7, demand_config={"demand_scale": 1.0}, options={}, **kwargs)
        with self.assertRaises(ValueError):
            run_once(None, 7, 60, 10, 2.0, {})
        with self.assertRaises(ValueError):
            run_once(None, 7, 60, 10, 1.0, {"controller": "adaptive"})       # молча не игнорируем

    def test_demand_log_is_not_truncated_on_long_runs(self):
        log = simulate(None, 7, 500, 0, 1.5, demand_log=True).demand_log
        self.assertGreater(len(log), 900)
        self.assertEqual([r["vehice_id"] for r in log], list(range(1, len(log) + 1)))

    def test_evaluate_returns_row_per_seed_and_parallel_matches_serial(self):
        plan = Plan(cycle=36, ns_green=12, ew_green=18, clearance=3)
        serial = evaluate(plan, [201, 202, 203], T=60, W=10)
        self.assertEqual([r["seed"] for r in serial], [201, 202, 203])
        self.assertEqual(serial, evaluate(plan, range(201, 204), T=60, W=10, workers=2))
        self.assertEqual(serial[1], run_once(plan, 202, 60, 10, {"demand_scale": 1.0}, {}))
        grid = evaluate_grid([plan, None], [201, 202], T=60, W=10, workers=2)
        self.assertEqual([(r["plan_id"], r["seed"]) for r in grid],
                         [(plan.plan_id, 201), (plan.plan_id, 202),
                          (make_plan().plan_id, 201), (make_plan().plan_id, 202)])
        self.assertEqual(grid[:2], serial[:2])
        with self.assertRaises(ValueError):
            evaluate(plan, [201, 201], T=60, W=10)

    def test_student_interval(self):
        self.assertAlmostEqual(t_critical(9), 2.2622, 4)
        self.assertAlmostEqual(t_critical(40), 2.0211, 3)
        mean, std, low, high = mean_ci95([1.0, 2.0, 3.0, 4.0])
        self.assertAlmostEqual(mean, 2.5)
        self.assertAlmostEqual(std, 1.2909944, 6)
        self.assertAlmostEqual(high - mean, 3.1824 * std / 2, 6)
        self.assertAlmostEqual(mean - low, high - mean)
        self.assertEqual(mean_ci95([5.0]), (5.0, None, None, None))

    def test_summary_schema_and_values(self):
        plan = Plan(36, 12, 18, 3)
        rows = evaluate(plan, [1, 2, 3], T=60, W=10)
        (summary,) = summarize(rows)
        self.assertEqual(list(summary), list(batch_runs.SUMMARY_COLUMNS))
        self.assertEqual(summary["n"], 3)
        self.assertEqual(summary["plan_id"], plan.plan_id)
        values = [r["generated"] for r in rows]
        self.assertAlmostEqual(summary["generated_mean"], sum(values) / 3, 5)
        self.assertLess(summary["generated_ci95_low"], summary["generated_mean"])
        self.assertIsNone(summary["delay_p95_sec_mean"])                     # не реализован -> пусто
        self.assertIsNone(summary["paired_n"])
        self.assertIn("seeds=1-3", summary["notes"])

    def test_cli_writes_one_row_per_run_and_all_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            batch_runs.main([
                "--seconds", "60", "--warmup", "10", "--seeds", "5-7", "--ew-green", "20",
                "--clearance", "4", "--workers", "2", "--timeseries", str(tmp / "ts.csv"),
                "--out", str(tmp / "runs.csv"), "--summary", str(tmp / "summary.csv"),
                "--demand-log", str(tmp / "demand.csv"),
            ])
            def read(name):
                with (tmp / name).open(encoding="utf-8") as f:
                    return list(csv.DictReader(f))
            runs = read("runs.csv")
            self.assertEqual([r["seed"] for r in runs], ["5", "6", "7"])     # последняя строка не теряется
            self.assertEqual({(r["cycle"], r["ns_green"], r["ew_green"], r["clearance"]) for r in runs},
                             {("42", "14", "20", "4")})
            self.assertEqual(list(runs[0]), list(batch_runs._RUN_ONCE_RESULT_KEYS))
            self.assertEqual(len(read("summary.csv")), 1)
            self.assertEqual(len(read("ts.csv")), 3 * 60)
            text = (tmp / "demand.csv").read_text(encoding="utf-8")
            self.assertEqual(text.count("Launch"), 3)

    def test_cli_rejects_bad_arguments(self):
        for argv in (["--seconds", "3601"], ["--seconds", "100", "--warmup", "100"],
                     ["--seeds", "1", "1"], ["--cycle", "40", "--ns-green", "20", "--ew-green", "20"],
                     ["--grid", "--cycle", "60"], ["--ns-green", "3"]):
            with self.assertRaises(SystemExit, msg=argv):
                batch_runs.main(argv)


if __name__ == "__main__":
    unittest.main()
