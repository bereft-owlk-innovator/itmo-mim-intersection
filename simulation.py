"""Модель без веб-зависимостей: геометрия, очереди, движение, метрики."""
from collections import deque
from dataclasses import dataclass
import math
import random

SIDES = "NESW"
TURNS = ("left", "straight", "right")
STOP = 364.0
CHANGE = 150.0
EXIT_LENGTH = 364.0


class _StoredRandom:
    def __init__(self, values):
        self._values = iter(values)

    def random(self):
        try:
            return next(self._values)
        except StopIteration as exc:
            raise RuntimeError("Для выбора полосы заранее не хватило случайных значений") from exc


def rotate(x, y, side):
    for _ in range(SIDES.index(side)):
        x, y = -y, x
    return [500 + x, 500 + y]


def node(side, lane, incoming=True):
    return rotate((-1 if incoming else 1) * (16 + 32 * lane), -96, side)


def destination(side, turn):
    return SIDES[(SIDES.index(side) + {"left": 1, "straight": 2, "right": 3}[turn]) % 4]


def turn_for(source, target):
    return {1: "left", 2: "straight", 3: "right"}.get(
        (SIDES.index(target) - SIDES.index(source)) % 4
    )


def default_connections():
    return [[s, lane, destination(s, turn), lane] for s in SIDES
            for lane, turn in enumerate(TURNS)]


def validate_connections(connections):
    if not isinstance(connections, list) or len(connections) > 108:
        raise ValueError("Ожидается список, не более 108 связей")
    result = []
    for item in connections:
        if not isinstance(item, list) or len(item) != 4:
            raise ValueError("Связь: [сторона, полоса, сторона, полоса]")
        a, i, b, j = item
        if a not in SIDES or b not in SIDES or a == b or type(i) is not int or type(j) is not int or i not in range(3) or j not in range(3):
            raise ValueError("Недопустимые вершины графа")
        if item not in result:
            result.append(item)
    return result


class Route:
    def __init__(self, connection):
        self.key = tuple(connection)
        a, i, b, j = connection
        p, q = node(a, i), node(b, j, False)
        # Касательные направлены вдоль входящей и выходящей полос.
        inward = rotate(0, 1, a)
        outward = rotate(0, -1, b)
        control = min(135, math.dist(p, q) * 0.6)
        c = [p[k] + (inward[k] - 500) * control for k in range(2)]
        d = [q[k] - (outward[k] - 500) * control for k in range(2)]
        self.points, self.lengths = [], [0.0]
        for n in range(81):
            u, v = n / 80, 1 - n / 80
            point = [v**3*p[k] + 3*v*v*u*c[k] + 3*v*u*u*d[k] + u**3*q[k] for k in range(2)]
            if self.points:
                self.lengths.append(self.lengths[-1] + math.dist(point, self.points[-1]))
            self.points.append(point)
        self.length = self.lengths[-1]

    def position(self, distance):
        import bisect
        index = min(79, max(0, bisect.bisect_right(self.lengths, distance) - 1))
        a, b = self.points[index:index+2]
        portion = max(0, min(1, (distance-self.lengths[index]) / (self.lengths[index+1]-self.lengths[index])))
        return [a[k] + (b[k]-a[k])*portion for k in range(2)], math.atan2(b[1]-a[1], b[0]-a[0])


@dataclass
class Car:
    id: int
    side: str
    turn: str
    born: float
    lane: int = 1
    chosen: bool = False
    stage: str = "approach"
    s: float = 0.0
    speed: float = 0.0
    delay: float = 0.0
    route: Route | None = None
    lane_tiebreakers: tuple = ()
    trajectory_pick: float = 0.0


class Simulation:
    def __init__(self, config, connections=None):
        if not math.isfinite(config.SPEED) or config.SPEED <= 0:
            raise ValueError("SPEED должен быть положительным")
        if not math.isfinite(config.MIN_GAP) or config.MIN_GAP < 20:
            raise ValueError("MIN_GAP должен быть не меньше 20")
        self.config = config
        self.arrival_rngs = {
            side: random.Random(config.SEED * 1000 + index)
            for index, side in enumerate(SIDES)
        }
        self.maneuver_rng = random.Random(config.SEED * 1000 + 4)
        self.lane_rng = random.Random(config.SEED * 1000 + 5)
        self.trajectory_rng = random.Random(config.SEED * 1000 + 6)
        self.time = 0.0
        self.cars = []
        self.pending = {s: deque() for s in SIDES}
        self.next_arrival = {s: self.interval(s, self.time) for s in SIDES}
        self.generated = self.completed = self.conflicts = 0
        self.total_delay = self.total_travel = 0.0
        self.contact_pairs = set()
        self.colliding = set()
        self.history = deque(maxlen=10000)
        self.demand_log = deque(maxlen=10000)
        self.next_sample = 0.0
        self.set_connections(getattr(config, "INITIAL_CONNECTIONS", default_connections()) if connections is None else connections)
        # Проверяем сигналы сразу, чтобы ошибка показывалась при загрузке.
        self.signals()

    def interval(self, side, t=None):
        if t is None:
            t = self.time
        value = float(self.config.ARRIVALS[side](self.arrival_rngs[side], t))
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"Интервал {side} должен быть конечным и > 0")
        rate_multiplier = getattr(self.config, "rate_multiplier", None)
        multiplier = 1.0 if rate_multiplier is None else float(rate_multiplier(side, t))
        if not math.isfinite(multiplier) or multiplier <= 0:
            raise ValueError(f"Множитель интенсивности {side} должен быть конечным и > 0")
        return max(0.05, value / multiplier)

    def set_connections(self, connections):
        self.connections = validate_connections(connections)
        self.routes = {tuple(c): Route(c) for c in self.connections}

    def green(self, side, lane, turn):
        """Без функции светофора движение ничем не регулируется."""
        signal = getattr(self.config, "green", None)
        return True if signal is None else bool(signal(side, lane, turn, self.time))

    def available(self, car, lane=None):
        return [r for key, r in self.routes.items() if key[0] == car.side
                and key[2] == destination(car.side, car.turn)
                and (lane is None or key[1] == lane)]

    def position(self, car):
        if car.stage == "crossing":
            return car.route.position(car.s)
        if car.stage == "exit":
            side, lane = car.route.key[2:]
            p = rotate(16 + lane * 32, -96-car.s, side)
            v = rotate(0, -1, side)
        else:
            # Перестроение — дискретное решение в точке CHANGE.
            p = rotate(-16-car.lane*32, -460+car.s, car.side)
            v = rotate(0, 1, car.side)
        return p, math.atan2(v[1]-500, v[0]-500)

    def step(self, dt=0.05):
        self.time += dt
        cfg = self.config
        for side in SIDES:
            while self.next_arrival[side] <= self.time:
                born = self.next_arrival[side]
                turn = cfg.desired_turn(side, self.maneuver_rng) if hasattr(cfg, "desired_turn") else "straight"
                if turn not in TURNS:
                    raise ValueError("desired_turn должен вернуть left, straight или right")
                self.generated += 1
                car = Car(
                    self.generated, side, turn, born,
                    lane_tiebreakers=tuple(self.lane_rng.random() for _ in range(3)),
                    trajectory_pick=self.trajectory_rng.random(),
                )
                self.pending[side].append(car)
                self.demand_log.append({"vehice_id":car.id, "t_born": car.born, "entry_side":car.side, "turn":car.turn})
                self.next_arrival[side] += self.interval(side, born)
            if self.pending[side] and not any(c.side == side and c.stage == "approach" and c.lane == 1 and c.s < cfg.MIN_GAP for c in self.cars):
                car = self.pending[side].popleft()
                if getattr(cfg, "COLLECT_STATS", False):
                    car.delay = self.time - car.born
                self.cars.append(car)

        # Впереди идущие машины обновляются первыми в пределах каждой стадии.
        for car in sorted(self.cars, key=lambda c: c.s, reverse=True):
            old_s = car.s
            limit = math.inf
            if car.stage == "approach":
                if not car.chosen and car.s >= CHANGE:
                    candidates = sorted({r.key[1] for r in self.available(car)})
                    if candidates:
                        queues = {lane: sum(c.side == car.side and c.stage == "approach" and c.lane == lane and c.s >= car.s for c in self.cars if c is not car) for lane in range(3)}
                        lane = cfg.choose_lane(
                            car.side, car.turn, candidates, queues,
                            _StoredRandom(car.lane_tiebreakers),
                        ) if hasattr(cfg, "choose_lane") else candidates[0]
                        if lane not in candidates:
                            raise ValueError("choose_lane вернул недопустимую полосу")
                        if not any(c is not car and c.stage == "approach" and c.side == car.side and c.lane == lane and abs(c.s-car.s) < cfg.MIN_GAP for c in self.cars):
                            car.lane, car.chosen = lane, True
                    if not car.chosen:
                        limit = CHANGE
                if not car.chosen:
                    limit = min(limit, CHANGE)
                routes = self.available(car, car.lane) if car.chosen else []
                allowed = bool(routes) and self.green(car.side, car.lane, car.turn)
                limit = min(limit, STOP)
                for other in self.cars:
                    if other is not car and other.stage == "approach" and other.side == car.side and other.lane == car.lane and other.s > car.s:
                        limit = min(limit, other.s - cfg.MIN_GAP)
                car.s = max(car.s, min(car.s + cfg.SPEED*dt, limit))
                if car.s >= STOP and allowed:
                    # Не входить вплотную за машиной на той же траектории.
                    free = [r for r in routes if not any(o.stage == "crossing" and o.route.key == r.key and o.s < cfg.MIN_GAP for o in self.cars)]
                    if free:
                        index = min(int(car.trajectory_pick * len(free)), len(free) - 1)
                        car.route = free[index]
                        car.stage, car.s = "crossing", 0.0
                        old_s = -max(0, STOP-old_s)
            elif car.stage == "crossing":
                limit = car.route.length
                for other in self.cars:
                    if other is car:
                        continue
                    if other.stage == "crossing" and other.route.key == car.route.key and other.s > car.s:
                        limit = min(limit, other.s-cfg.MIN_GAP)
                    if other.stage == "exit" and other.route.key[2:] == car.route.key[2:]:
                        limit = min(limit, car.route.length+other.s-cfg.MIN_GAP)
                car.s = max(car.s, min(car.s+cfg.SPEED*dt, limit))
                if car.s >= car.route.length:
                    old_s -= car.route.length
                    car.stage, car.s = "exit", 0.0
            else:
                for other in self.cars:
                    if other is not car and other.stage == "exit" and other.route.key[2:] == car.route.key[2:] and other.s > car.s:
                        limit = min(limit, other.s-cfg.MIN_GAP)
                car.s = max(car.s, min(car.s+cfg.SPEED*dt, limit))
            car.speed = max(0, (car.s-old_s)/dt)
            if getattr(cfg, "COLLECT_STATS", False):
                car.delay += dt * max(0, 1-car.speed/cfg.SPEED)

        for car in self.cars[:]:
            if car.stage == "exit" and car.s >= EXIT_LENGTH:
                self.completed += 1
                if getattr(cfg, "COLLECT_STATS", False):
                    self.total_delay += car.delay
                    self.total_travel += self.time-car.born
                self.cars.remove(car)
        # Учебный критерий конфликта: расстояние между центрами < 15.
        crossing = [(c, self.position(c)[0]) for c in self.cars if c.stage == "crossing"] if getattr(cfg, "COUNT_CONFLICTS", False) else []
        pairs = set()
        for i, (a, p) in enumerate(crossing):
            for b, q in crossing[i+1:]:
                if math.dist(p, q) < 15:
                    pairs.add(tuple(sorted((a.id, b.id))))
        self.conflicts += len(pairs-self.contact_pairs)
        self.contact_pairs = pairs
        self.colliding = {i for pair in pairs for i in pair}
        if getattr(cfg, "COLLECT_STATS", False) and self.time >= self.next_sample:
            self.history.append({"t": round(self.time, 1), **self.metrics()})
            self.next_sample = self.time + 1

    def metrics(self):
        pending = sum(map(len, self.pending.values()))
        return {"active": len(self.cars), "pending": pending,
                "queue": pending + sum(c.speed < 1 for c in self.cars),
                "generated": self.generated, "completed": self.completed,
                "conflicts": self.conflicts,
                "delay": round(self.total_delay / self.completed, 2) if self.completed else 0,
                "travel": round(self.total_travel / self.completed, 2) if self.completed else 0,
                "flow": round(self.completed * 3600 / self.time, 1) if self.time else 0}

    def signals(self):
        return {s: [[self.green(s, lane, turn) for turn in TURNS] for lane in range(3)] for s in SIDES}

    def snapshot(self):
        cars = []
        for car in self.cars:
            p, angle = self.position(car)
            cars.append({"id": car.id, "x": p[0], "y": p[1], "angle": angle,
                         "side": car.side, "turn": car.turn, "speed": round(car.speed, 1),
                         "conflict": car.id in self.colliding})
        return {"time": round(self.time, 2), "cars": cars, "metrics": self.metrics(),
                "features": {"signals": hasattr(self.config, "green"),
                             "conflicts": getattr(self.config, "COUNT_CONFLICTS", False),
                             "stats": getattr(self.config, "COLLECT_STATS", False)},
                "signals": self.signals(), "history": list(self.history),
                "pending": {s: len(q) for s, q in self.pending.items()},
                "connections": self.connections,
                "routes": [{"key": list(r.key), "points": r.points} for r in self.routes.values()]}
