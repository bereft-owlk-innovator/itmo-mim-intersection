"""Перекрёсток — сценарий лабораторной работы.

Меняйте параметры в ЭТОМ файле и сохраняйте Ctrl+S.
Python автоматически начнёт новый опыт; статистику можно скачать из браузера.
Все расстояния условные. N/E/S/W — сторона, ОТКУДА едет машина.
"""

SEED = 7
SPEED = 42.0
MIN_GAP = 27.0

# 0: внутренняя полоса (налево), 1: средняя (прямо), 2: внешняя (направо).
SIDES = "NESW"
INITIAL_CONNECTIONS = [
    [side, lane, SIDES[(SIDES.index(side) + lane + 1) % 4], lane]
    for side in SIDES for lane in range(3)
]

# Средние интервалы поступления: N/S — 2.4 с, E/W — 3.2 с.
# Не трогайте ARRIVALS, пока не сравнили варианты светофора.
ARRIVALS = {
    "N": lambda rng: rng.expovariate(1 / 2.4),
    "E": lambda rng: rng.expovariate(1 / 3.2),
    "S": lambda rng: rng.expovariate(1 / 2.4),
    "W": lambda rng: rng.expovariate(1 / 3.2),
}


def rate_multiplier(approach, t):
    """Множитель интенсивности прибытия; для базового сценария спрос постоянный."""
    return 0,95 


def desired_turn(side, rng):
    """Распределение желаемых манёвров: 25% налево, 60% прямо, 15% направо."""
    return rng.choices(
        ["left", "straight", "right"],
        weights=[0.25, 0.60, 0.15],
    )[0]


def choose_lane(side, turn, candidates, queues, rng):
    """Выбрать самую короткую из допустимых очередей."""
    return min(candidates, key=lambda lane: (queues[lane], rng.random()))


# Базовый план. В сериях экспериментов параметры можно переопределять через CLI.
CYCLE = 42
CLEARANCE = 3
NS_GREEN = 18
EW_GREEN = CYCLE - 2 * CLEARANCE - NS_GREEN

# Не разрешаем накладываться основным фазам и убираем нулевые фазы.
assert NS_GREEN > 0 and EW_GREEN > 0 and CLEARANCE >= 0
SIGNAL_MODE = "cycle"              # "cycle" или "off"
LANE_SIGNALS = {}                   # например {("N", 0): lambda t: 4 <= t % 42 < 12}
EXTRA_SECTIONS = {}                 # например {("E", "right"): lambda t: True}


def road_green(side, t):
    """Три стадии: NS -> всем красный -> EW -> всем красный."""
    phase = t % CYCLE
    if side in ("N", "S"):
        return phase < NS_GREEN
    return NS_GREEN + CLEARANCE <= phase < NS_GREEN + CLEARANCE + EW_GREEN


def signal_for(config, side, lane, turn, t):
    """Общий закон управления для браузера и пакетных экспериментов.

    Все значения берутся из переданной конфигурации, а не из глобальных
    констант. Благодаря этому серия прогонов может менять план без правки файла.
    """
    if config.SIGNAL_MODE == "off":
        return True
    phase = t % config.CYCLE
    if side in ("N", "S"):
        road_open = phase < config.NS_GREEN
    else:
        road_open = (config.NS_GREEN + config.CLEARANCE <= phase
                     < config.NS_GREEN + config.CLEARANCE + config.EW_GREEN)
    lane_rule = config.LANE_SIGNALS.get((side, lane))
    main = lane_rule(t) if lane_rule else road_open
    extra = config.EXTRA_SECTIONS.get((side, turn))
    return bool(main or (extra(t) if extra else False))


def green(side, lane, turn, t):
    """Разрешение на проезд с учётом полос и дополнительных секций."""
    if SIGNAL_MODE == "off":
        return True
    signal = LANE_SIGNALS.get((side, lane))
    main = signal(t) if signal else road_green(side, t)
    section = EXTRA_SECTIONS.get((side, turn))
    return bool(main or (section(t) if section else False))


# Метрики обязательно нужны для исследования.
COUNT_CONFLICTS = True
COLLECT_STATS = True
