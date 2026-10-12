#!/usr/bin/env python3
"""API HTTP de DeckDoctor: expone el estado de la Deck en JSON y en formato Prometheus.

Solo lectura y protegida con token. Comprueba en segundo plano cada `interval` segundos; las
comprobaciones lentas o que tiran de internet (SteamOS, Flatpak) cada `slow_interval`.
"""

import hmac
import importlib.util
import json
import os
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deckdoctor as dd  # noqa: E402

VERSION = "1.0"
CONFIG = os.path.join(dd.HOME, ".config/deckdoctor/api.json")
DEFAULTS = {"port": 9555, "bind": "0.0.0.0", "interval": 300, "slow_interval": 21600, "games_interval": 3600,
            "token": ""}
SLOW = {"check_steamos", "check_flatpak"}
STATUS_VALUE = {"ok": 0, "info": 1, "warn": 2, "bad": 3}
METRIC_HELP = {
    "ssh_active": "SSH en marcha (1/0)",
    "ssh_password_auth": "SSH acepta contraseña (1/0)",
    "ssh_keys": "Claves SSH autorizadas",
    "suspend_on_ac": "Se suspende sola estando enchufada, modo escritorio (1/0)",
    "decky_active": "Decky Loader en marcha (1/0)",
    "decky_plugins": "Plugins de Decky instalados",
    "failed_units": "Unidades de systemd fallidas (sin contar systemd-repart)",
    "steamos_update_available": "Hay actualización de SteamOS (1/0)",
    "uptime_seconds": "Segundos desde el arranque",
    "battery_health_percent": "Capacidad actual de la batería frente a la original (%)",
    "battery_charge_percent": "Carga de la batería (%)",
    "battery_charging": "Batería cargando (1/0)",
    "battery_plugged": "Enchufada a la corriente (1/0)",
    "battery_cycles": "Ciclos de carga de la batería",
    "temperature_celsius": "Temperatura por sensor (°C)",
    "disk_free_bytes": "Espacio libre por unidad (bytes)",
    "disk_total_bytes": "Tamaño de cada unidad (bytes)",
    "sd_mounted": "microSD montada (1/0)",
    "flatpak_updates": "Actualizaciones de Flatpak pendientes",
    "journal_bytes": "Tamaño de los registros del sistema (bytes)",
    "backups_bytes": "Tamaño de ~/Backups (bytes)",
    "backups_oldest_days": "Antigüedad de la copia más vieja de ~/Backups (días)",
    "trash_bytes": "Tamaño de la papelera (bytes)",
    "shadercache_bytes": "Tamaño de la caché de shaders de Steam (bytes)",
    "api_active": "API de DeckDoctor en marcha (1/0)",
    "games_installed": "Juegos instalados por tipo (steam, tool = Proton y runtimes, non_steam)",
    "games_bytes": "Lo que ocupan los juegos de Steam por unidad, con Proton y shaders si ya se midieron (bytes)",
    "games_unplayed_6m": "Juegos de Steam sin jugar en 6 meses",
    "games_recoverable_bytes": "Espacio que DeckSpace podría limpiar (restos, bytes)",
    "games_sized_timestamp_seconds": "Hora del último análisis completo de tamaños (Unix)",
}
UNPLAYED_DAYS = 182


def load_deckspace():
    """Importa deckspace.py si DeckSpace está instalada (~/Apps/DeckSpace o junto a esta carpeta). Si no, None."""
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.path.join(dd.APPS, "DeckSpace"), os.path.join(os.path.dirname(here), "DeckSpace")):
        path = os.path.join(d, "deckspace.py")
        if os.path.exists(path):
            spec = importlib.util.spec_from_file_location("deckspace", path)
            mod = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(mod)
                return mod
            except Exception as e:  # noqa: BLE001
                print(f"No se pudo cargar DeckSpace ({path}): {e}", file=sys.stderr, flush=True)
    return None


def load_config():
    """Lee la configuración; si no existe (o falta el token) la crea con un token aleatorio."""
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    if not cfg["token"]:
        cfg["token"] = secrets.token_urlsafe(24)
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    return cfg


class State:
    """Últimos resultados. Un hilo los refresca; las peticiones solo los leen."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.fast, self.slow = [], []
        self.fast_at = self.slow_at = 0.0
        self.duration = 0.0
        # Juegos (de DeckSpace): la lista se lee en cada comprobación; los tamaños completos, cada games_interval
        self.ds = load_deckspace()
        self.games, self.non_steam, self.games_at = {}, {}, 0.0
        self.sized, self.recoverable, self.sized_at = {}, None, 0.0
        self.size_wake = threading.Event()

    def refresh_games(self):
        """Lista rápida: appmanifest y shortcuts.vdf, sin medir carpetas."""
        if not self.ds:
            return
        games, non_steam = self.ds.installed_games(), self.ds.shortcuts()
        with self.lock:
            self.games, self.non_steam, self.games_at = games, non_steam, time.time()

    def size_loop(self):
        """Análisis completo de DeckSpace (du de cada juego, Proton y shaders), en su hilo y con prioridad baja."""
        try:
            os.setpriority(os.PRIO_PROCESS, 0, 10)  # en Linux afecta a este hilo y a los du que lance
        except OSError:
            pass
        while True:
            try:
                res = self.ds.analyze(lambda *a: None)
                with self.lock:
                    self.sized = {g["appid"]: g for g in res["games"]}
                    self.recoverable, self.sized_at = res["orphan_total"], time.time()
            except Exception as e:  # noqa: BLE001
                print(f"Error al analizar los juegos: {e}", file=sys.stderr, flush=True)
            self.size_wake.wait(self.cfg["games_interval"])
            self.size_wake.clear()

    def refresh(self, force_slow=False):
        start = time.time()
        fast = dd.run_all(checks=[c for c in dd.CHECKS if c.__name__ not in SLOW])
        slow = None
        if force_slow or start - self.slow_at >= self.cfg["slow_interval"]:
            slow = dd.run_all(checks=[c for c in dd.CHECKS if c.__name__ in SLOW])
        try:
            self.refresh_games()
        except Exception as e:  # noqa: BLE001
            print(f"Error al leer los juegos: {e}", file=sys.stderr, flush=True)
        with self.lock:
            self.fast, self.fast_at = fast, time.time()
            if slow is not None:
                self.slow, self.slow_at = slow, time.time()
            self.duration = time.time() - start

    def loop(self):
        while True:
            forced = self.wake.wait(self.cfg["interval"])  # True si alguien pidió POST /api/refresh
            self.wake.clear()
            if forced:
                self.size_wake.set()
            try:
                self.refresh(force_slow=forced)
            except Exception as e:  # noqa: BLE001
                print(f"Error al comprobar: {e}", file=sys.stderr, flush=True)

    def snapshot(self):
        with self.lock:
            return dd.sort_results(self.fast + self.slow), self.fast_at, self.slow_at, self.duration

    def games_snapshot(self):
        with self.lock:
            return (dict(self.games), dict(self.non_steam), self.games_at, dict(self.sized), self.recoverable,
                    self.sized_at)


# ---------------------------------------------------------------- formatos de salida

def games_json(state):
    """Juegos instalados con su tamaño. Tamaños de Proton y shaders: los del último análisis completo."""
    if not state.ds:
        return {"available": False, "error": "DeckSpace no está instalada (~/Apps/DeckSpace)"}
    games, non_steam, listed_at, sized, recoverable, sized_at = state.games_snapshot()
    out, by_drive, cutoff = [], {}, time.time() - UNPLAYED_DAYS * 86400
    for g in games.values():
        full = sized.get(g["appid"], {})
        item = {"appid": g["appid"], "name": g["name"], "drive": g["drive"], "tool": g["tool"],
                "installed": bool(g["state"] & 4), "last_played": g["last_played"] or None,
                "game_bytes": g["size"], "proton_bytes": full.get("compat"), "shader_bytes": full.get("shader")}
        item["total_bytes"] = g["size"] + (item["proton_bytes"] or 0) + (item["shader_bytes"] or 0)
        out.append(item)
        if not g["tool"]:
            by_drive[g["drive"]] = by_drive.get(g["drive"], 0) + item["total_bytes"]
    out.sort(key=lambda i: -i["total_bytes"])
    steam = [i for i in out if not i["tool"]]
    return {"available": True, "listed_at": listed_at, "sized_at": sized_at or None,
            "summary": {"games": len(steam), "tools": len(out) - len(steam), "non_steam": len(non_steam),
                        "bytes_by_drive": by_drive, "total_bytes": sum(by_drive.values()),
                        "unplayed_6m": sum(1 for i in steam if (i["last_played"] or 0) < cutoff),
                        "recoverable_bytes": recoverable},
            "games": out,
            "non_steam": sorted(({"appid": a, "name": n} for a, n in non_steam.items()), key=lambda i: i["name"].lower())}


def games_metrics(state):
    """Totales de juegos como métricas (la lista juego a juego solo va en /api/games)."""
    data = games_json(state)
    if not data["available"]:
        return []
    s = data["summary"]
    m = [dd.metric("games_installed", s["games"], kind="steam"), dd.metric("games_installed", s["tools"], kind="tool"),
         dd.metric("games_installed", s["non_steam"], kind="non_steam"),
         dd.metric("games_unplayed_6m", s["unplayed_6m"])]
    m += [dd.metric("games_bytes", v, drive=k) for k, v in s["bytes_by_drive"].items()]
    if data["sized_at"]:
        m += [dd.metric("games_recoverable_bytes", s["recoverable_bytes"]),
              dd.metric("games_sized_timestamp_seconds", round(data["sized_at"]))]
    return m


def public(r):
    """Resultado sin la función de arreglo (no se puede ejecutar desde la API)."""
    return {"id": r["id"], "group": r["group"], "title": r["title"], "status": r["status"],
            "detail": r["detail"], "fix": r["fix"]["label"] if r["fix"] else None, "metrics": r["metrics"]}


def summary(results):
    counts = {k: sum(r["status"] == k for r in results) for k in STATUS_VALUE}
    worst = max((r["status"] for r in results), key=STATUS_VALUE.get, default="ok")
    return {"status": worst, "counts": counts}


def flat_metrics(results, extra=()):
    """{nombre: valor} o, si la métrica tiene una etiqueta, {nombre: {valor_etiqueta: valor}}."""
    out = {}
    for m in [m for r in results for m in r["metrics"]] + list(extra):
        if m["labels"]:
            out.setdefault(m["name"], {})[next(iter(m["labels"].values()))] = m["value"]
        else:
            out[m["name"]] = m["value"]
    return out


def health_json(state):
    results, fast_at, slow_at, duration = state.snapshot()
    return {"host": socket.gethostname(), "version": VERSION,
            "checked_at": fast_at, "slow_checked_at": slow_at, "duration_seconds": round(duration, 3),
            "summary": summary(results), "metrics": flat_metrics(results, games_metrics(state)),
            "checks": [public(r) for r in results]}


def num(v):
    v = float(v)
    return str(int(v)) if v.is_integer() else repr(v)


def esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def prometheus(state):
    results, fast_at, slow_at, duration = state.snapshot()
    families = {}  # nombre -> (ayuda, [(etiquetas, valor)]); Prometheus exige cada familia seguida

    def add(name, value, help_, labels=None):
        families.setdefault(f"deckdoctor_{name}", (help_, []))[1].append((labels or {}, value))

    for r in results:
        add("check_status", STATUS_VALUE[r["status"]], "Estado de cada comprobación: 0 ok, 1 info, 2 aviso, 3 problema",
            {"id": r["id"], "group": r["group"], "title": r["title"]})
    for k, v in summary(results)["counts"].items():
        add("checks", v, "Número de comprobaciones por estado", {"status": k})
    for r in results:
        for m in r["metrics"]:
            add(m["name"], m["value"], METRIC_HELP.get(m["name"], r["title"]), m["labels"])
    for m in games_metrics(state):
        add(m["name"], m["value"], METRIC_HELP[m["name"]], m["labels"])
    add("last_check_timestamp_seconds", fast_at, "Hora de la última comprobación (Unix)")
    add("last_slow_check_timestamp_seconds", slow_at, "Hora de la última comprobación de SteamOS y Flatpak (Unix)")
    add("check_duration_seconds", duration, "Lo que tardó la última comprobación")

    lines = []
    for name, (help_, samples) in families.items():
        lines += [f"# HELP {name} {help_}", f"# TYPE {name} gauge"]
        for labels, value in samples:
            lab = ",".join(f'{k}="{esc(v)}"' for k, v in labels.items())
            lines.append(f"{name}{{{lab}}} {num(value)}" if lab else f"{name} {num(value)}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- servidor

def make_handler(state):
    token = state.cfg["token"]

    class Handler(BaseHTTPRequestHandler):
        server_version = f"DeckDoctor/{VERSION}"

        def log_message(self, fmt, *args):  # solo errores en el journal
            pass

        def send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body.encode() if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            auth = self.headers.get("Authorization", "")
            given = auth[7:] if auth.startswith("Bearer ") else \
                parse_qs(urlparse(self.path).query).get("token", [""])[0]
            return hmac.compare_digest(given.encode(), token.encode())

        def do_OPTIONS(self):  # CORS para dashboards web en otro origen
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Authorization")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()

        def do_GET(self):
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/api/ping":
                return self.send(200, {"ok": True, "version": VERSION})
            if not self.authorized():
                return self.send(401, {"error": "token incorrecto o ausente"})
            if path == "/api/health":
                return self.send(200, health_json(state))
            if path == "/api/metrics":
                data = health_json(state)
                return self.send(200, {"checked_at": data["checked_at"], "status": data["summary"]["status"],
                                       "metrics": data["metrics"]})
            if path == "/api/games":
                return self.send(200, games_json(state))
            if path == "/metrics":
                return self.send(200, prometheus(state), "text/plain; version=0.0.4; charset=utf-8")
            self.send(404, {"error": "no existe", "routes": ["/api/ping", "/api/health", "/api/metrics",
                                                              "/api/games", "/metrics", "POST /api/refresh"]})

        def do_POST(self):
            path = urlparse(self.path).path.rstrip("/")
            if not self.authorized():
                return self.send(401, {"error": "token incorrecto o ausente"})
            if path == "/api/refresh":
                state.wake.set()
                return self.send(202, {"ok": True, "message": "Comprobación lanzada"})
            self.send(404, {"error": "no existe"})

    return Handler


def main():
    cfg = load_config()
    state = State(cfg)
    state.refresh(force_slow=True)
    threading.Thread(target=state.loop, daemon=True).start()
    if state.ds:
        threading.Thread(target=state.size_loop, daemon=True).start()
    server = ThreadingHTTPServer((cfg["bind"], int(cfg["port"])), make_handler(state))
    print(f"DeckDoctor API {VERSION} en http://{cfg['bind']}:{cfg['port']}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
