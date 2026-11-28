"""Пакетные прогоны основной модели. Только стандартная библиотека Python.

Примеры:
    uv run python batch_runs.py --seconds 3600 --warmup 300 --seeds 101-110 --timeseries
    uv run python batch_runs.py --seconds 600 --seeds 7 17 27 --cycle 60 --ew-green 20 --clearance 4
    uv run python batch_runs.py --grid --seconds 3600 --warmup 300 --seeds 201-203 --workers 8

Из скриптов (запускать из корня репозитория: uv run python -m scripts.<имя>):
    from batch_runs import Plan, evaluate
    rows = evaluate(Plan(cycle=60, ns_green=24, ew_green=30, clearance=3), range(201, 211))

Важно: граф берётся из scenario.INITIAL_CONNECTIONS, а не из connections.json.
"""

import argparse
import csv
import math
import multiprocessing
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple

import scenario
from simulation import SIDES, Simulation


DT = 0.05
STEPS_PER_SECOND = round(1 / DT)
MIN_HORIZON, MAX_HORIZON = (
    60,
    3600,
)  # границы горизонта для CLI; API принимает 1..MAX_HORIZON
MIN_GREEN = 6
RESULTS_DIR = Path("results")

# Сетка задания 4: CYCLE 30..120 с шагом 10, доля зелёного NS 0,30..0,70 с шагом 0,05
# от CYCLE - 2*CLEARANCE.
GRID_CYCLES = tuple(range(30, 121, 10))
GRID_SHARES = tuple(round(0.30 + 0.05 * i, 2) for i in range(9))

_META_KEYS = (
    "run_id",
    "plan_id",
    "seed",
    "T",
    "W",
    "demand_scale",
    "phase_scheme",
    "controller",
    "peak_enabled",
    "peak_approach",
    "peak_start",
    "peak_end",
    "peak_multiplier",
    "physics",
    "cycle",
    "clearance",
    "ns_green",
    "ew_green",
)
_METRIC_KEYS = (
    "generated",
    "completed",
    "remaining",
    "mean_queue_after_warmup",
    "max_queue_after_warmup",
    "final_queue",
    "delay_completed_sec",
    "flow_completed_per_hour",
    "conflicts",
    "conflicts_per_100_completed",
    "accumulated_delay_machine_sec",
    "mean_queue_N",
    "mean_queue_E",
    "mean_queue_S",
    "mean_queue_W",
    "max_queue_N",
    "max_queue_E",
    "max_queue_S",
    "max_queue_W",
    "mean_delay_N_sec",
    "mean_delay_E_sec",
    "mean_delay_S_sec",
    "mean_delay_W_sec",
    "delay_p95_sec",
    "queue_mean_last_quarter",
    "queue_mean_second_quarter",
    "objective_value",
    "webster_cycle_sec",
    "webster_lost_time_sec",
    "webster_flow_ratio",
    "conflicts_by_phase",
    "peak_max_queue",
    "peak_recovery_time_sec",
    "adaptive_green_extensions",
)
_RUN_ONCE_RESULT_KEYS = _META_KEYS + _METRIC_KEYS

SUMMARY_GROUP_KEYS = (
    "plan_id",
    "phase_scheme",
    "controller",
    "physics",
    "demand_scale",
    "T",
    "W",
)
# Агрегируются все числовые показатели; ненаписанные пока остаются пустыми.
SUMMARY_METRICS = tuple(
    k for k in _METRIC_KEYS if k not in ("objective_value", "conflicts_by_phase")
)
_STATS = ("mean", "std", "ci95_low", "ci95_high")
SUMMARY_COLUMNS = (
    *SUMMARY_GROUP_KEYS,
    "n",
    *(f"{m}_{s}" for m in SUMMARY_METRICS for s in _STATS),
    "paired_with_plan_id",
    "paired_n",
    "paired_difference_mean",
    "paired_difference_std",
    "paired_difference_ci95_low",
    "paired_difference_ci95_high",
    "objective_name",
    "objective_value",
    "notes",
)
TIMESERIES_COLUMNS = (
    "run_id",
    "plan_id",
    "seed",
    "t",
    "queue",
    *(f"queue_{s}" for s in SIDES),
)
DEMAND_LOG_COLUMNS = ("vehice_id", "t_born", "entry_side", "turn")

# Двусторонний 95% квантиль Стьюдента для df = 1..30.
_T975 = (
    12.7062,
    4.3027,
    3.1824,
    2.7764,
    2.5706,
    2.4469,
    2.3646,
    2.3060,
    2.2622,
    2.2281,
    2.2010,
    2.1788,
    2.1604,
    2.1448,
    2.1314,
    2.1199,
    2.1098,
    2.1009,
    2.0930,
    2.0860,
    2.0796,
    2.0739,
    2.0687,
    2.0639,
    2.0595,
    2.0555,
    2.0518,
    2.0484,
    2.0452,
    2.0423,
)


def _num(x):
    """42.0 -> 42, чтобы в CSV и plan_id не появлялись лишние «.0»."""
    x = float(x)
    return int(x) if x.is_integer() else x


# --------------------------------------------------------------------------- план


@dataclass(frozen=True)
class Plan:
    """План светофора: CYCLE = NS_GREEN + EW_GREEN + 2 * CLEARANCE."""

    cycle: float
    ns_green: float
    ew_green: float
    clearance: float

    def __post_init__(self):
        values = (self.cycle, self.ns_green, self.ew_green, self.clearance)
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
            raise ValueError("Параметры плана должны быть конечными числами")
        if self.clearance < 0:
            raise ValueError("clearance не может быть отрицательным")
        if self.ns_green < MIN_GREEN or self.ew_green < MIN_GREEN:
            raise ValueError(
                f"Для обоих направлений требуется не менее {MIN_GREEN} с зелёного"
            )
        if not math.isclose(
            self.cycle, self.ns_green + self.ew_green + 2 * self.clearance, abs_tol=1e-6
        ):
            raise ValueError(
                f"cycle ({self.cycle:g}) != ns_green ({self.ns_green:g}) + "
                f"ew_green ({self.ew_green:g}) + 2*clearance ({self.clearance:g})"
            )

    @property
    def plan_id(self):
        return (
            f"cycle-{_num(self.cycle)!r}_ns-{_num(self.ns_green)!r}_"
            f"ew-{_num(self.ew_green)!r}_clearance-{_num(self.clearance)!r}"
        )


def make_plan(cycle=None, ns_green=None, ew_green=None, clearance=None):
    """Достраивает план: недостающее берётся из scenario.py или выводится из равенства цикла."""
    clearance = scenario.CLEARANCE if clearance is None else clearance
    if cycle is None and ns_green is not None and ew_green is not None:
        cycle = ns_green + ew_green + 2 * clearance
    if cycle is None:
        cycle = scenario.CYCLE
    if ns_green is None and ew_green is None:
        ns_green = scenario.NS_GREEN
    if ns_green is None:
        ns_green = cycle - 2 * clearance - ew_green
    if ew_green is None:
        ew_green = cycle - 2 * clearance - ns_green
    return Plan(*map(_num, (cycle, ns_green, ew_green, clearance)))


def as_plan(plan):
    """None -> базовый план scenario.py; Plan -> как есть; dict -> make_plan(**dict)."""
    if plan is None:
        return make_plan()
    if isinstance(plan, Plan):
        return plan
    if isinstance(plan, dict):
        return make_plan(**plan)
    raise ValueError(
        f"plan: ожидается None, Plan или dict, получено {type(plan).__name__}"
    )


def grid_plans(cycles=GRID_CYCLES, shares=GRID_SHARES, clearance=None):
    """Планы сетки З4: доля NS считается от CYCLE - 2*CLEARANCE."""
    clearance = scenario.CLEARANCE if clearance is None else clearance
    plans = []
    for cycle in cycles:
        usable = cycle - 2 * clearance
        for share in shares:
            ns = round(share * usable, 4)
            plans.append(make_plan(cycle, ns, round(usable - ns, 4), clearance))
    return plans


# ------------------------------------------------------------------- конфигурация


def _config_for(seed, plan, demand_scale):
    # Отдельный объект конфигурации для каждого прогона; функции берутся из scenario.py.
    if not getattr(scenario, "COLLECT_STATS", False):
        raise ValueError("Для сравнения нужна COLLECT_STATS = True")
    if not getattr(scenario, "COUNT_CONFLICTS", False):
        raise ValueError("Для сравнения нужна COUNT_CONFLICTS = True")
    if not (0.5 <= demand_scale <= 1.5):
        raise ValueError("demand_scale должен быть от 0.5 до 1.5")
    config = SimpleNamespace(
        **{
            name: value
            for name, value in vars(scenario).items()
            if not name.startswith("__")
        }
    )
    config.SEED = seed
    config.CYCLE = plan.cycle
    config.NS_GREEN = plan.ns_green
    config.EW_GREEN = plan.ew_green
    config.CLEARANCE = plan.clearance
    config.DEMAND_LOG_LIMIT = (
        None  # по умолчанию журнал хранит только 900 последних записей
    )
    # demand_scale — множитель интенсивности, поэтому интервалы делятся на него.
    config.ARRIVALS = {
        side: (lambda rng, generate=generate: generate(rng) / demand_scale)
        for side, generate in scenario.ARRIVALS.items()
    }
    # Исходная функция scenario.green читает параметры из файла. Для пакетных
    # запусков используем эквивалентный закон с параметрами текущего прогона.
    config.green = lambda side, lane, turn, t: scenario.signal_for(
        config, side, lane, turn, t
    )
    return config


def make_config(
    seed, ns_green=None, cycle=None, demand_scale=1.0, ew_green=None, clearance=None
):
    return _config_for(
        seed, make_plan(cycle, ns_green, ew_green, clearance), demand_scale
    )


# ----------------------------------------------------------------- единичный прогон


class RunOutput(NamedTuple):
    row: dict
    timeseries: "list | None"
    demand_log: "list | None"


def _check_run_args(seed, T, W):
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed должен быть целым числом")
    if T != int(T) or W != int(W):
        raise ValueError("T и W задаются целым числом секунд")
    if not (1 <= T <= MAX_HORIZON) or not (0 <= W < T):
        raise ValueError(f"Требуется 1 <= T <= {MAX_HORIZON} и 0 <= W < T")


def _queues(sim):
    """Очередь по въездам: ожидающие вне схемы и машины со скоростью ниже 1 (как sim.metrics)."""
    queue = {side: len(sim.pending[side]) for side in SIDES}
    for car in sim.cars:
        if car.speed < 1:
            queue[car.side] += 1
    return queue


def _counters(sim):
    return SimpleNamespace(
        generated=sim.generated,
        completed=sim.completed,
        conflicts=sim.conflicts,
        delay=sim.total_delay,
    )


def simulate(
    plan,
    seed,
    T,
    W,
    demand_scale=1.0,
    options=None,
    *,
    timeseries=False,
    demand_log=False,
):
    """Один прогон; помимо строки результата может вернуть ряд очереди и журнал спроса."""
    plan = as_plan(plan)
    if options:
        raise ValueError(f"Параметры options пока не поддерживаются: {sorted(options)}")
    _check_run_args(seed, T, W)
    T, W = int(T), int(W)
    config = _config_for(seed, plan, demand_scale)
    sim = Simulation(config, config.INITIAL_CONNECTIONS)

    start = _counters(sim)  # состояние на t = 0, если W == 0
    series = []  # секундные отсчёты (t, суммарная и по въездам)
    waiting_steps = 0
    for step in range(1, T * STEPS_PER_SECOND + 1):
        sim.step(DT)
        if step > W * STEPS_PER_SECOND:
            waiting_steps += sum(_queues(sim).values())
        if step == W * STEPS_PER_SECOND:
            start = _counters(sim)  # всё, что накоплено до W, из показателей вычитается
        if step % STEPS_PER_SECOND == 0:
            queue = _queues(sim)
            series.append((step // STEPS_PER_SECOND, sum(queue.values()), queue))
    end = _counters(sim)

    observed = ([0] if W == 0 else []) + [total for t, total, _ in series if t >= W]
    generated = end.generated - start.generated
    completed = end.completed - start.completed
    conflicts = end.conflicts - start.conflicts
    row = dict.fromkeys(_RUN_ONCE_RESULT_KEYS)
    row.update(
        run_id=f"{plan.plan_id}-seed-{seed}",
        plan_id=plan.plan_id,
        seed=seed,
        T=T,
        W=W,
        demand_scale=demand_scale,
        cycle=plan.cycle,
        clearance=plan.clearance,
        ns_green=plan.ns_green,
        ew_green=plan.ew_green,
        generated=generated,
        completed=completed,
        remaining=generated - completed,
        mean_queue_after_warmup=round(sum(observed) / len(observed), 6),
        max_queue_after_warmup=max(observed),
        final_queue=series[-1][1],
        accumulated_delay_machine_sec=round(waiting_steps * DT, 6),
        delay_completed_sec=(
            round((end.delay - start.delay) / completed, 6) if completed else None
        ),
        flow_completed_per_hour=round(completed * 3600 / (T - W), 6),
        conflicts=conflicts,
        conflicts_per_100_completed=round(100 * conflicts / completed, 6)
        if completed
        else None,
    )
    rows = None
    if timeseries:
        rows = [
            {
                "run_id": row["run_id"],
                "plan_id": plan.plan_id,
                "seed": seed,
                "t": t,
                "queue": total,
                **{f"queue_{s}": q[s] for s in SIDES},
            }
            for t, total, q in series
        ]
    return RunOutput(row, rows, list(sim.demand_log) if demand_log else None)


def run_once(plan, seed, T, W, demand_scale, options) -> dict:
    """Один прогон одного плана с одним SEED; показатели считаются на [W, T]."""
    return simulate(plan, seed, T, W, demand_scale, options).row


# ------------------------------------------------------------------ серии прогонов


def _check_seeds(seeds):
    seeds = list(seeds)
    if not seeds:
        raise ValueError("Нужен хотя бы один SEED")
    if len(set(seeds)) != len(seeds):
        raise ValueError("SEED не должны повторяться")
    return seeds


def _run_task(task):
    index, args, kwargs = task
    return index, simulate(*args, **kwargs)


def run_batch(
    plans,
    seeds,
    T,
    W,
    demand_scale=1.0,
    options=None,
    *,
    workers=1,
    timeseries=False,
    demand_log=False,
    progress=None,
):
    """Все пары (план, SEED); результаты в порядке «план, затем SEED» при любом workers.

    workers=1 — в этом процессе, workers=None — по числу ядер. Журнал спроса не зависит
    от плана, поэтому возвращается только для первого плана каждого SEED.
    """
    plans = [as_plan(p) for p in plans]
    seeds = _check_seeds(seeds)
    tasks = [
        (
            i * len(seeds) + j,
            (plan, seed, T, W, demand_scale, options),
            {"timeseries": timeseries, "demand_log": demand_log and i == 0},
        )
        for i, plan in enumerate(plans)
        for j, seed in enumerate(seeds)
    ]
    if not tasks:
        raise ValueError("Нужен хотя бы один план")
    _check_run_args(seeds[0], T, W)  # ошибки параметров — до запуска процессов
    _config_for(seeds[0], plans[0], demand_scale)
    workers = (os.cpu_count() or 1) if workers is None else workers
    workers = max(1, min(workers, len(tasks)))

    outputs = [None] * len(tasks)
    if workers == 1:
        results = map(_run_task, tasks)
        pool = None
    else:
        pool = multiprocessing.Pool(workers)
        results = pool.imap_unordered(_run_task, tasks)
    try:
        for done, (index, output) in enumerate(results, 1):
            outputs[index] = output
            if progress:
                progress(done, len(tasks))
    finally:
        if pool:
            pool.terminate()
            pool.join()
    return outputs


def evaluate(plan, seeds, *, T=3600, W=300, demand_scale=1.0, options=None, workers=1):
    """Строки run_once для плана — по одной на SEED, в порядке seeds. Для скриптов З4–З6."""
    return [
        o.row
        for o in run_batch([plan], seeds, T, W, demand_scale, options, workers=workers)
    ]


def evaluate_grid(
    plans, seeds, *, T=3600, W=300, demand_scale=1.0, options=None, workers=None
):
    """Как evaluate для набора планов; плоский список строк, порядок «план, затем SEED»."""
    return [
        o.row
        for o in run_batch(plans, seeds, T, W, demand_scale, options, workers=workers)
    ]


# ------------------------------------------------------------------------- сводка


def t_critical(df):
    """Двусторонний 95% квантиль Стьюдента (для df > 30 — разложение Корниша–Фишера)."""
    if df <= len(_T975):
        return _T975[df - 1]
    z = 1.959964
    return z + (z**3 + z) / (4 * df) + (5 * z**5 + 16 * z**3 + 3 * z) / (96 * df**2)


def mean_ci95(values):
    """(среднее, стд. отклонение, нижняя и верхняя границы ДИ); при n = 1 — без разброса."""
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        return mean, None, None, None
    std = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))
    half = t_critical(n - 1) * std / math.sqrt(n)
    return mean, std, mean - half, mean + half


def _format_seeds(seeds):
    seeds, parts, i = sorted(seeds), [], 0
    while i < len(seeds):
        j = i
        while j + 1 < len(seeds) and seeds[j + 1] == seeds[j] + 1:
            j += 1
        parts.append(str(seeds[i]) if i == j else f"{seeds[i]}-{seeds[j]}")
        i = j + 1
    return ",".join(parts)


def summarize(rows):
    """Одна строка на группу (план, режим, спрос, T, W); ДИ — по Стьюденту, x̄ ± t·s/√n."""
    groups = defaultdict(list)
    for row in rows:
        groups[tuple(row[k] for k in SUMMARY_GROUP_KEYS)].append(row)
    result = []
    for key, members in groups.items():
        out = dict.fromkeys(SUMMARY_COLUMNS)
        out.update(zip(SUMMARY_GROUP_KEYS, key), n=len(members))
        for metric in SUMMARY_METRICS:
            values = [r[metric] for r in members]
            if all(isinstance(v, (int, float)) for v in values):
                out.update(
                    (f"{metric}_{s}", None if v is None else round(v, 6))
                    for s, v in zip(_STATS, mean_ci95(values))
                )
        first = members[0]
        out["notes"] = (
            f"seeds={_format_seeds([r['seed'] for r in members])}; "
            f"cycle={first['cycle']}; ns_green={first['ns_green']}; "
            f"ew_green={first['ew_green']}; clearance={first['clearance']}"
        )
        result.append(out)
    return result


# --------------------------------------------------------------------------- CSV


def write_csv(path, columns, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows(rows)


def write_demand_log(path, logs):
    """Блоки «Launch i» — по одному на каждый SEED серии, заголовок один."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(DEMAND_LOG_COLUMNS))
        writer.writeheader()
        for i, log in enumerate(logs, 1):
            f.write(f"Launch {i}\n")
            writer.writerows(log)


# --------------------------------------------------------------------------- CLI


def parse_seeds(text):
    """'7 17' / '201-210' / '1,5,9-12' -> список целых; диапазоны включительны."""
    seeds = []
    for part in text.split(","):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part.strip())
        if not m:
            raise argparse.ArgumentTypeError(f"неверный SEED или диапазон: {part!r}")
        low, high = int(m[1]), int(m[2] if m[2] is not None else m[1])
        if high < low:
            raise argparse.ArgumentTypeError(f"диапазон {part!r} убывает")
        seeds.extend(range(low, high + 1))
    return seeds


def _seconds(text):
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"ожидается число, получено {text!r}"
        ) from None
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("ожидается неотрицательное конечное число")
    return _num(value)


def build_parser():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Показатели интервала наблюдения [W, T]: generated, completed, conflicts и задержка\n"
            "завершивших считаются с момента W (всё накопленное до W вычитается), потоки — на\n"
            "(T - W) секунд, очереди — по секундным отсчётам t = W..T. remaining = generated -\n"
            "completed на этом интервале.\n\n"
            "Файлы (в results/, суффикс серии --tag, по умолчанию дата-время):\n"
            "  runs_<tag>.csv        одна строка на прогон (контракт: CONTRACT.md)\n"
            "  summary_<tag>.csv     одна строка на план: n, среднее, std, 95% ДИ Стьюдента\n"
            "  timeseries_<tag>.csv  с --timeseries: очередь каждую секунду, 0 < t <= T\n"
            "  demand_log_<tag>.csv  журнал входного спроса, блок «Launch i» на каждый SEED\n\n"
            "SEED выбирайте из реестра в CONTRACT.md; 901-930 (holdout) до T-45 не использовать."
        ),
    )
    p.add_argument(
        "--seconds",
        type=int,
        default=240,
        help=f"горизонт T, модельные секунды, {MIN_HORIZON}..{MAX_HORIZON} (240)",
    )
    p.add_argument(
        "--warmup",
        type=int,
        default=60,
        help="прогрев W, с: 0 <= W < T; показатели считаются на [W, T] (60)",
    )
    p.add_argument(
        "--seeds",
        type=parse_seeds,
        nargs="+",
        default=[[7, 17, 27]],
        metavar="SEED",
        help="SEED через пробел или запятую, диапазоны включительны: 7 17 201-210 (7 17 27)",
    )
    p.add_argument(
        "--cycle",
        type=_seconds,
        default=None,
        help="общий цикл, с, 24..180 (scenario.py: CYCLE)",
    )
    p.add_argument(
        "--ns-green",
        type=_seconds,
        default=None,
        help="зелёный север-юг, с (scenario.py: NS_GREEN)",
    )
    p.add_argument(
        "--ew-green",
        type=_seconds,
        default=None,
        help="зелёный восток-запад, с; недостающее из cycle, ns-green и clearance "
        "выводится по cycle = ns + ew + 2*clearance (scenario.py)",
    )
    p.add_argument(
        "--clearance",
        type=_seconds,
        default=None,
        help="интервал «всем красный» после каждой фазы, с (scenario.py: CLEARANCE)",
    )
    p.add_argument(
        "--demand-scale",
        type=float,
        default=1.0,
        help="множитель интенсивности всех входящих потоков, 0.5..1.5 (1.0)",
    )
    p.add_argument(
        "--grid",
        action="store_true",
        help="сетка З4: CYCLE 30..120 шаг 10 × доля NS 0.30..0.70 шаг 0.05 от "
        "CYCLE - 2*clearance (90 планов); несовместимо с --cycle/--ns-green/--ew-green",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=None,
        help="число процессов multiprocessing; 1 — без параллельности "
        "(по умолчанию число ядер, но не больше числа прогонов)",
    )
    p.add_argument(
        "--timeseries",
        nargs="?",
        const=True,
        default=None,
        type=Path,
        metavar="PATH",
        help="сохранить ряд очереди по секундам (по умолчанию results/timeseries_<tag>.csv)",
    )
    p.add_argument(
        "--tag",
        default=None,
        help="суффикс имён файлов серии (по умолчанию дата-время)",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        metavar="PATH",
        help="файл подробных результатов (results/runs_<tag>.csv)",
    )
    p.add_argument(
        "--summary",
        type=Path,
        default=None,
        metavar="PATH",
        help="файл сводки (results/summary_<tag>.csv)",
    )
    p.add_argument(
        "--demand-log",
        type=Path,
        default=None,
        metavar="PATH",
        help="журнал входного спроса (results/demand_log_<tag>.csv)",
    )
    p.add_argument(
        "--no-demand-log", action="store_true", help="не писать журнал входного спроса"
    )
    return p


def _progress(done, total):
    if done == total or done % max(1, total // 20) == 0:
        print(f"  выполнено {done}/{total}", file=sys.stderr, flush=True)


def _print_report(rows, summary):
    if len(rows) <= 30:
        print(
            "plan_id | seed | generated | completed | avg queue | delay, s | conflicts | conflicts/100"
        )
        for r in rows:
            delay = (
                "-"
                if r["delay_completed_sec"] is None
                else f"{r['delay_completed_sec']:.2f}"
            )
            c100 = (
                "-"
                if r["conflicts_per_100_completed"] is None
                else f"{r['conflicts_per_100_completed']:.2f}"
            )
            print(
                f"{r['plan_id']} | {r['seed']:4d} | {r['generated']:9d} | {r['completed']:9d} | "
                f"{r['mean_queue_after_warmup']:9.2f} | {delay:>8} | {r['conflicts']:9d} | {c100:>13}"
            )
    print("plan_id | n | avg queue, mean ± 95% CI | delay, s, mean ± 95% CI")
    for s in summary:

        def cell(metric):
            mean, low, high = (
                s[f"{metric}_{k}"] for k in ("mean", "ci95_low", "ci95_high")
            )
            if mean is None:
                return "-"
            return (
                f"{mean:.2f}" if low is None else f"{mean:.2f} ± {(high - low) / 2:.2f}"
            )

        print(
            f"{s['plan_id']} | {s['n']} | {cell('mean_queue_after_warmup')} | "
            f"{cell('delay_completed_sec')}"
        )


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    seeds = [s for group in a.seeds for s in group]
    if not (MIN_HORIZON <= a.seconds <= MAX_HORIZON) or not (0 <= a.warmup < a.seconds):
        p.error(
            f"Требуется {MIN_HORIZON} <= seconds <= {MAX_HORIZON} и 0 <= warmup < seconds"
        )
    if a.cycle is not None and not (24 <= a.cycle <= 180):
        p.error("Требуется 24 <= cycle <= 180")
    if not (0.5 <= a.demand_scale <= 1.5):
        p.error("Требуется 0.5 <= demand-scale <= 1.5")
    if a.workers is not None and a.workers < 1:
        p.error("Требуется workers >= 1")
    if a.tag is not None and not re.fullmatch(r"[\w.-]+", a.tag):
        p.error("tag может содержать только буквы, цифры, '_', '.', '-'")
    if a.grid and (a.cycle, a.ns_green, a.ew_green) != (None, None, None):
        p.error("--grid несовместим с --cycle, --ns-green и --ew-green")
    try:
        plans = (
            grid_plans(clearance=a.clearance)
            if a.grid
            else [make_plan(a.cycle, a.ns_green, a.ew_green, a.clearance)]
        )
        _check_seeds(seeds)
        started = time.perf_counter()
        outputs = run_batch(
            plans,
            seeds,
            a.seconds,
            a.warmup,
            a.demand_scale,
            workers=a.workers,
            timeseries=a.timeseries is not None,
            demand_log=not a.no_demand_log,
            progress=_progress,
        )
    except ValueError as exc:
        p.error(str(exc))
    elapsed = time.perf_counter() - started

    tag = a.tag or datetime.now().strftime("%Y%m%d-%H%M%S")
    rows = [o.row for o in outputs]
    summary = summarize(rows)
    paths = [
        (
            a.out or RESULTS_DIR / f"runs_{tag}.csv",
            _RUN_ONCE_RESULT_KEYS,
            rows,
            "Сохранено",
        ),
        (
            a.summary or RESULTS_DIR / f"summary_{tag}.csv",
            SUMMARY_COLUMNS,
            summary,
            "Сводка",
        ),
    ]
    if a.timeseries is not None:
        path = (
            RESULTS_DIR / f"timeseries_{tag}.csv"
            if a.timeseries is True
            else a.timeseries
        )
        paths.append(
            (
                path,
                TIMESERIES_COLUMNS,
                [r for o in outputs for r in o.timeseries],
                "Ряд очереди",
            )
        )
    for path, columns, data, label in paths:
        write_csv(path, columns, data)
        print(f"{label}: {path}")
    if not a.no_demand_log:
        path = a.demand_log or RESULTS_DIR / f"demand_log_{tag}.csv"
        write_demand_log(
            path, [o.demand_log for o in outputs if o.demand_log is not None]
        )
        print(f"Журнал входного спроса: {path}")

    _print_report(rows, summary)
    print(f"Прогонов: {len(rows)}, время: {elapsed:.1f} с")
    print(
        "Предупреждение: delay усредняется только по автомобилям, завершившим поездку на [W, T]."
    )


if __name__ == "__main__":
    main()
