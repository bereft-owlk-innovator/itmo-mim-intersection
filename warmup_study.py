"""Задание 3.2: прогрев W по 10 сидам, метод Уэлча."""
import matplotlib

matplotlib.use("Agg")
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

from batch_runs import make_config
from simulation import Simulation


# --- Константы эксперимента ------------------------------------------------
DT = 0.05
T = 3600                         # горизонт, секунды (≥1800 — требование)
SEEDS = list(range(101, 111))    # 101..110
DEMAND = 1.10                    # множитель своего варианта! Проверь таблицу 3
W_CANDIDATE = 300                # предварительный W (≥300 — требование)
SAMPLE_EVERY = 1.0               # секунд между точками графика
WINDOW = 30                      # окно скользящего среднего, секунд
PLATEAU_THRESHOLD = 0.05         # 5 % — порог "режим установился"


# --- Прогон одного сида ----------------------------------------------------
def queue_series(seed):
    """Возвращает (times, queues) для одного прогона при заданном SEED."""
    config = make_config(seed, demand_scale=DEMAND)
    sim = Simulation(config, config.INITIAL_CONNECTIONS)
    n_steps = int(round(T / DT))
    step_per_sample = int(round(SAMPLE_EVERY / DT))

    times, queues = [], []
    for i in range(n_steps):
        sim.step(DT)
        if i % step_per_sample == 0:
            times.append(sim.time)
            queues.append(sim.metrics()["queue"])
    return np.array(times), np.array(queues)


# --- Сборка серии ----------------------------------------------------------
def collect_series(seeds):
    """Прогоняет серию и возвращает усреднённые по сидам t и queue."""
    print("Прогоны по сидам:", seeds)
    q_series = []
    for seed in seeds:
        print("  seed", seed)
        t, q = queue_series(seed)
        q_series.append(q)

    mean_q = np.stack(q_series).mean(axis=0)
    return t, mean_q


# --- Скользящее среднее (Уэлч) --------------------------------------------
def welch_smooth(x, window):
    kernel = np.ones(window) / window
    return np.convolve(x, kernel, mode="valid")


# --- Проверка плато --------------------------------------------------------
def check_plateau(smooth, smooth_t):
    """Сравнивает средние на двух последних четвертях [0.5T, 0.75T] и [0.75T, T].

    Обе зоны уже в предполагаемом плато, если оно вообще есть, — линейный
    рост даёт явную разницу, выход на плато — близкие средние.
    """
    q_prev = smooth[(smooth_t >= 0.50 * T) & (smooth_t < 0.75 * T)].mean()
    q_last = smooth[smooth_t >= 0.75 * T].mean()
    growth = (q_last - q_prev) / max(1e-9, q_prev)
    return q_prev, q_last, growth


# --- График ----------------------------------------------------------------
def make_plot(mean_t, mean_q, smooth, smooth_t, out_path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.figure(figsize=(11, 5))
    plt.plot(mean_t, mean_q, color="lightsteelblue",
             label="средняя очередь по 10 сидам")
    plt.plot(smooth_t, smooth, color="navy", lw=2,
             label=f"скользящее среднее (окно {WINDOW} с)")
    plt.axvline(W_CANDIDATE, color="crimson", ls="--", lw=1.5,
                label=f"выбранный W = {W_CANDIDATE} с")
    plt.axvline(T, color="grey", ls=":", lw=1, label=f"T = {T} с")
    plt.xlabel("время, с")
    plt.ylabel("суммарная очередь, машин")
    plt.title("Прогрев: средняя очередь, метод Уэлча")
    plt.legend()
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close()
    print(f"Сохранено: {out_path}")


# --- main ------------------------------------------------------------------
def main():
    t, mean_q = collect_series(SEEDS)

    smooth = welch_smooth(mean_q, WINDOW)
    smooth_t = t[WINDOW - 1:]

    make_plot(t, mean_q, smooth, smooth_t,
              Path("report/fig/warmup.png"))

    q_prev, q_last, growth = check_plateau(smooth, smooth_t)
    print(f"Средняя сглаженная очередь на [0.50T, 0.75T] = {q_prev:.2f}")
    print(f"Средняя сглаженная очередь на [0.75T, T]     = {q_last:.2f}")
    print(f"Относительный рост: {growth * 100:.1f} %")

    if growth > PLATEAU_THRESHOLD:
        print("ВНИМАНИЕ: очередь растёт до конца прогона "
              "→ режим НЕ установившийся.")
    else:
        print("Плато достигнуто → режим можно считать установившимся.")


if __name__ == "__main__":
    main()
