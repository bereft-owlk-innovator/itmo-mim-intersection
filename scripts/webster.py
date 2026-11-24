"""Оценка цикла Уэбстера. Запуск: uv run python -m scripts.webster."""

import csv
import sys
from collections import Counter

from batch_runs import DT, make_config
from simulation import STOP, Simulation, default_connections, turn_for

PHASES = ("NS", "EW")
SEEDS = (201, 202, 203)
LOAD = 6
WARMUP_CYCLES = 8
MEASURE_CYCLES = 20
LANES = [(side, lane) for phase in PHASES for side in phase for lane in range(3)]


def measure(config):
    sim = Simulation(config, config.INITIAL_CONNECTIONS)
    steps_per_cycle = round(config.CYCLE / DT)
    for _ in range(WARMUP_CYCLES * steps_per_cycle):
        sim.step(DT)

    intervals, duration = Counter(), Counter()
    queued, last_departure = set(), {}
    previous_phase = None
    for _ in range(MEASURE_CYCLES * steps_per_cycle):
        phase = next((p for p in PHASES if config.green(p[0], 1, "straight", sim.time + DT)), None)
        if phase != previous_phase:
            # Only the contiguous front of a stopped queue can supply saturation headways.
            queued = {car.id for car in sim.cars
                      if phase and car.side in phase and car.stage == "approach"
                      and car.chosen and car.speed < 1
                      and car.s >= STOP - config.MIN_GAP - 1e-6}
            last_departure.clear()
        sim.step(DT)
        for car in sim.cars:
            if car.id not in queued or car.stage != "crossing":
                continue
            queued.remove(car.id)
            lane = (car.side, car.lane)
            if lane in last_departure:
                intervals[lane] += 1
                duration[lane] += sim.time - last_departure[lane]
            last_departure[lane] = sim.time
        previous_phase = phase
    return intervals, duration


def main():
    intervals, duration = Counter(), Counter()
    for seed in SEEDS:
        config = make_config(seed, ns_green=18, cycle=42)
        if ((config.CLEARANCE, config.EW_GREEN) != (3, 18)
                or config.SIGNAL_MODE != "cycle" or config.LANE_SIGNALS or config.EXTRA_SECTIONS
                or config.INITIAL_CONNECTIONS != default_connections()):
            raise ValueError("Для замера нужен базовый двухфазный план и исходный граф")
        config.ARRIVALS = {
            side: (lambda rng, interval=interval: interval(rng) / LOAD)
            for side, interval in config.ARRIVALS.items()
        }
        sample_intervals, sample_duration = measure(config)
        intervals.update(sample_intervals)
        duration.update(sample_duration)
    if any(not intervals[lane] for lane in LANES):
        raise ValueError("Недостаточно пар машин из очереди у стоп-линии")

    rates = {lane: intervals[lane] / duration[lane] for lane in LANES}
    demand = {
        (side, lane): 0.95 * config.TURN_SHARES[turn_for(side, target)] / config.MEAN_ARRIVAL_SECONDS[side]
        for side, lane, target, _ in config.INITIAL_CONNECTIONS
    }
    ratios = {lane: demand[lane] / rates[lane] for lane in LANES}

    output = csv.writer(sys.stdout)
    output.writerow(("lane", "headways", "sampled_green_sec", "q_veh_per_sec", "s_veh_per_sec", "y"))
    for side, lane in LANES:
        key = side, lane
        output.writerow((f"{side}{lane}", intervals[key], f"{duration[key]:.2f}",
                         f"{demand[key]:.6f}", f"{rates[key]:.6f}", f"{ratios[key]:.6f}"))
    total = 0
    for phase in PHASES:
        critical = max((lane for lane in LANES if lane[0] in phase), key=ratios.__getitem__)
        total += ratios[critical]
        side, lane = critical
        print(f"{phase}: critical={side}{lane}, y={ratios[critical]:.6f}", file=sys.stderr)
    lost = 2 * config.CLEARANCE
    cycle = f"{(1.5 * lost + 5) / (1 - total):.6f}" if total < 1 else "undefined"
    print(f"L={lost} s, Y={total:.6f}, C0={cycle} s", file=sys.stderr)


if __name__ == "__main__":
    main()
