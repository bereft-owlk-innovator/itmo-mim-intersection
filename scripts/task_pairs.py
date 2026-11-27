"""Задание: сравнение двух планов на общих и независимых потоках.

План A = базовый (42/18/18/3), план B = (42/22/14/3).
Сравниваем по delay_completed_sec (средняя задержка завершивших поездку
машин) на [W, T], W = 300, T = 3600.

Для каждой пары считаем разность delay(A) − delay(B):
  * общие потоки — оба плана на SEED 1..20 (одна и та же реализация);
  * независимые  — A на SEED 1..20, B на SEED 21..40 (разные реализации).

Дополнительно скрипт делает то, что нужно для отчёта по заданию:

  * 95% ДИ для средней разности (t-распределение Стьюдента, n−1 ст. св.);
  * отношение дисперсий common/independent и корреляцию delay_A/delay_B
    на общих потоках — числовое подтверждение формулы
        Var(X − Y) = Var(X) + Var(Y) − 2 Cov(X, Y);
  * проверку теста п.3: журнал входного спроса совпадает для планов A и B
    при одном SEED до 1800 с;
  * фрагмент журнала (первые 5 строк) — вставить в отчёт.

Множитель спроса варианта 2 (0,95) реализован через
scenario.rate_multiplier(side, t). В задачах по светофору его менять нельзя,
поэтому DEMAND_SCALE = 1.0: итоговая интенсивность уже равна 0,95 от базовой.
Если в сценарии rate_multiplier будет возвращать 1.0, задайте DEMAND_SCALE
равным множителю варианта (например 0.95) вручную.

Запуск из корня репозитория:
    python task_pairs.py                # последовательно
    python task_pairs.py --workers 4    # параллельно (порядок пар сохраняется)

Результаты:
    results/task_pairs.csv     одна строка на пару (40 строк)
    results/task_summary.csv   сводка по режимам: mean, std, 95% ДИ разности
    results/task_meta.csv      корреляция, отношение дисперсий, результат
                                проверки журнала, план и параметры прогона
    results/task_demand_fragment.csv   5 строк журнала входного спроса
"""

import argparse
import csv
import math
import statistics
from pathlib import Path

from batch_runs import DEMAND_LOG_COLUMNS, Plan, evaluate, simulate, t_critical

RESULTS = Path(__file__).resolve().parent.parent / "results"
RESULTS.mkdir(exist_ok=True)

PLAN_A = Plan(cycle=42, ns_green=18, ew_green=18, clearance=3)
PLAN_B = Plan(cycle=42, ns_green=22, ew_green=14, clearance=3)
T, W = 3600, 300
DEMAND_SCALE = 1.0
METRIC = "delay_completed_sec"

COMMON_SEEDS = list(range(1, 21))
INDEP_SEEDS_A = list(range(1, 21))
INDEP_SEEDS_B = list(range(21, 41))

JOURNAL_CHECK_SEED = 1
JOURNAL_CHECK_T = 1800
JOURNAL_FRAGMENT_ROWS = 5


# ---------------------------------------------------------------- пары прогонов

def pair_differences(seeds_a, seeds_b, workers=None):
    """Разность delay(A) − delay(B) для пар (seed_A, seed_B); порядок сохраняется."""
    rows_a = evaluate(PLAN_A, seeds_a, T=T, W=W,
                      demand_scale=DEMAND_SCALE, workers=workers)
    rows_b = evaluate(PLAN_B, seeds_b, T=T, W=W,
                      demand_scale=DEMAND_SCALE, workers=workers)
    pairs = []
    for i, (ra, rb) in enumerate(zip(rows_a, rows_b), 1):
        da, db = ra[METRIC], rb[METRIC]
        if da is None or db is None:
            raise RuntimeError(
                f"{METRIC} is None в паре {i}: "
                f"A.seed={ra['seed']} ({da}), B.seed={rb['seed']} ({db})"
            )
        pairs.append({
            "pair": i,
            "seed_A": ra["seed"],
            "seed_B": rb["seed"],
            "delay_A_sec": da,
            "delay_B_sec": db,
            "diff_A_minus_B_sec": da - db,
        })
    return pairs


def describe(pairs, mode):
    diffs = [p["diff_A_minus_B_sec"] for p in pairs]
    n = len(diffs)
    mean_diff = statistics.fmean(diffs)
    std_diff = statistics.stdev(diffs)
    if n > 1:
        half = t_critical(n - 1) * std_diff / math.sqrt(n)
        ci_low, ci_high = mean_diff - half, mean_diff + half
    else:
        ci_low = ci_high = None
    return {
        "mode": mode,
        "n": n,
        "mean_diff": mean_diff,
        "std_diff": std_diff,
        "ci95_low": ci_low,
        "ci95_high": ci_high,
        "mean_A": statistics.fmean(p["delay_A_sec"] for p in pairs),
        "std_A": statistics.stdev(p["delay_A_sec"] for p in pairs),
        "mean_B": statistics.fmean(p["delay_B_sec"] for p in pairs),
        "std_B": statistics.stdev(p["delay_B_sec"] for p in pairs),
    }


# ------------------------------------------------- проверка теста п.3 задания

def journal_identity_check():
    """Журнал спроса одинаков для планов A и B при одном SEED до 1800 с.

    Это прямая демонстрация п.3 задания 1 без запуска unittest: журнал зависит
    только от SEED, но не от плана светофора. Возвращает (ok, len_a, len_b, log_a).
    """
    log_a = simulate(PLAN_A, JOURNAL_CHECK_SEED, JOURNAL_CHECK_T, 0,
                     DEMAND_SCALE, demand_log=True).demand_log
    log_b = simulate(PLAN_B, JOURNAL_CHECK_SEED, JOURNAL_CHECK_T, 0,
                     DEMAND_SCALE, demand_log=True).demand_log
    keys = ("t_born", "entry_side", "turn")
    a = [tuple(r[k] for k in keys) for r in log_a]
    b = [tuple(r[k] for k in keys) for r in log_b]
    return a == b, len(a), len(b), log_a


# ------------------------------------------------------------------ утилиты

def _write_csv(path, columns, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows(rows)


def _parse_workers(text):
    if text.lower() == "auto":
        return None
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("workers должен быть >= 1 или 'auto'")
    return value


def build_parser():
    return argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


# ----------------------------------------------------------------------- main

def main(argv=None):
    parser = build_parser()
    parser.add_argument("--workers", type=_parse_workers, default=None,
                        help="число процессов multiprocessing; 'auto' — по числу ядер "
                             "(по умолчанию)")
    args = parser.parse_args(argv)
    workers = args.workers

    print(f"План A: {PLAN_A}")
    print(f"План B: {PLAN_B}")
    print(f"T = {T} с, W = {W} с, DEMAND_SCALE = {DEMAND_SCALE}, "
          f"workers = {workers if workers is not None else 'auto'}")
    print()

    common = pair_differences(COMMON_SEEDS, COMMON_SEEDS, workers=workers)
    indep = pair_differences(INDEP_SEEDS_A, INDEP_SEEDS_B, workers=workers)
    summary = [describe(common, "common"), describe(indep, "independent")]

    # Положительная корреляция delay(A), delay(B) на общих потоках — причина,
    # по которой Var(X − Y) = Var(X) + Var(Y) − 2·Cov(X, Y) уменьшается.
    corr = statistics.correlation(
        [p["delay_A_sec"] for p in common],
        [p["delay_B_sec"] for p in common],
    )

    std_common = summary[0]["std_diff"]
    std_indep = summary[1]["std_diff"]
    var_ratio = (std_common ** 2 / std_indep ** 2) if std_indep > 0 else float("nan")

    journal_ok, journal_n_a, journal_n_b, journal_log_a = journal_identity_check()

    # --- CSV: по строке на пару (40 строк).
    _write_csv(
        RESULTS / "task_pairs.csv",
        ["mode", "pair", "seed_A", "seed_B",
         "delay_A_sec", "delay_B_sec", "diff_A_minus_B_sec"],
        [{"mode": "common", **p} for p in common]
        + [{"mode": "independent", **p} for p in indep],
    )

    # --- CSV: сводка по режимам со средним, std и 95% ДИ для разности.
    _write_csv(
        RESULTS / "task_summary.csv",
        list(summary[0]),
        summary,
    )

    # --- CSV: метаданные (для отчёта и воспроизводимости).
    _write_csv(
        RESULTS / "task_meta.csv",
        ["plan_A", "plan_B", "T_sec", "W_sec", "demand_scale",
         "seeds_common", "seeds_independent_A", "seeds_independent_B",
         "corr_delay_A_delay_B_common", "var_ratio_common_over_indep",
         "journal_identical", "journal_n_a", "journal_n_b",
         "journal_seed", "journal_horizon_sec"],
        [{
            "plan_A": str(PLAN_A),
            "plan_B": str(PLAN_B),
            "T_sec": T,
            "W_sec": W,
            "demand_scale": DEMAND_SCALE,
            "seeds_common": "1-20",
            "seeds_independent_A": "1-20",
            "seeds_independent_B": "21-40",
            "corr_delay_A_delay_B_common": round(corr, 6),
            "var_ratio_common_over_indep": round(var_ratio, 6),
            "journal_identical": int(journal_ok),
            "journal_n_a": journal_n_a,
            "journal_n_b": journal_n_b,
            "journal_seed": JOURNAL_CHECK_SEED,
            "journal_horizon_sec": JOURNAL_CHECK_T,
        }],
    )

    # --- CSV: фрагмент журнала из 5 строк (Launch 1, план A, seed JOURNAL_CHECK_SEED).
    fragment = journal_log_a[:JOURNAL_FRAGMENT_ROWS]
    _write_csv(
        RESULTS / "task_demand_fragment.csv",
        DEMAND_LOG_COLUMNS,
        fragment,
    )

    # --- Консольный отчёт.
    print(f"corr(delay_A, delay_B) на общих потоках = {corr:.4f}")
    print(f"Отношение дисперсий common/indep = {var_ratio:.4f}  "
          f"(Var(X−Y) = Var(X)+Var(Y)−2·Cov(X,Y); <1 при Cov>0)")
    print(f"Журнал совпал для планов A и B при SEED={JOURNAL_CHECK_SEED} "
          f"до {JOURNAL_CHECK_T} с: {journal_ok} "
          f"(n_A={journal_n_a}, n_B={journal_n_b})")
    print()
    print(f"{'mode':<12} {'n':>2} {'mean(diff)':>11} {'std(diff)':>10} "
          f"{'95% CI':>24} {'mean(A)':>8} {'mean(B)':>8}")
    for s in summary:
        ci = (f"[{s['ci95_low']:.3f}, {s['ci95_high']:.3f}]"
              if s["ci95_low"] is not None else "-")
        print(f"{s['mode']:<12} {s['n']:>2} {s['mean_diff']:>11.3f} "
              f"{s['std_diff']:>10.3f} {ci:>24} "
              f"{s['mean_A']:>8.3f} {s['mean_B']:>8.3f}")
    print()
    print("Фрагмент журнала (5 строк, план A, SEED "
          f"{JOURNAL_CHECK_SEED}):")
    for r in fragment:
        print(f"  {r['vehice_id']:>4d}  t_born={r['t_born']:>7.2f}  "
              f"side={r['entry_side']}  turn={r['turn']}")
    print()
    print(f"CSV: {RESULTS/'task_pairs.csv'}")
    print(f"CSV: {RESULTS/'task_summary.csv'}")
    print(f"CSV: {RESULTS/'task_meta.csv'}")
    print(f"CSV: {RESULTS/'task_demand_fragment.csv'}")


if __name__ == "__main__":
    main()
