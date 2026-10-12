#!/usr/bin/env python3
"""DeckDoctor: chequeo de salud de la Steam Deck, con arreglos seguros para cada aviso."""

import datetime as dt
import glob
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from concurrent.futures import ThreadPoolExecutor
from tkinter import ttk

HOME = os.path.expanduser("~")
BASHRC = os.path.join(HOME, ".bashrc")
KXKBRC = os.path.join(HOME, ".config/kxkbrc")
BACKUPS = os.path.join(HOME, "Backups")
TRASH = os.path.join(HOME, ".local/share/Trash")
SD_ROOT = "/run/media/deck"
APPS = os.path.join(HOME, "Apps")
APP_DIR = os.path.dirname(os.path.abspath(__file__))  # donde esté instalada (normalmente ~/Apps/DeckDoctor)

PATH_BLOCK = """
# Binarios del usuario (claude, etc.)
case ":$PATH:" in
\t*":$HOME/.local/bin:"*) ;;
\t*) export PATH="$HOME/.local/bin:$PATH" ;;
esac
"""
CONFIG = os.path.join(HOME, ".config/deckdoctor/config.json")
API_UNIT = "deckdoctor-api.service"
API_UNIT_SRC = os.path.join(APP_DIR, API_UNIT)
API_UNIT_DST = os.path.join(HOME, ".config/systemd/user", API_UNIT)
API_CONFIG = os.path.join(HOME, ".config/deckdoctor/api.json")
IGNORED_FAILED = {"systemd-repart.service"}  # falla en cada arranque en SteamOS y no afecta a nada

# Tema oscuro inspirado en la Steam Deck (el mismo que DeckSpace)
BG, SURFACE, SURFACE2, BORDER = "#0e1319", "#161d26", "#1e2732", "#2b3644"
FG, MUTED, ACCENT, ACCENT_DARK = "#e6edf3", "#8b98a5", "#1a9fff", "#1d4f7c"
OK, WARN, DANGER = "#3ecf8e", "#f2b84b", "#ff6b6b"

STATUS = {  # orden de gravedad, color y texto
    "bad": (0, DANGER, "Problema"),
    "warn": (1, WARN, "Aviso"),
    "info": (2, ACCENT, "Info"),
    "ok": (3, OK, "Correcto"),
}
GROUPS = ["Seguridad", "Personalizaciones", "Sistema", "Hardware", "Espacio y mantenimiento", "Integraciones"]


# ---------------------------------------------------------------- utilidades

def sh(cmd, timeout=30):
    """Ejecuta una orden y devuelve (código, salida). Nunca lanza excepciones."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


def read(path, default=""):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024


def du(path):
    code, out = sh(["du", "-s", "-B1", path], timeout=60)
    try:
        return int(out.split()[0])
    except (ValueError, IndexError):
        return 0


def result(group, title, status, detail, fix=None, metrics=(), id=None):
    """fix = {"label", "fn", "confirm" (opcional), "danger" (opcional)}; fn() devuelve un mensaje.
    metrics = lista de metric(); id = identificador estable para la API (por defecto, el de la comprobación)."""
    return {"id": id, "group": group, "title": title, "status": status, "detail": detail, "fix": fix,
            "metrics": list(metrics)}


def metric(name, value, **labels):
    """Dato numérico para la API y Prometheus (sin el prefijo deckdoctor_)."""
    return {"name": name, "value": float(value), "labels": labels}


def open_path(path):
    subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"Abierto {path}"


def load_config():
    """Preferencias de esta Deck (~/.config/deckdoctor/config.json). Hoy solo "keyboard": la distribución
    de teclado que se espera en KDE ("es"…); sin ella no se comprueba el teclado."""
    import json
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def launch_app(name):
    script = os.path.join(APPS, name, f"{name.lower()}.py")
    if not os.path.exists(script):
        raise RuntimeError(f"{name} no está instalada en {os.path.dirname(script)}")
    subprocess.Popen([sys.executable, script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return f"Abierto {name}"


def run_checked(cmd, ok_msg, timeout=120):
    code, out = sh(cmd, timeout=timeout)
    if code != 0:
        raise RuntimeError(f"Falló «{' '.join(cmd)}»:\n{out[-600:]}")
    return ok_msg


def sd_mount():
    for p in sorted(glob.glob(os.path.join(SD_ROOT, "*"))):
        if os.path.ismount(p):
            return p
    return None


# ---------------------------------------------------------------- comprobaciones: seguridad

SSH_KEYONLY = "/etc/ssh/sshd_config.d/10-solo-clave.conf"
SSH_KEYONLY_CONF = "# DeckDoctor: SSH solo con clave (borrar este archivo para volver a permitir la contraseña)\n" \
                   "PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n"


def ssh_keys():
    path = os.path.join(HOME, ".ssh/authorized_keys")
    return [l for l in read(path).splitlines() if l.strip() and not l.startswith("#")]


def ssh_key_login_seen():
    """¿Alguien ha entrado ya con clave? Así sabemos que quitar la contraseña no deja a nadie fuera."""
    _, out = sh(["journalctl", "-u", "sshd", "--no-pager", "-q", "-g", "Accepted publickey for"])
    return "Accepted publickey" in out


def ssh_password_off():
    """Solo clave: escribe el archivo, lo valida con sshd -t y recarga (si falla, lo quita)."""
    script = (f"printf '%s' '{SSH_KEYONLY_CONF}' > {SSH_KEYONLY} && chmod 644 {SSH_KEYONLY} && sshd -t && "
              f"{{ ! systemctl is-active -q sshd || systemctl reload sshd; }} || {{ rm -f {SSH_KEYONLY}; exit 1; }}")
    return run_checked(["pkexec", "sh", "-c", script], "Entrada con contraseña desactivada: solo con clave")


def ssh_password_on():
    script = f"rm -f {SSH_KEYONLY} && sshd -t && {{ ! systemctl is-active -q sshd || systemctl reload sshd; }}"
    return run_checked(["pkexec", "sh", "-c", script], "Entrada con contraseña activada")


def check_ssh():
    G = "Seguridad"
    _, active = sh(["systemctl", "is-active", "sshd"])
    _, enabled = sh(["systemctl", "is-enabled", "sshd"])
    on = active == "active" or enabled == "enabled"
    keys = ssh_keys()
    conf = read("/etc/ssh/sshd_config") + "".join(read(p) for p in glob.glob("/etc/ssh/sshd_config.d/*.conf"))
    pw_off = any(l.split()[:2] == ["PasswordAuthentication", "no"] for l in conf.splitlines())
    n = f"{len(keys)} clave{'s' if len(keys) != 1 else ''} autorizada{'s' if len(keys) != 1 else ''}"

    # 1) Servidor SSH: encendido o apagado
    if not on:
        server = result(G, "Servidor SSH", "ok", f"Apagado: nadie puede entrar por la red ({n}).",
                        {"label": "Activar SSH",
                         "confirm": "Se arrancará el servidor SSH y se activará al arrancar. Pedirá tu contraseña.\n\n"
                                    + ("Solo se podrá entrar con clave." if pw_off else
                                       "OJO: la entrada con contraseña está permitida; cualquiera de tu red podrá "
                                       "intentarlo. Desactívala abajo si ya tienes una clave."),
                         "fn": lambda: run_checked(["pkexec", "systemctl", "enable", "--now", "sshd"],
                                                   "SSH activado")},
                        [metric("ssh_active", 0), metric("ssh_keys", len(keys))], id="ssh")
    else:
        estado = "Encendido" if active == "active" else "Se enciende al arrancar (ahora parado)"
        ips = ", ".join(lan_ips())
        server = result(G, "Servidor SSH", "ok", f"{estado} en el puerto 22 ({ips}), {n}.",
                        {"label": "Desactivar SSH", "danger": True,
                         "confirm": "Se parará el servidor SSH y no se activará al arrancar. Pedirá tu contraseña.\n\n"
                                    "Las sesiones abiertas desde otro equipo se cortarán y no podrás volver a entrar "
                                    "hasta que lo actives de nuevo.",
                         "fn": lambda: run_checked(["pkexec", "systemctl", "disable", "--now", "sshd"],
                                                   "SSH desactivado")},
                        [metric("ssh_active", active == "active"), metric("ssh_keys", len(keys))], id="ssh")

    # 2) Entrada con contraseña: permitida o solo clave
    pm = [metric("ssh_password_auth", not pw_off)]
    if pw_off:
        if os.path.exists(SSH_KEYONLY):
            fix = {"label": "Permitir contraseña", "danger": True, "fn": ssh_password_on,
                   "confirm": "Se volverá a poder entrar por SSH con la contraseña de deck, además de con clave. "
                              "Pedirá tu contraseña.\n\nCualquiera de tu red podrá intentarlo: úsalo solo para "
                              "añadir una clave nueva y vuelve a desactivarlo."}
            detail = f"Solo se entra con clave ({n})."
        else:
            fix, detail = None, f"Solo se entra con clave ({n}); lo fija la configuración de sshd, no DeckDoctor."
        password = result(G, "Entrada SSH con contraseña", "ok", detail, fix, pm, id="ssh_password")
    elif keys:
        seen = ssh_key_login_seen()
        fix = {"label": "Solo con clave", "fn": ssh_password_off, "danger": not seen,
               "confirm": f"Se desactivará la entrada con contraseña; solo se podrá entrar con clave ({n}). "
                          "Las sesiones abiertas no se cortan. Pedirá tu contraseña.\n\n"
                          + ("Ya se ha entrado con clave alguna vez, así que no te quedarás fuera."
                             if seen else "OJO: aún nadie ha entrado con clave. Pruébalo antes desde el otro equipo "
                                          "(ssh deck@IP) o te podrías quedar fuera.")}
        password = result(G, "Entrada SSH con contraseña", "warn" if on else "info",
                          "Permitida: se puede entrar con la contraseña de deck además de con clave."
                          + ("" if on else " (SSH está apagado, así que ahora no importa)."), fix, pm,
                          id="ssh_password")
    else:
        password = result(G, "Entrada SSH con contraseña", "warn" if on else "info",
                          "Permitida y sin claves: es la única forma de entrar. Para quitarla, añade antes tu clave "
                          "desde el otro equipo (ssh-copy-id deck@IP) y vuelve a comprobar.", None, pm,
                          id="ssh_password")
    return [server, password]


def check_password():
    G = "Seguridad"
    code, out = sh(["passwd", "-S", os.environ.get("USER", "deck")])
    parts = out.split()
    if code != 0 or len(parts) < 3:
        return [result(G, "Contraseña de deck", "info", "No se pudo consultar.")]
    if parts[1] == "P":
        return [result(G, "Contraseña de deck", "ok", f"Establecida (último cambio: {parts[2]}).")]
    return [result(G, "Contraseña de deck", "info",
                   "Sin contraseña: sudo no funciona. Se pone con «passwd» en una terminal.")]


# ---------------------------------------------------------------- comprobaciones: personalizaciones

def fix_path():
    with open(BASHRC, "a", encoding="utf-8") as f:
        f.write(PATH_BLOCK)
    return "Añadido ~/.local/bin al PATH en ~/.bashrc (vale para terminales nuevas)"


def check_path():
    G = "Personalizaciones"
    if not os.path.isdir(os.path.join(HOME, ".local/bin")):
        return []  # nada que poner en el PATH
    if ".local/bin" in read(BASHRC):
        ok = os.path.exists(os.path.join(HOME, ".local/bin/claude"))
        extra = "" if ok else " (aunque ~/.local/bin/claude no existe)"
        return [result(G, "PATH con ~/.local/bin", "ok" if ok else "info",
                       f"~/.bashrc lo añade: «claude» funciona en cualquier terminal{extra}.")]
    return [result(G, "PATH con ~/.local/bin", "bad",
                   "~/.bashrc no añade ~/.local/bin al PATH: «claude» no se encontrará en terminales nuevas.",
                   {"label": "Arreglar", "fn": fix_path})]


def fix_keyboard(layout):
    run_checked(["kwriteconfig6", "--file", "kxkbrc", "--group", "Layout", "--key", "LayoutList", layout], "")
    run_checked(["kwriteconfig6", "--file", "kxkbrc", "--group", "Layout", "--key", "Use", "true"], "")
    sh(["dbus-send", "--session", "--type=signal", "/Layouts", "org.kde.keyboard.reloadConfig"])
    return f"Teclado «{layout}» aplicado"


def check_keyboard():
    G = "Personalizaciones"
    wanted = load_config().get("keyboard")
    if not wanted:
        return []  # solo si se ha elegido una distribución en config.json
    title = "Teclado en español" if wanted == "es" else f"Teclado «{wanted}»"
    layouts = ""
    for line in read(KXKBRC).splitlines():
        if line.startswith("LayoutList="):
            layouts = line.split("=", 1)[1]
    if layouts.split(",")[0] == wanted:
        return [result(G, title, "ok", f"Distribución de KDE: {layouts}.")]
    return [result(G, title, "bad",
                   f"La distribución es «{layouts or 'la predeterminada (us)'}» y se espera «{wanted}»: algunas "
                   "teclas y símbolos saldrán cambiados.",
                   {"label": "Arreglar", "fn": lambda: fix_keyboard(wanted)})]


def check_nsl():
    G = "Personalizaciones"
    _, files = sh(["systemctl", "--user", "list-unit-files", "nslgamescanner.service", "--no-legend"])
    if "nslgamescanner" not in files:
        return []  # NonSteamLaunchers no está instalado
    _, enabled = sh(["systemctl", "--user", "is-enabled", "nslgamescanner.service"])
    _, active = sh(["systemctl", "--user", "is-active", "nslgamescanner.service"])
    if enabled in ("disabled", "masked") or "not-found" in enabled or enabled == "":
        if active != "active":
            return [result(G, "NonSteamLaunchers desactivado", "ok",
                           "nslgamescanner no está activo y no reescribirá shortcuts.vdf.")]
    return [result(G, "NonSteamLaunchers desactivado", "bad",
                   f"nslgamescanner vuelve a estar {active}/{enabled}: reescribe shortcuts.vdf "
                   "y puede borrar o duplicar accesos de juegos no de Steam.",
                   {"label": "Desactivar", "fn": lambda: run_checked(
                       ["systemctl", "--user", "disable", "--now", "nslgamescanner.service"],
                       "nslgamescanner desactivado")})]


def check_decky():
    G = "Personalizaciones"
    if not os.path.isdir(os.path.join(HOME, "homebrew")):
        return [result(G, "Decky Loader", "info", "No está instalado (es opcional; se instala desde decky.xyz).")]
    _, active = sh(["systemctl", "is-active", "plugin_loader"])
    plugins = sorted(os.listdir(os.path.join(HOME, "homebrew/plugins"))) \
        if os.path.isdir(os.path.join(HOME, "homebrew/plugins")) else []
    m = [metric("decky_active", active == "active"), metric("decky_plugins", len(plugins))]
    if active == "active":
        return [result(G, "Decky Loader", "ok", f"Funcionando con {len(plugins)} plugins: {', '.join(plugins)}.",
                       metrics=m)]
    return [result(G, "Decky Loader", "warn",
                   "El servicio plugin_loader no está activo (una actualización de SteamOS puede romperlo). "
                   "Se reinstala desde decky.xyz.",
                   {"label": "Reiniciar servicio", "fn": lambda: run_checked(
                       ["pkexec", "systemctl", "restart", "plugin_loader"], "plugin_loader reiniciado")}, m)]


# ---------------------------------------------------------------- comprobaciones: sistema

def check_failed():
    G = "Sistema"
    out, m = [], []
    for scope, label in ((["systemctl"], "sistema"), (["systemctl", "--user"], "usuario")):
        _, text = sh(scope + ["--failed", "--no-legend", "--plain"])
        units = [l.split()[0] for l in text.splitlines() if l.strip() and not l.startswith("0 ")]
        units = [u for u in units if u.endswith((".service", ".socket", ".mount", ".timer"))
                 and u not in IGNORED_FAILED]
        m.append(metric("failed_units", len(units), scope="system" if label == "sistema" else "user"))
        if units:
            out.append(result(G, f"Servicios fallidos ({label})", "warn",
                              "Han fallado: " + ", ".join(units) + ". Míralo con «journalctl -b -u <servicio>».",
                              id=f"failed_{'system' if label == 'sistema' else 'user'}"))
    if out:
        out[0]["metrics"] = m
        return out
    return [result(G, "Servicios", "ok", "Ninguno ha fallado (systemd-repart falla siempre en SteamOS y se ignora).",
                   metrics=m)]


def check_steamos():
    G = "Sistema"
    rel = dict(l.split("=", 1) for l in read("/etc/os-release").splitlines() if "=" in l)
    version = f"SteamOS {rel.get('VERSION_ID', '?')} ({rel.get('BUILD_ID', '?')})"
    code, out = sh(["steamos-update", "check"], timeout=60)
    if "No update available" in out:
        return [result(G, "Actualización de SteamOS", "ok", f"{version}: al día.",
                       metrics=[metric("steamos_update_available", 0)])]
    if code == 0 or "available" in out.lower():
        return [result(G, "Actualización de SteamOS", "info",
                       f"{version}: hay una actualización. Se instala desde el modo juego "
                       "(Ajustes → Sistema). Luego abre DeckDoctor para comprobar que todo sigue bien.",
                       metrics=[metric("steamos_update_available", 1)])]
    return [result(G, "Actualización de SteamOS", "info", f"{version}. No se pudo comprobar (¿sin conexión?).")]


def check_uptime():
    secs = float(read("/proc/uptime", "0").split()[0])
    days = secs / 86400
    status = "info" if days >= 7 else "ok"
    text = f"Encendida desde hace {int(days)} d {int(secs % 86400 // 3600)} h." if days >= 1 \
        else f"Encendida desde hace {int(secs // 3600)} h {int(secs % 3600 // 60)} min."
    if days >= 7:
        text += " Conviene reiniciar de vez en cuando."
    return [result("Sistema", "Tiempo encendida", status, text, metrics=[metric("uptime_seconds", round(secs))])]


def power_setting(profile, key):
    """Valor de powerdevil (modo escritorio): primero el del usuario y si no, el de /etc/xdg."""
    for path in (os.path.join(HOME, ".config/powerdevilrc"), "/etc/xdg/powerdevilrc"):
        group, found = None, None
        for line in read(path).splitlines():
            if line.startswith("["):
                group = line.strip()
            elif group == f"[{profile}][SuspendAndShutdown]" and line.startswith(f"{key}="):
                found = line.split("=", 1)[1].strip()
        if found is not None:
            return found
    return None


def fix_suspend():
    run_checked(["kwriteconfig6", "--file", "powerdevilrc", "--group", "AC", "--group", "SuspendAndShutdown",
                 "--key", "AutoSuspendAction", "0"], "")
    sh(["qdbus6", "org.kde.Solid.PowerManagement", "/org/kde/Solid/PowerManagement",
        "org.kde.Solid.PowerManagement.reparseConfiguration"])
    return "Enchufada ya no se suspende (modo escritorio)"


def check_suspend():
    G = "Sistema"
    ac = power_setting("AC", "AutoSuspendAction")
    bat_secs = power_setting("Battery", "AutoSuspendIdleTimeoutSec")
    bat = power_setting("Battery", "AutoSuspendAction")
    bat_txt = "con batería no se suspende" if bat == "0" else \
        f"con batería se suspende a los {int(bat_secs or 900) // 60} min de inactividad (también corta el SSH)"
    m = [metric("suspend_on_ac", ac != "0")]
    if ac == "0":
        return [result(G, "Suspensión (modo escritorio)", "ok",
                       f"Enchufada no se suspende; {bat_txt}. En modo juego manda Steam (Ajustes → Pantalla).",
                       metrics=m)]
    return [result(G, "Suspensión (modo escritorio)", "warn",
                   f"Enchufada se suspende sola: corta el SSH y la API. {bat_txt[0].upper() + bat_txt[1:]}.",
                   {"label": "No suspender enchufada", "fn": fix_suspend}, m)]


# ---------------------------------------------------------------- comprobaciones: hardware

def check_battery():
    G = "Hardware"
    bat = "/sys/class/power_supply/BAT1"
    try:
        full = int(read(f"{bat}/charge_full"))
        design = int(read(f"{bat}/charge_full_design"))
    except ValueError:
        return [result(G, "Batería", "info", "No se pudo leer.")]
    health = full * 100 / design
    cap = read(f"{bat}/capacity").strip()
    state = {"Charging": "cargando", "Discharging": "descargando", "Full": "llena",
             "Not charging": "enchufada sin cargar"}.get(read(f"{bat}/status").strip(), "")
    raw = read(f"{bat}/status").strip()
    m = [metric("battery_health_percent", round(health, 1)), metric("battery_charge_percent", int(cap or 0)),
         metric("battery_charging", raw == "Charging"), metric("battery_plugged", raw != "Discharging")]
    try:
        cycles = int(read(f"{bat}/cycle_count"))
        if cycles:  # la Deck suele dar 0 (no lo informa)
            m.append(metric("battery_cycles", cycles))
    except ValueError:
        pass
    status = "ok" if health >= 80 else "warn" if health >= 65 else "bad"
    text = f"Salud {health:.0f} % de la capacidad original. Ahora al {cap} % ({state})."
    if status != "ok":
        text += " Si suele estar enchufada, limita la carga al 80 % en Ajustes → Batería."
    return [result(G, "Batería", status, text, metrics=m)]


def check_temps():
    G = "Hardware"
    names = {"acpitz": "placa", "steamdeck_hwmon": "Deck", "nvme": "SSD", "amdgpu": "APU"}
    ids = {"placa": "board", "Deck": "deck", "SSD": "ssd", "APU": "apu"}
    temps = []
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        label = names.get(read(f"{d}/name").strip())
        if not label:
            continue
        vals = []
        for f in glob.glob(f"{d}/temp*_input"):
            try:
                vals.append(int(read(f)) / 1000)
            except ValueError:
                pass
        if vals:
            temps.append((label, max(vals)))
    if not temps:
        return [result(G, "Temperaturas", "info", "No se pudieron leer.")]
    hottest = max(t for _, t in temps)
    status = "ok" if hottest < 85 else "warn" if hottest < 95 else "bad"
    text = ", ".join(f"{n} {t:.0f} °C" for n, t in sorted(temps, key=lambda x: -x[1]))
    if status != "ok":
        text += ". Revisa que las rejillas no estén tapadas."
    return [result(G, "Temperaturas", status, text + ".",
                   metrics=[metric("temperature_celsius", round(t, 1), sensor=ids[n]) for n, t in temps])]


def check_disks():
    G = "Hardware"
    out = []
    u = shutil.disk_usage(HOME)
    free = u.free * 100 / u.total
    st = "ok" if free >= 10 else "warn" if free >= 5 else "bad"
    fix = None if st == "ok" else {"label": "Abrir DeckSpace", "fn": lambda: launch_app("DeckSpace")}
    out.append(result(G, "SSD interno", st, f"{human(u.free)} libres de {human(u.total)} ({free:.0f} %).", fix,
                      [metric("disk_free_bytes", u.free, disk="ssd"), metric("disk_total_bytes", u.total, disk="ssd")],
                      id="disk_ssd"))

    sd = sd_mount()
    if sd:
        u = shutil.disk_usage(sd)
        free = u.free * 100 / u.total
        st = "ok" if free >= 10 else "warn"
        emu = " Contiene Emulation/ de EmuDeck." if os.path.isdir(os.path.join(sd, "Emulation")) else ""
        out.append(result(G, "microSD", st, f"{human(u.free)} libres de {human(u.total)} ({free:.0f} %).{emu}",
                          metrics=[metric("sd_mounted", 1), metric("disk_free_bytes", u.free, disk="sd"),
                                   metric("disk_total_bytes", u.total, disk="sd")], id="disk_sd"))
    else:
        out.append(result(G, "microSD", "warn",
                          "No está montada: los emuladores de EmuDeck y las copias de SaveVault no estarán.",
                          metrics=[metric("sd_mounted", 0)], id="disk_sd"))
    return out


# ---------------------------------------------------------------- comprobaciones: espacio y mantenimiento

def check_flatpak():
    G = "Espacio y mantenimiento"
    code, out = sh(["flatpak", "remote-ls", "--updates", "--columns=application"], timeout=90)
    if code != 0:
        return [result(G, "Actualizaciones de Flatpak", "info", "No se pudo comprobar (¿sin conexión?).")]
    apps = sorted({l.strip() for l in out.splitlines() if l.strip() and not l.startswith("Application")})
    if not apps:
        return [result(G, "Actualizaciones de Flatpak", "ok", "Todas las apps y runtimes están al día.",
                       metrics=[metric("flatpak_updates", 0)])]
    names = ", ".join(a.split(".")[-1] for a in apps[:8]) + ("…" if len(apps) > 8 else "")
    return [result(G, "Actualizaciones de Flatpak", "info", f"{len(apps)} pendientes: {names}.",
                   {"label": "Actualizar", "confirm": f"Se actualizarán {len(apps)} apps/runtimes de Flatpak. "
                                                      "Puede tardar varios minutos.",
                    "fn": lambda: run_checked(["flatpak", "update", "-y", "--noninteractive"],
                                              "Flatpaks actualizados", timeout=1800)},
                   [metric("flatpak_updates", len(apps))])]


def check_journal():
    G = "Espacio y mantenimiento"
    _, out = sh(["journalctl", "--disk-usage"])
    m = re.search(r"([\d.]+)([BKMGT])\b", out)  # «take up 46.5M in the file system»
    size = float(m.group(1)) * 1024 ** "BKMGT".index(m.group(2)) if m else 0
    text = f"Ocupan {human(size)}." if size else "No se pudo medir."
    return [result(G, "Registros del sistema", "warn" if size > 1024 ** 3 else "ok", text,
                   metrics=[metric("journal_bytes", round(size))] if size else [])]


def check_backups():
    G = "Espacio y mantenimiento"
    if not os.path.isdir(BACKUPS):
        return [result(G, "Copias en ~/Backups", "ok", "No hay copias.", metrics=[metric("backups_bytes", 0)])]
    size = du(BACKUPS)
    entries = [os.path.join(BACKUPS, e) for e in os.listdir(BACKUPS)]
    oldest = min((os.path.getmtime(e) for e in entries), default=time.time())
    days = (time.time() - oldest) / 86400
    text = f"{human(size)} en {len(entries)} carpetas; la más antigua es de hace {int(days)} días."
    status = "info" if size > 1024 ** 3 and days > 30 else "ok"
    if status == "info":
        text += " Si ya no hacen falta, puedes moverlas a la microSD o borrarlas."
    return [result(G, "Copias en ~/Backups", status, text,
                   {"label": "Abrir carpeta", "fn": lambda: open_path(BACKUPS)},
                   [metric("backups_bytes", size), metric("backups_oldest_days", round(days, 1))])]


def empty_trash():
    code, out = sh(["gio", "trash", "--empty"])
    if code != 0:
        raise RuntimeError(out)
    return "Papelera vaciada"


def check_trash():
    G = "Espacio y mantenimiento"
    size = du(os.path.join(TRASH, "files")) if os.path.isdir(os.path.join(TRASH, "files")) else 0
    if size < 1024 ** 2:
        return [result(G, "Papelera", "ok", "Vacía.", metrics=[metric("trash_bytes", size)])]
    return [result(G, "Papelera", "warn" if size > 1024 ** 3 else "info", f"Ocupa {human(size)}.",
                   {"label": "Vaciar", "danger": True, "confirm": f"Se borrarán para siempre {human(size)} "
                                                                  "de la papelera.", "fn": empty_trash},
                   [metric("trash_bytes", size)])]


def check_shaders():
    G = "Espacio y mantenimiento"
    path = os.path.join(HOME, ".local/share/Steam/steamapps/shadercache")
    size = du(path) if os.path.isdir(path) else 0
    text = f"La caché de shaders de Steam ocupa {human(size)}."
    status = "ok"
    if size > 5 * 1024 ** 3:
        status = "info"
        text += " DeckSpace limpia la de los juegos desinstalados."
    return [result(G, "Caché de shaders", status, text,
                   {"label": "Abrir DeckSpace", "fn": lambda: launch_app("DeckSpace")},
                   [metric("shadercache_bytes", size)])]


def lan_ips():
    _, out = sh(["ip", "-4", "-o", "addr", "show", "scope", "global"])
    return [l.split()[3].split("/")[0] for l in out.splitlines() if len(l.split()) > 3]


def enable_api():
    os.makedirs(os.path.dirname(API_UNIT_DST), exist_ok=True)
    with open(API_UNIT_SRC, encoding="utf-8") as f:  # la plantilla apunta a ~/Apps/DeckDoctor
        unit = f.read().replace("%h/Apps/DeckDoctor", APP_DIR)
    with open(API_UNIT_DST, "w", encoding="utf-8") as f:
        f.write(unit)
    sh(["systemctl", "--user", "daemon-reload"])
    return run_checked(["systemctl", "--user", "enable", "--now", API_UNIT], "API activada")


def check_api():
    G = "Integraciones"
    _, active = sh(["systemctl", "--user", "is-active", API_UNIT])
    if active != "active":
        return [result(G, "API para dashboards", "info",
                       "Desactivada. Expone el estado de la Deck por la red local (JSON y Prometheus, solo lectura, "
                       "con token).",
                       {"label": "Activar API", "fn": enable_api,
                        "confirm": "Se abrirá el puerto de la API en tu red local y arrancará con la Deck. Solo "
                                   "permite leer el estado y pide un token, que está en ~/.config/deckdoctor/api.json."},
                       [metric("api_active", 0)])]
    port = 9555
    try:
        import json
        with open(API_CONFIG, encoding="utf-8") as f:
            port = json.load(f).get("port", port)
    except (OSError, ValueError):
        pass
    urls = ", ".join(f"http://{ip}:{port}" for ip in lan_ips()) or f"puerto {port}"
    return [result(G, "API para dashboards", "ok",
                   f"Activa en {urls} (/api/health, /api/metrics, /metrics). Token en ~/.config/deckdoctor/api.json.",
                   {"label": "Desactivar API", "danger": True,
                    "confirm": "Se parará la API y no arrancará con la Deck. Tu dashboard dejará de recibir datos.",
                    "fn": lambda: run_checked(["systemctl", "--user", "disable", "--now", API_UNIT],
                                              "API desactivada")},
                   [metric("api_active", 1)])]


CHECKS = [check_ssh, check_password, check_path, check_keyboard, check_nsl, check_decky,
          check_failed, check_steamos, check_uptime, check_suspend, check_battery, check_temps, check_disks,
          check_flatpak, check_journal, check_backups, check_trash, check_shaders, check_api]


def run_all(progress=lambda m: None, checks=None):
    checks = CHECKS if checks is None else checks
    results = []
    with ThreadPoolExecutor(8) as ex:
        futures = {ex.submit(fn): fn for fn in checks}
        for done, fut in enumerate(futures, 1):
            fn = futures[fut]
            name = fn.__name__.removeprefix("check_")
            try:
                items = fut.result()
            except Exception as e:  # noqa: BLE001
                items = [result("Sistema", fn.__name__, "info", f"Error al comprobar: {e}")]
            for r in items:
                r["id"] = r["id"] or name
                r["check"] = name
            results += items
            progress(f"Comprobando… {done}/{len(checks)}")
    return sort_results(results)


def sort_results(results):
    order = {g: i for i, g in enumerate(GROUPS)}
    return sorted(results, key=lambda r: (order.get(r["group"], 99), STATUS[r["status"]][0]))


# ---------------------------------------------------------------- interfaz

def setup_style(root):
    root.configure(background=BG)
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
        tkfont.nametofont(name).configure(size=11)
    base = tkfont.nametofont("TkDefaultFont").actual("family")

    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=BG, foreground=FG, bordercolor=BORDER, lightcolor=BG, darkcolor=BG,
                troughcolor=SURFACE2, focuscolor=ACCENT, fieldbackground=SURFACE2, insertcolor=FG)
    s.configure("TFrame", background=BG)
    s.configure("Card.TFrame", background=SURFACE)
    s.configure("TLabel", background=BG, foreground=FG)
    s.configure("Muted.TLabel", foreground=MUTED)
    s.configure("Card.TLabel", background=SURFACE)
    s.configure("CardMuted.TLabel", background=SURFACE, foreground=MUTED)
    s.configure("CardBold.TLabel", background=SURFACE, font=(base, 12, "bold"))
    s.configure("Title.TLabel", font=(base, 20, "bold"))
    s.configure("H2.TLabel", background=SURFACE, font=(base, 13, "bold"))

    s.configure("TButton", background=SURFACE2, foreground=FG, borderwidth=0, padding=(14, 8), relief="flat",
                width=-4)
    s.map("TButton", background=[("disabled", SURFACE), ("pressed", BORDER), ("active", BORDER)],
          foreground=[("disabled", MUTED)])
    s.configure("Accent.TButton", background=ACCENT, foreground="#ffffff")
    s.map("Accent.TButton", background=[("disabled", ACCENT_DARK), ("pressed", "#1580d0"), ("active", "#3aaeff")])
    s.configure("Danger.TButton", background="#c94848", foreground="#ffffff")
    s.map("Danger.TButton", background=[("disabled", "#5a2c2c"), ("pressed", "#a83a3a"), ("active", "#de5a5a")],
          foreground=[("disabled", "#c9a3a3")])
    s.configure("Vertical.TScrollbar", background=SURFACE2, troughcolor=BG, arrowcolor=MUTED,
                borderwidth=0, relief="flat")
    s.configure("Horizontal.TProgressbar", troughcolor=SURFACE2, background=ACCENT, borderwidth=0,
                lightcolor=ACCENT, darkcolor=ACCENT)
    return base


class App:
    def __init__(self, root):
        self.root = root
        self.mtime = os.path.getmtime(__file__)
        self.busy = False
        self.results = []
        self.events = queue.Queue()
        self.font = setup_style(root)
        root.title("DeckDoctor")
        root.geometry("1100x760")
        root.minsize(760, 520)
        try:
            root.attributes("-zoomed", True)
        except tk.TclError:
            pass

        outer = ttk.Frame(root, padding=(18, 14, 18, 12))
        outer.pack(fill="both", expand=True)

        head = ttk.Frame(outer)
        head.pack(fill="x")
        ttk.Label(head, text="DeckDoctor", style="Title.TLabel").pack(side="left")
        self.subtitle = ttk.Label(head, text="  Salud de tu Steam Deck", style="Muted.TLabel")
        self.subtitle.pack(side="left", pady=(8, 0))
        self.check_btn = ttk.Button(head, text="⟳  Comprobar", style="Accent.TButton", command=self.check)
        self.check_btn.pack(side="right")
        self.progress = ttk.Progressbar(head, mode="indeterminate", length=140)
        self.status = ttk.Label(head, text="", style="Muted.TLabel")
        self.status.pack(side="right", padx=12)

        self.summary = tk.Frame(outer, background=BG)
        self.summary.pack(fill="x", pady=(12, 8))

        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(body, background=BG, highlightthickness=0, borderwidth=0)
        sb = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas)
        self.inner_id = self.canvas.create_window(0, 0, anchor="nw", window=self.inner)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self.on_resize)
        root.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-3, "units"))
        root.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(3, "units"))
        self.wrap = 600

        self.logline = ttk.Label(outer, text="", style="Muted.TLabel")
        self.logline.pack(fill="x", pady=(8, 0))

        root.after(100, self.poll)
        root.after(200, self.check)

    def on_resize(self, e):
        self.canvas.itemconfigure(self.inner_id, width=e.width)
        wrap = max(300, e.width - 420)
        if abs(wrap - self.wrap) > 40:
            self.wrap = wrap
            for lbl in getattr(self, "details", []):
                lbl.configure(wraplength=wrap)

    # ------------------------------------------------------------ pintar resultados
    def render(self):
        for w in self.summary.winfo_children():
            w.destroy()
        counts = {k: sum(r["status"] == k for r in self.results) for k in STATUS}
        f = self.font
        if counts["bad"]:
            big, color = f"{counts['bad']} problema{'s' if counts['bad'] > 1 else ''} por arreglar", DANGER
        elif counts["warn"]:
            big, color = f"{counts['warn']} aviso{'s' if counts['warn'] > 1 else ''} que revisar", WARN
        else:
            big, color = "Todo en orden", OK
        tk.Label(self.summary, text=big, fg=color, bg=BG, font=(f, 18, "bold")).pack(side="left")
        plural = {"bad": ("problema", "problemas"), "warn": ("aviso", "avisos"), "info": ("info", "info"),
                  "ok": ("correcto", "correctos")}
        for k in ("bad", "warn", "info", "ok"):
            col = STATUS[k][1]
            word = plural[k][counts[k] != 1]
            tk.Label(self.summary, text=f"   ● {counts[k]} {word}", fg=col, bg=BG,
                     font=(f, 11)).pack(side="left", pady=(6, 0))

        for w in self.inner.winfo_children():
            w.destroy()
        self.details = []
        for group in GROUPS:
            items = [r for r in self.results if r["group"] == group]
            if not items:
                continue
            card = ttk.Frame(self.inner, style="Card.TFrame", padding=(20, 14))
            card.pack(fill="x", pady=(0, 12), padx=(0, 6))
            ttk.Label(card, text=group, style="H2.TLabel").pack(anchor="w", pady=(0, 6))
            for r in items:
                self.render_row(card, r)

    def render_row(self, card, r):
        _, color, label = STATUS[r["status"]]
        row = ttk.Frame(card, style="Card.TFrame")
        row.pack(fill="x", pady=6)
        tk.Label(row, text="●", fg=color, bg=SURFACE, font=(self.font, 16)).pack(side="left", anchor="n",
                                                                               padx=(0, 12))
        if r["fix"]:
            fx = r["fix"]
            style = "Danger.TButton" if fx.get("danger") else \
                "Accent.TButton" if r["status"] in ("bad", "warn") else "TButton"
            ttk.Button(row, text=fx["label"], style=style,
                       command=lambda fx=fx, r=r: self.apply_fix(r, fx)).pack(side="right", anchor="center",
                                                                              padx=(12, 0))
        text = ttk.Frame(row, style="Card.TFrame")
        text.pack(side="left", fill="x", expand=True)
        top = ttk.Frame(text, style="Card.TFrame")
        top.pack(anchor="w")
        ttk.Label(top, text=r["title"], style="CardBold.TLabel").pack(side="left")
        tk.Label(top, text=f"  {label}", fg=color, bg=SURFACE, font=(self.font, 10, "bold")).pack(side="left")
        d = ttk.Label(text, text=r["detail"], style="CardMuted.TLabel", wraplength=self.wrap, justify="left")
        d.pack(anchor="w", pady=(2, 0))
        self.details.append(d)

    # ------------------------------------------------------------ acciones
    def check(self):
        if os.path.getmtime(__file__) != self.mtime:  # el código cambió con la app abierta: recargar
            self.root.destroy()
            os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])

        def done(results):
            self.results = results
            self.subtitle.configure(text=f"  Última comprobación: {dt.datetime.now():%H:%M}")
            self.render()
        self.run("Comprobando…", run_all, done)

    def apply_fix(self, r, fx):
        if fx.get("confirm") and not self.dialog(r["title"], fx["confirm"], ok=fx["label"],
                                                 danger=fx.get("danger", False)):
            return

        def done(msg):
            self.logline.configure(text=f"✔ {msg}")
            self.root.after(300, self.check)
        self.run(f"{fx['label']}…", lambda progress: fx["fn"](), done)

    # ------------------------------------------------------------ diálogo propio (tema oscuro)
    def dialog(self, title, text, ok="Aceptar", cancel="Cancelar", danger=False):
        win = tk.Toplevel(self.root, background=SURFACE)
        win.title(title)
        win.transient(self.root)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=24, style="Card.TFrame")
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=title, style="H2.TLabel").pack(anchor="w")
        ttk.Label(frm, text=text, style="Card.TLabel", wraplength=560, justify="left").pack(anchor="w", pady=(12, 20))
        result_ = {"ok": False}
        btns = ttk.Frame(frm, style="Card.TFrame")
        btns.pack(fill="x")

        def close(value):
            result_["ok"] = value
            win.destroy()
        okb = ttk.Button(btns, text=ok, style="Danger.TButton" if danger else "Accent.TButton",
                         command=lambda: close(True))
        okb.pack(side="right")
        if cancel:
            ttk.Button(btns, text=cancel, command=lambda: close(False)).pack(side="right", padx=8)
        win.bind("<Escape>", lambda e: close(False))
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_height()) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        win.grab_set()
        okb.focus_set()
        self.root.wait_window(win)
        return result_["ok"]

    # ------------------------------------------------------------ trabajo en segundo plano
    def run(self, text, fn, then):
        if self.busy:
            return
        self.busy = True
        self.check_btn.configure(state="disabled")
        self.progress.pack(side="right", padx=(0, 4))
        self.progress.start(12)
        self.status.configure(text=text)

        def worker():
            try:
                self.events.put(("done", then, fn(lambda m: self.events.put(("msg", m)))))
            except Exception as e:  # noqa: BLE001
                self.events.put(("error", str(e)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == "msg":
                    self.status.configure(text=ev[1])
                    continue
                self.busy = False
                self.check_btn.configure(state="normal")
                self.progress.stop()
                self.progress.pack_forget()
                self.status.configure(text="")
                if ev[0] == "done":
                    ev[1](ev[2])
                else:
                    self.logline.configure(text=f"✖ {ev[1].splitlines()[0]}")
                    self.dialog("Error", ev[1], ok="Cerrar", cancel=None)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)


def main():
    if "--check" in sys.argv:  # informe en la terminal, sin ventana
        results = run_all()
        for r in results:
            mark = {"ok": "✔", "info": "i", "warn": "!", "bad": "✖"}[r["status"]]
            fix = f"  [{r['fix']['label']}]" if r["fix"] else ""
            print(f"{mark} {r['group']} · {r['title']}: {r['detail']}{fix}")
        sys.exit(1 if any(r["status"] == "bad" for r in results) else 0)
    root = tk.Tk(className="DeckDoctor")
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
