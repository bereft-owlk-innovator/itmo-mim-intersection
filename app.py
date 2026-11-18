"""python app.py — локальный сервер, без pip/npm. Python 3.10+."""
import argparse
import csv
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
import traceback
import types

from simulation import Simulation, default_connections, validate_connections

ROOT = Path(__file__).resolve().parent


def load_scenario():
    module = types.ModuleType("scenario")
    path = ROOT / "scenario.py"
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


class App:
    def __init__(self):
        self.lock = threading.RLock()
        self.running, self.speed = True, 1.0
        self.error = ""
        self.revision = 0
        path = ROOT / "connections.json"
        config = load_scenario()
        self.connections = validate_connections(json.loads(path.read_text())) if path.exists() else getattr(config, "INITIAL_CONNECTIONS", [])
        self.sim = Simulation(config, self.connections)
        self.last_state = self.sim.snapshot()
        self.stamp = (ROOT / "scenario.py").stat().st_mtime_ns

    def reset(self):
        config = load_scenario()
        connections = self.connections
        # Правка начального графа в Python заменяет граф. Остальные правки
        # сохраняют связи, нарисованные мышью.
        if getattr(config, "INITIAL_CONNECTIONS", []) != getattr(self.sim.config, "INITIAL_CONNECTIONS", []):
            connections = getattr(config, "INITIAL_CONNECTIONS", [])
        candidate = Simulation(config, connections)
        if connections != self.connections:
            (ROOT / "connections.json").write_text(json.dumps(connections, indent=2)+"\n", encoding="utf-8")
        self.sim = candidate
        self.connections = candidate.connections
        self.error = ""
        self.revision += 1

    def loop(self):
        previous, accumulator = time.monotonic(), 0.0
        while True:
            time.sleep(0.02)
            now = time.monotonic()
            elapsed, previous = min(0.2, now-previous), now
            with self.lock:
                try:
                    stamp = (ROOT / "scenario.py").stat().st_mtime_ns
                    if stamp != self.stamp:
                        self.stamp = stamp
                        self.reset()
                        accumulator = 0
                    if self.running and not self.error:
                        accumulator += elapsed*self.speed
                        while accumulator >= 0.05:
                            self.sim.step(0.05)
                            accumulator -= 0.05
                    else:
                        accumulator = 0
                except Exception:
                    self.error = traceback.format_exc(limit=4)

    def state(self):
        try:
            self.last_state = self.sim.snapshot()
        except Exception:
            self.error = traceback.format_exc(limit=4)
        state = dict(self.last_state)
        state.update(running=self.running, speed=self.speed, error=self.error, revision=self.revision)
        return state


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, body, kind="application/json; charset=utf-8", status=200):
        if not isinstance(body, bytes):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            if self.path == "/api/state":
                with self.server.app.lock:
                    self.send(json.dumps(self.server.app.state()))
            elif self.path == "/api/stats.csv":
                with self.server.app.lock:
                    if not getattr(self.server.app.sim.config, "COLLECT_STATS", False):
                        self.send("Статистика ещё не включена", "text/plain; charset=utf-8", 404)
                        return
                    rows = list(self.server.app.sim.history)
                stream = io.StringIO()
                if rows:
                    writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
                    writer.writeheader()
                    writer.writerows(rows)
                self.send(stream.getvalue(), "text/csv; charset=utf-8")
            elif self.path in ("/", "/app.js", "/style.css"):
                name, kind = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}[self.path]
                self.send((ROOT / "web" / name).read_bytes(), kind+"; charset=utf-8")
            else:
                self.send("Not found", "text/plain", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.send(json.dumps({"error": str(exc)}), status=500)

    def do_POST(self):
        # Локальное приложение: запрещаем команды от сторонних веб-страниц.
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + self.headers.get("Host", ""):
            self.send("Forbidden", "text/plain", 403)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size < 20000:
                raise ValueError("Некорректный размер запроса")
            data = json.loads(self.rfile.read(size))
            app = self.server.app
            with app.lock:
                if self.path == "/api/control":
                    action = data["action"]
                    if action == "toggle":
                        app.running = not app.running
                    elif action == "step":
                        app.running = False
                        if not app.error:
                            for _ in range(10):
                                app.sim.step(0.05)
                    elif action == "reset":
                        app.reset()
                    elif action == "speed":
                        speed = float(data["value"])
                        if speed not in (0.5, 1, 2, 4, 8):
                            raise ValueError("Недопустимая скорость")
                        app.speed = speed
                    else:
                        raise ValueError("Неизвестная команда")
                elif self.path == "/api/connections":
                    connections = validate_connections(data["connections"])
                    # Сначала подготовить граф, затем сохранить и применить.
                    candidate = Simulation(load_scenario(), connections)
                    (ROOT / "connections.json").write_text(json.dumps(connections, indent=2)+"\n", encoding="utf-8")
                    app.connections = candidate.connections
                    app.sim.set_connections(connections)
                else:
                    self.send("Not found", "text/plain", 404)
                    return
                self.send(json.dumps({"ok": True}))
        except Exception as exc:
            self.send(json.dumps({"error": str(exc)}), status=400)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    app = App()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.app = app
    threading.Thread(target=app.loop, daemon=True).start()
    print(f"Server started: http://127.0.0.1:{args.port}", flush=True)
    print("Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer process stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
