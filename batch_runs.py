"""Многократные прогоны основной модели. Только стандартная библиотека Python.

Пример:
    python batch_runs.py --seconds 300 --seeds 7 17 27 --ns-green 18 --out results/baseline.csv

Важно: граф берётся из scenario.INITIAL_CONNECTIONS, а не из connections.json.
"""

import argparse
import csv
import math
from pathlib import Path
from types import SimpleNamespace

import scenario
from simulation import Simulation


DT = 0.05

_RUN_ONCE_RESULT_KEYS = (
    "run_id", "plan_id", "seed", "T", "W", "demand_scale",
    "phase_scheme", "controller", "peak_enabled", "peak_approach",
    "peak_start", "peak_end", "peak_multiplier", "physics",
    "cycle", "clearance", "ns_green", "ew_green",
    "generated", "completed", "remaining", "mean_queue_after_warmup",
    "max_queue_after_warmup", "final_queue", "delay_completed_sec",
    "flow_completed_per_hour", "conflicts", "conflicts_per_100_completed",
    "accumulated_delay_machine_sec",
    "mean_queue_N", "mean_queue_E", "mean_queue_S", "mean_queue_W",
    "max_queue_N", "max_queue_E", "max_queue_S", "max_queue_W",
    "mean_delay_N_sec", "mean_delay_E_sec", "mean_delay_S_sec",
    "mean_delay_W_sec", "delay_p95_sec", "queue_mean_last_quarter",
    "queue_mean_second_quarter", "objective_value", "webster_cycle_sec",
    "webster_lost_time_sec", "webster_flow_ratio", "conflicts_by_phase",
    "peak_max_queue", "peak_recovery_time_sec", "adaptive_green_extensions",
)

def scale_interval(t_start, duration, side, scale_config):
    demand_scale = scale_config["demand_scale"]
    if not all(k in scale_config.keys() for k in {"peak_start", "peak_approach", "peak_end", "peak_factor"}) or side != scale_config["peak_approach"]: return duration / demand_scale

    peak_start = max(0, scale_config["peak_start"] - t_start)
    peak_end = max(0, scale_config["peak_end"] - t_start)
    peak_factor = scale_config["peak_factor"]

    if duration <= peak_start * demand_scale: return duration / demand_scale
    duration -= peak_start * demand_scale
    if duration <= (peak_end - peak_start) * demand_scale * peak_factor: return peak_start + duration / demand_scale / peak_factor
    duration -= (peak_end - peak_start) * demand_scale * peak_factor
    
    return peak_end + duration / demand_scale


def make_config(seed, ns_green=None, cycle=None, scale_config={"demand_scale": 1.0}):
    # Отдельный объект конфигурации для каждого прогона; функции берутся из scenario.py.
    config = SimpleNamespace(
        **{name: value for name, value in vars(scenario).items() if not name.startswith("__")}
    )
    config.SEED = seed
    config.CYCLE = scenario.CYCLE if cycle is None else cycle
    config.NS_GREEN = scenario.NS_GREEN if ns_green is None else ns_green
    config.EW_GREEN = config.CYCLE - 2 * config.CLEARANCE - config.NS_GREEN
    if config.NS_GREEN < 6 or config.EW_GREEN < 6:
        raise ValueError("Для обоих направлений требуется не менее 6 с зелёного")
    if not (0.5 <= scale_config["demand_scale"] <= 1.5):
        raise ValueError("demand_scale должен быть от 0.5 до 1.5")
    # demand_scale — множитель интенсивности, поэтому интервалы делятся на него.
    config.ARRIVALS = {
        side: (lambda rng, t, side=side, generate=generate: scale_interval(t, generate(rng), side, scale_config))
        for side, generate in scenario.ARRIVALS.items()
    }
    # Исходная функция scenario.green читает параметры из файла. Для пакетных
    # запусков используем эквивалентный закон с параметрами текущего прогона.
    config.green = lambda side, lane, turn, t: scenario.signal_for(config, side, lane, turn, t)
    return config


def run_once(plan, seed, T, W, demand_scale, options) -> dict:
    """Return the complete result shape until the one-run API is implemented."""
    return {key: None for key in _RUN_ONCE_RESULT_KEYS}


def run_one(seed, seconds, warmup, ns_green=None, cycle=None, demand_scale=1.0):
    config = make_config(seed, ns_green, cycle, demand_scale)
    sim = Simulation(config, config.INITIAL_CONNECTIONS)
    n_steps = int(round(seconds / DT))
    for _ in range(n_steps):
        sim.step(DT)
    rows = [row for row in sim.history if row["t"] >= warmup]
    metrics = sim.metrics()
    avg_queue = sum(row["queue"] for row in rows) / len(rows) if rows else float("nan")
    max_queue = max((row["queue"] for row in rows), default=0)
    return {
        "rows":{
            "seed": seed,
            "seconds": seconds,
            "cycle": config.CYCLE,
            "clearance": config.CLEARANCE,
            "ns_green": config.NS_GREEN,
            "ew_green": config.EW_GREEN,
            "demand_scale": demand_scale,
            "generated": metrics["generated"],
            "completed": metrics["completed"],
            "remaining": metrics["generated"] - metrics["completed"],
            "mean_queue_after_warmup": round(avg_queue, 2),
            "max_queue_after_warmup": max_queue,
            "final_queue": metrics["queue"],
            "delay_completed_sec": metrics["delay"],
            "flow_completed_per_hour": metrics["flow"],
            "conflicts": metrics["conflicts"],
            "conflicts_per_100_completed": round(
                100 * metrics["conflicts"] / max(1, metrics["completed"]), 2
            ),
        },
        "demand_log": list(sim.demand_log),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seconds", type=int, default=240, help="Сколько модельных секунд")
    p.add_argument("--warmup", type=int, default=60, help="Начало усреднения очереди")
    p.add_argument("--seeds", type=int, nargs="+", default=[7, 17, 27])
    p.add_argument("--ns-green", type=int, default=None, help="Зелёный север–юг; иначе из scenario.py")
    p.add_argument("--cycle", type=int, default=None, help="Общий цикл, с; иначе из scenario.py")
    p.add_argument("--demand-scale", type=float, default=1.0,
                   help="Множитель интенсивности всех входящих потоков (0.5–1.5)")
    p.add_argument("--out", type=Path, default=Path("results/results.csv"))
    p.add_argument("--demand-log", type=Path, default=Path("results/demand_log.csv"))
    
    p.add_argument("--peak-start", type=int, help="Время начала пиковового спроса")
    p.add_argument("--peak-approach", choices=list(scenario.SIDES), help="Сторона с которой происходит пиковый спрос")
    p.add_argument("--peak-end", type=int, help="Время начала пиковового спроса")
    p.add_argument("--peak-factor", type=float,
                   help="Множитель интенсивности при пиковом спроса (0.5–1.5)")

    a = p.parse_args()
    if not (60 <= a.seconds <= 3600) or not (0 <= a.warmup < a.seconds):
        p.error("Требуется 60 <= seconds <= 3600 и 0 <= warmup < seconds")
    if not getattr(scenario, "COLLECT_STATS", False):
        p.error("Для сравнения нужна COLLECT_STATS = True")
    if not getattr(scenario, "COUNT_CONFLICTS", False):
        p.error("Для сравнения нужна COUNT_CONFLICTS = True")
    if a.cycle is not None and not (24 <= a.cycle <= 180):
        p.error("Требуется 24 <= cycle <= 180")
    if not (0.5 <= a.demand_scale <= 1.5):
        p.error("Требуется 0.5 <= demand-scale <= 1.5")
    if a.peak_factor is not None and not (0.5 <= a.peak_factor <= 1.5):
        p.error("Требуется 0.5 <= peak-factor <= 1.5")
    try:
        results = [run_one(seed, a.seconds, a.warmup, a.ns_green,
                        a.cycle, {k: getattr(a, k) for k in {"demand_scale", "peak_start", "peak_approach", "peak_end", "peak_factor"}}) for seed in a.seeds]
        rows = [r["rows"] for r in results]
        demand_log = [r["demand_log"] for r in results]
    except ValueError as exc:
        p.error(str(exc))

    a.out.parent.mkdir(parents=True, exist_ok=True)
    with a.out.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows[:-1])
    print(f"Сохранено: {a.out}")

    a.demand_log.parent.mkdir(parents=True, exist_ok=True)
    with a.demand_log.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(demand_log[0][0]))
        writer.writeheader()
        for i, launch in enumerate(demand_log):
            f.write(f"Launch {i + 1}\n")
            writer.writerows(launch[:-1])
    print(f"Журнал входного спроса: {a.demand_log}")
    
    print("seed | generated | completed | avg queue | delay, s | conflicts | conflicts/100")
    for r in rows:
        print(f"{r['seed']:4d} | {r['generated']:9d} | {r['completed']:9d} | "
              f"{r['mean_queue_after_warmup']:9.2f} | {r['delay_completed_sec']:8.2f} | "
              f"{r['conflicts']:9d} | {r['conflicts_per_100_completed']:13.2f}")
    print("Предупреждение: delay усредняется только по завершившим поездку автомобилям.")
    print("Идентичный seed не гарантирует одинаковые потоки между РАЗНЫМИ сценариями.")


if __name__ == "__main__":
    main()
