#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Dnfshelf: a Synaptic-style front end for Fedora (GTK4 + libadwaita).

Dependencies:
    sudo dnf install python3-gobject gtk4 libadwaita dnf5 dnf5-plugins polkit

Run:
    python3 dnfshelf.py

Install to ~/.local/bin and the app menu (or use the menu entry inside the app):
    python3 dnfshelf.py --install
    python3 dnfshelf.py --uninstall

Everything goes through the dnf5 CLI. Privileged operations use pkexec, so you
get one polkit prompt per transaction. Pins use `dnf5 versionlock`
(state in /etc/dnf/versionlock.toml).
"""
import gzip
import hashlib
import ipaddress
import json
import operator
import os
import pwd
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk, Pango  # noqa: E402


APP_ID = "org.outusuke.Dnfshelf"
VERSIONLOCK_FILE = "/etc/dnf/versionlock.toml"
AUTO_CONF = "/etc/dnf/automatic.conf"
AUTO_DEFAULTS_CONF = "/usr/share/dnf5/dnf5-plugins/automatic.conf"
AUTO_TIMER = "dnf5-automatic.timer"
AUTO_PKG = "dnf5-plugin-automatic"
AUTO_DEFAULTS = {"apply_updates": "no", "download_updates": "yes",
                 "upgrade_type": "default", "reboot": "never"}
AUTO_MODES = [{"apply_updates": "no", "download_updates": "no"},
              {"apply_updates": "no", "download_updates": "yes"},
              {"apply_updates": "yes", "download_updates": "yes"}]
AUTO_MODE_LABELS = ["Check only", "Download only", "Download and install"]
AUTO_MODE_HINTS = ["Only reports what is available",
                   "Downloads updates, you install them from here",
                   "Installs updates without asking"]
AUTO_SCOPES = {"default": "All updates", "security": "Security updates only"}
AUTO_REBOOTS = {"never": "Never", "when-needed": "When needed", "when-changed": "After any update"}
REPORT_FILE = "/var/lib/dnfshelf/report.txt"
UNIT_DIR = os.path.join(GLib.get_user_config_dir(), "systemd", "user")
NOTIFY_PATH = "dnfshelf-notify.path"
NOTIFY_SERVICE = "dnfshelf-notify.service"
NOTIFY_PATH_UNIT = """[Unit]
Description=Notify when dnf5 automatic has run (Dnfshelf)

[Path]
PathChanged={report}

[Install]
WantedBy=default.target
"""
NOTIFY_SERVICE_UNIT = """[Unit]
Description=Show the dnf5 automatic report (Dnfshelf)

[Service]
Type=oneshot
ExecStart={exe} --notify
"""
GS_NOTE = "Separate from the timer above. It also covers Flatpaks, which dnf5 automatic does not."
GS_BOTH = "Both are on and will update the same packages. Turn one off."
QF = r"%{name}|%{evr}|%{arch}|%{repoid}|%{summary}\n"
# List packages from the local metadata cache (no network round trip on every start).
# Falls back to a normal dnf5 run automatically if that fails. Use "Refresh repo
# metadata" in the menu to update the cache.
USE_CACHEONLY = True
CACHE_DIR = os.path.join(GLib.get_user_cache_dir(), "dnfshelf")
CACHE_FILE = os.path.join(CACHE_DIR, "packages.json")
SHOT_TAB = 4
SHOT_HEIGHT = 160
SEARCH_MODES = ("Name & description", "Name only", "Word start", "Exact name")
ICON_PX = 28
INFO_RULE = "\u2500" * 40
PKG_QF = r"%{name}\n"
SO_CAP = re.compile(r"^([^()\s]+\.so[^()\s]*)\(.*\)$")
COMPS_RE = re.compile(r"^(\S+)\s+(.+?)\s+(yes|no)\s*$")
WIDE_ENV = {**os.environ, "COLUMNS": "1000"}  # keep dnf5 tables from truncating names
MODES = ["All", "Installed", "Not installed", "Upgradable", "Queued changes",
         "Security updates", "User-installed", "Unneeded", "Leaves", "Extras"]
M_UPGRADABLE, M_QUEUED, M_SECURITY, M_USER, M_UNNEEDED, M_LEAVES, M_EXTRAS = 3, 4, 5, 6, 7, 8, 9
# Modes filled by one repoquery each, lazily: mode -> (flags, allow cache-only).
# Advisory data may be missing from the offline cache, so security may go online.
# No --installed: --userinstalled rejects it, and these modes only show installed packages.
SET_QUERIES = {
    M_SECURITY: (("--upgrades", "--security", "--latest-limit=1"), False),
    M_USER: (("--userinstalled",), True),
    M_UNNEEDED: (("--unneeded",), True),
    M_LEAVES: (("--leaves",), True),
    M_EXTRAS: (("--extras",), True),
}


# ---------------------------------------------------------------- data layer
def run(argv, env=None):
    return subprocess.run(argv, capture_output=True, text=True, env=env).stdout


def dnf5(*args, cache=True, env=None):
    """Read-only dnf5 call, preferring the local metadata cache."""
    if USE_CACHEONLY and cache:
        r = subprocess.run(["dnf5", "--cacheonly", *args], capture_output=True, text=True,
                           env=env)
        if r.returncode == 0:
            return r.stdout
    return run(["dnf5", *args], env=env)


def query(*flags, cache=True):
    out = dnf5("repoquery", *flags, "--queryformat", QF, cache=cache)
    rows = []
    for line in out.splitlines():
        parts = line.split("|", 4)
        if len(parts) == 5:
            rows.append(parts)
    return rows


def reboot_status():
    """(required, packages) from `dnf5 needs-restarting`, or None if unavailable."""
    try:
        r = subprocess.run(["dnf5", "needs-restarting", "--json"], capture_output=True, text=True)
        for o in json.loads(r.stdout):
            if o.get("type") == "reboot":
                return bool(o.get("reboot_required")), list(o.get("packages") or [])
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    try:  # older dnf5 without --json: exit code 1 means a reboot is recommended
        r = subprocess.run(["dnf5", "needs-restarting"], capture_output=True, text=True)
    except OSError:
        return None
    return (r.returncode == 1, []) if r.returncode in (0, 1) else None


def parse_comps(text):
    """Parse `dnf5 group|environment list` into [(id, name, installed)] sorted by name."""
    out = []
    for line in text.splitlines():
        m = COMPS_RE.match(line)
        if m:
            out.append((m.group(1), m.group(2), m.group(3) == "yes"))
    return sorted(out, key=lambda t: (t[1].lower(), t[0]))


def comps_text(kind, sub, *specs, hidden=False):
    """Output of `dnf5 <group|environment> <list|info> [--hidden] [specs]`."""
    flags = ["--hidden"] if hidden and kind == "group" else []
    return dnf5(kind, sub, *flags, *specs, env=WIDE_ENV)


BIN_PATH = os.path.expanduser("~/.local/bin/dnfshelf")
DESKTOP_PATH = os.path.expanduser(f"~/.local/share/applications/{APP_ID}.desktop")
SERVICE_PATH = os.path.expanduser(f"~/.local/share/dbus-1/services/{APP_ID}.service")
DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=Dnfshelf
Comment=Synaptic-style package manager for Fedora
Exec="{exec}"
Icon=system-software-install
Terminal=false
DBusActivatable=true
Categories=System;PackageManager;GTK;
Keywords=dnf;rpm;packages;software;
StartupWMClass={app_id}
"""
SERVICE_ENTRY = """[D-BUS Service]
Name={app_id}
Exec={exec} --gapplication-service
"""


def install_launcher():
    """Copy this script to ~/.local/bin and add an app-menu entry."""
    src = os.path.abspath(__file__)
    os.makedirs(os.path.dirname(BIN_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(DESKTOP_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(SERVICE_PATH), exist_ok=True)
    if not (os.path.exists(BIN_PATH) and os.path.samefile(src, BIN_PATH)):
        shutil.copyfile(src, BIN_PATH)
    os.chmod(BIN_PATH, 0o755)
    with open(DESKTOP_PATH, "w") as f:
        f.write(DESKTOP_ENTRY.format(exec=BIN_PATH, app_id=APP_ID))
    with open(SERVICE_PATH, "w") as f:
        f.write(SERVICE_ENTRY.format(exec=BIN_PATH, app_id=APP_ID))
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", os.path.dirname(DESKTOP_PATH)],
                       capture_output=True)
    return f"Installed {BIN_PATH} and added to the app menu and D-Bus services"


def uninstall_launcher():
    removed = []
    for path in (BIN_PATH, DESKTOP_PATH, SERVICE_PATH):
        if os.path.exists(path):
            os.remove(path)
            removed.append(path)
    return "Removed " + ", ".join(removed) if removed else "Nothing to remove"


def load_pins():
    try:
        with open(VERSIONLOCK_FILE, "rb") as f:
            data = tomllib.load(f)
        return {p["name"] for p in data.get("packages", []) if "name" in p}
    except Exception:
        return set()


def load_pin_entries():
    """[(name, readable conditions)] from the dnf5 versionlock file."""
    try:
        with open(VERSIONLOCK_FILE, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return []
    out = []
    for p in data.get("packages", []):
        if "name" not in p:
            continue
        conds = ", ".join(
            f'{c.get("key", "?")} {c.get("comparator", "?")} {c.get("value", "?")}'
            for c in p.get("conditions", []))
        out.append((p["name"], conds or "locked"))
    return sorted(out)


def ini_values(text, section):
    out, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"\s*\[([^\]]+)\]", line)
        if m:
            cur = m.group(1).strip()
            continue
        m = re.match(r"\s*([\w.-]+)\s*=\s*(.*?)\s*$", line)
        if cur == section and m:
            out[m.group(1)] = m.group(2)
    return out


def set_ini_keys(text, section, values):
    """Set keys in one INI section and leave every other line as it is."""
    out, cur, seen, last, found = [], None, set(), 0, False
    for line in text.splitlines():
        m = re.match(r"\s*\[([^\]]+)\]", line)
        if m:
            cur = m.group(1).strip()
            found = found or cur == section
        k = re.match(r"\s*([\w.-]+)\s*=", line)
        if cur == section and k and k.group(1) in values:
            line = f"{k.group(1)} = {values[k.group(1)]}"
            seen.add(k.group(1))
        out.append(line)
        if cur == section and line.strip() and not line.lstrip().startswith(("#", ";")):
            last = len(out)
    missing = [f"{k} = {v}" for k, v in values.items() if k not in seen]
    if found:
        out[last:last] = missing
    elif missing:
        out += ([""] if out else []) + [f"[{section}]"] + missing
    return "\n".join(out) + "\n"


def automatic_settings():
    vals = dict(AUTO_DEFAULTS)
    for path in (AUTO_DEFAULTS_CONF, AUTO_CONF):
        try:
            with open(path) as f:
                vals.update(ini_values(f.read(), "commands"))
        except OSError:
            pass
    on = lambda k: vals[k].lower() in ("yes", "true", "1", "on", "enabled")
    mode = 2 if on("apply_updates") else 1 if on("download_updates") else 0
    return {"mode": mode, "scope": vals["upgrade_type"], "reboot": vals["reboot"]}


def timer_state():
    """(unit exists, enabled, next run) for dnf5-automatic.timer."""
    try:
        have = subprocess.run(["systemctl", "cat", AUTO_TIMER], capture_output=True).returncode == 0
        enabled = run(["systemctl", "is-enabled", AUTO_TIMER]).strip() == "enabled"
        nxt = run(["systemctl", "show", AUTO_TIMER, "-p", "NextElapseUSecRealtime", "--value"]).strip()
    except OSError:
        return False, False, ""
    return have, enabled, nxt


def emit_settings():
    """(section, emit_via list) as dnf5 automatic will read them."""
    sec, via = "emitters", ["stdio"]
    for path in (AUTO_DEFAULTS_CONF, AUTO_CONF):
        try:
            with open(path) as f:
                text = f.read()
        except OSError:
            continue
        for name in re.findall(r"^\s*\[([^\]]+)\]", text, re.M):
            vals = ini_values(text, name.strip())
            if "emit_via" in vals:
                sec, via = name.strip(), [v.strip() for v in vals["emit_via"].split(",") if v.strip()]
    return sec, via


GAMEMODE = "com.feralinteractive.GameMode"


def gamemode_active():
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(GAMEMODE, "/com/feralinteractive/GameMode",
                              "org.freedesktop.DBus.Properties", "Get",
                              GLib.Variant("(ss)", (GAMEMODE, "ClientCount")), None,
                              Gio.DBusCallFlags.NO_AUTO_START, 2000, None)
    except GLib.Error:
        return False
    return reply.unpack()[0] > 0


def update_blocker():
    """Why automatic updates should wait, or "". Same checks as GNOME Software, plus GameMode."""
    if Gio.NetworkMonitor.get_default().get_network_metered():
        return "the connection is metered"
    monitor = Gio.PowerProfileMonitor.dup_default()
    if monitor and monitor.get_power_saver_enabled():
        return "power saver is on"
    if gamemode_active():
        return "GameMode is active"
    return ""


GUARD_FILE = "/etc/systemd/system/dnf5-automatic.service.d/dnfshelf.conf"


def guard_unit():
    """Drop-in that skips the timer's run while update_blocker() says to wait."""
    uid = os.getuid()
    check = shlex.join(["runuser", "-u", pwd.getpwuid(uid).pw_name, "--", "env",
                        f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus",
                        sys.executable, os.path.abspath(__file__), "--can-update"])
    retry = ("systemd-run --quiet --collect --on-active=30min --unit=dnfshelf-retry /bin/sh -c "
             f'"systemctl is-enabled --quiet {AUTO_TIMER} && systemctl --no-block start '
             'dnf5-automatic.service"')
    # only exit status 3 means wait, so a broken check can't stop updates for good
    return (f"[Service]\nExecCondition=/bin/sh -c '{check}; r=$$?; "
            f"[ $$r -ne 3 ] || {{ {retry}; exit 1; }}'\n")


def notify_enabled():
    try:
        r = subprocess.run(["systemctl", "--user", "is-enabled", NOTIFY_PATH],
                           capture_output=True, text=True)
    except OSError:
        return False
    return r.stdout.strip() == "enabled"


def set_notify(on):
    """Write and enable (or disable) the user path unit; returns an error message or ""."""
    try:
        if on:
            if not os.path.exists(BIN_PATH):
                install_launcher()  # the unit runs the installed copy of this script
            os.makedirs(UNIT_DIR, exist_ok=True)
            for name, text in ((NOTIFY_SERVICE, NOTIFY_SERVICE_UNIT.format(exe=BIN_PATH)),
                               (NOTIFY_PATH, NOTIFY_PATH_UNIT.format(report=REPORT_FILE))):
                with open(os.path.join(UNIT_DIR, name), "w") as f:
                    f.write(text)
        steps = [["daemon-reload"], ["enable", "--now", NOTIFY_PATH]] if on \
            else [["disable", "--now", NOTIFY_PATH]]
        for step in steps:
            r = subprocess.run(["systemctl", "--user", *step], capture_output=True, text=True)
            if r.returncode:
                return (r.stderr or r.stdout).strip() or f"systemctl exited with {r.returncode}"
    except OSError as e:
        return str(e)
    return ""


def notify(notes):
    """notes: [(id, title, body, action)], all sent from one registered app."""
    app = Gio.Application(application_id=APP_ID)
    app.register(None)
    for nid, title, body, action in notes:
        note = Gio.Notification.new(title)
        note.set_body(body)
        note.set_default_action(action)
        app.send_notification(nid, note)
    conn = app.get_dbus_connection()
    if conn:
        conn.flush_sync(None)  # we exit right after, so push the message out first


def refresh_packagekit():
    """GNOME Software keeps offering updates dnf already installed until PackageKit refreshes."""
    try:
        subprocess.run(["pkcon", "refresh"], capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        pass


REPORT_SECTION = re.compile(r"^(Installing|Upgrading|Downgrading|Reinstalling|Removing)\b[^:]*:\s*$")
REPORT_ROW = re.compile(r"^\s+(\S+)\s+\S+\s+\d+:\S+\s")
DONE_VERBS = {"Installing": "Installed", "Upgrading": "Upgraded", "Downgrading": "Downgraded",
              "Reinstalling": "Reinstalled", "Removing": "Removed"}


def report_packages(lines):
    """{verb: [package names]} from the transaction table in the dnf5 report."""
    out, cur = {}, None
    for ln in lines:
        m = REPORT_SECTION.match(ln)
        if m:
            cur = out.setdefault(m.group(1), [])
        elif cur is not None and not ln.lstrip().startswith("replacing"):
            r = REPORT_ROW.match(ln)
            if r:
                cur.append(r.group(1))
    return {v: n for v, n in out.items() if n}


def report_summary(lines, mode):
    """Package names instead of the raw report, or "" if the table was not found."""
    groups = report_packages(lines)
    if not groups:
        return ""
    show = lambda names: ", ".join(names[:8]) + (f" +{len(names) - 8} more" if len(names) > 8 else "")
    if mode == 2:
        return "\n".join(f"{DONE_VERBS[v]}: {show(n)}" for v, n in groups.items())
    names = [x for n in groups.values() for x in n]
    return f"{'Downloaded' if mode == 1 else 'Available'} ({len(names)}): {show(names)}"


def notify_report():
    try:
        with open(REPORT_FILE) as f:
            lines = [ln.rstrip() for ln in f if ln.strip()]
    except OSError:
        return 1
    if not lines:
        return 0
    mode = automatic_settings()["mode"]
    installed = mode == 2
    body = report_summary(lines, mode)
    if not body:
        if len(lines) > 12:
            lines[12:] = [f"\u2026 and {len(lines) - 12} more lines"]
        body = "\n".join(lines)
    notes = [("auto-update", "Automatic updates", body, "app.history" if installed else "app.open")]
    status = reboot_status()
    if status and status[0]:
        names = ", ".join(status[1][:3]) + (" and more" if len(status[1]) > 3 else "")
        notes.append(("reboot", "Restart recommended", names or "Restart to finish the updates",
                      "app.open"))
    notify(notes)
    if installed:
        refresh_packagekit()
    return 0


def gnome_software_settings():
    """GNOME Software's settings, or None when it isn't installed (a missing schema would abort)."""
    src = Gio.SettingsSchemaSource.get_default()
    schema = src and src.lookup("org.gnome.software", True)
    if schema and schema.has_key("download-updates"):
        return Gio.Settings.new_full(schema, None, None)
    return None


GS_SERVICE = "gnome-software.service"


def gs_paused():
    try:
        r = subprocess.run(["systemctl", "--user", "is-enabled", GS_SERVICE],
                           capture_output=True, text=True)
    except OSError:
        return False
    return r.stdout.strip() == "masked"


def set_gs_paused(on):
    """Mask or unmask GNOME Software's background service; returns an error message or ""."""
    verb = ["mask", "--now"] if on else ["unmask"]
    try:
        r = subprocess.run(["systemctl", "--user", *verb, GS_SERVICE],
                           capture_output=True, text=True)
    except OSError as e:
        return str(e)
    return "" if r.returncode == 0 else (r.stderr or r.stdout).strip() or f"systemctl exited with {r.returncode}"


APPSTREAM_DIRS = ("/usr/share/swcatalog", "/var/cache/swcatalog")
APPSTREAM_CACHE = os.path.join(CACHE_DIR, "appstream.json")
APPSTREAM_FORMAT = 4  # bump when the parsed fields change
# Pre-0.16 catalogs name desktop apps just "desktop".
APP_TYPES = ("desktop-application", "console-application", "desktop")
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
SHOT_DIR = os.path.join(CACHE_DIR, "screenshots")
SHOT_MAX_BYTES = 5 * 1024 * 1024
SHOT_MAX_PIXELS = 16_000_000
SHOT_MAX_WIDTH = 640
SHOT_TIMEOUT = 10
SHOT_DEADLINE = 20
SHOT_KEEP = 200
SHOT_LIMIT = 8
_apps, _apps_lock = None, threading.Lock()
_shot_failed = set()
_textures = {}


def ui_languages():
    raw = (os.environ.get("LANGUAGE") or os.environ.get("LC_ALL")
           or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or "")
    langs = []
    for part in raw.split(":"):
        part = part.split(".")[0].split("@")[0]
        if part and part not in ("C", "POSIX"):
            langs += [part, part.split("_")[0]]
    return list(dict.fromkeys(langs))


def appstream_files():
    files = []
    for base in APPSTREAM_DIRS:
        folder = os.path.join(base, "xml")
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        files += [os.path.join(folder, n) for n in names if n.endswith((".xml", ".xml.gz"))]
    return files


def _localized(comp, tag, langs):
    found = {e.get(XML_LANG): e for e in comp.findall(tag)}
    for lang in (*langs, None):
        if lang in found:
            return found[lang]
    return None


def _flat(elem):
    return " ".join("".join(elem.itertext()).split())


def _description(elem):
    blocks = []
    for child in elem:
        if child.tag in ("ul", "ol"):
            blocks.append("\n".join("  \u2022 " + _flat(li) for li in child))
        elif _flat(child):
            blocks.append(_flat(child))
    return "\n\n".join(blocks)


def _safe_name(s):
    return bool(s) and os.path.basename(s) == s and s not in (".", "..")


def _screenshots(comp, langs):
    shots = comp.findall("screenshots/screenshot")
    shots.sort(key=lambda s: s.get("type") != "default")
    out = []
    for shot in shots:
        for img in shot.findall("image"):
            url = (img.text or "").strip()
            if img.get("type", "source") == "source" and url.startswith("https://") and len(url) <= 2048:
                cap = _localized(shot, "caption", langs)
                out.append([_flat(cap) if cap is not None else "", url])
                break
    return out[:SHOT_LIMIT]


def parse_appstream(stream, langs):
    """{package name: metadata} from one AppStream catalog file."""
    out, origin = {}, ""
    for event, el in ET.iterparse(stream, events=("start", "end")):
        if event == "start" and el.tag == "components":
            origin = el.get("origin", "")
        elif event == "end" and el.tag == "component":
            if el.get("type") in APP_TYPES:
                text = lambda tag: _flat(e) if (e := _localized(el, tag, langs)) is not None else ""
                desc = _localized(el, "description", langs)
                icon = next((i.text for i in el.findall("icon")
                             if i.get("type") == "cached" and i.text), "")
                meta = {"name": text("name"), "summary": text("summary"),
                        "description": _description(desc) if desc is not None else "",
                        "license": el.findtext("project_license") or "",
                        "homepage": next((u.text or "" for u in el.findall("url")
                                          if u.get("type") == "homepage"), ""),
                        "categories": [c.text for c in el.findall("categories/category") if c.text],
                        "icon": [origin, icon] if _safe_name(origin) and _safe_name(icon) else [],
                        "screenshots": _screenshots(el, langs)}
                for pkg in el.findall("pkgname"):
                    if pkg.text:
                        out.setdefault(pkg.text, meta)
            el.clear()
    return out


def build_appstream(langs):
    apps = {}
    for path in appstream_files():
        opener = gzip.open if path.endswith(".gz") else open
        try:
            with opener(path, "rb") as f:
                for pkg, meta in parse_appstream(f, langs).items():
                    apps.setdefault(pkg, meta)
        except (OSError, EOFError, ET.ParseError):
            continue
    return apps


def load_appstream(langs):
    """Catalog index, rebuilt only when a catalog file or the language changed."""
    sig = [APPSTREAM_FORMAT, langs]
    for path in appstream_files():
        try:
            st = os.stat(path)
        except OSError:
            continue
        sig.append([path, st.st_mtime_ns, st.st_size])
    try:
        with open(APPSTREAM_CACHE) as f:
            data = json.load(f)
        if data.get("sig") == sig:
            return data["apps"]
    except (OSError, ValueError, AttributeError, KeyError):
        pass
    apps = build_appstream(langs)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = APPSTREAM_CACHE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"sig": sig, "apps": apps}, f, separators=(",", ":"))
        os.replace(tmp, APPSTREAM_CACHE)
    except OSError:
        pass
    return apps


def appstream_apps():
    global _apps
    with _apps_lock:
        if _apps is None:
            _apps = load_appstream(ui_languages())
        return _apps


def appstream_text(name):
    meta = appstream_apps().get(name)
    if not meta:
        return ""
    head = meta["name"] + (f" \u2014 {meta['summary']}" if meta["summary"] else "")
    facts = "\n".join(f"{k}: {v}" for k, v in (
        ("Homepage", meta["homepage"]), ("License", meta["license"]),
        ("Categories", ", ".join(meta["categories"]))) if v)
    return "\n\n".join(x for x in (head, meta["description"], facts) if x)


def appstream_icon(name, sizes=("128x128", "64x64")):
    """Icon file for a package, only if the catalog is already loaded (never blocks the UI)."""
    meta = (_apps or {}).get(name)
    if not meta or not meta["icon"]:
        return ""
    origin, filename = meta["icon"]
    for base in APPSTREAM_DIRS:
        for size in sizes:
            path = os.path.join(base, "icons", origin, size, filename)
            if os.path.isfile(path):
                return path
    return ""


def appstream_screenshots(name):
    meta = (_apps or {}).get(name)
    return meta["screenshots"] if meta else []


def texture_from(path):
    if path not in _textures:
        if len(_textures) > 600:
            _textures.clear()
        try:
            _textures[path] = Gdk.Texture.new_from_filename(path)
        except GLib.Error:
            _textures[path] = None
    return _textures[path]


def screenshot_path(url):
    return os.path.join(SHOT_DIR, hashlib.sha256(url.encode()).hexdigest() + ".png")


def _public_host(host, port):
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addrs = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]
    except (OSError, ValueError):
        return False
    return bool(addrs) and all((getattr(a, "ipv4_mapped", None) or a).is_global for a in addrs)


def screenshot_url_ok(url):
    """Catalog URLs are untrusted: public https hosts only."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port or 443
    except ValueError:
        return False
    return (parts.scheme == "https" and len(url) <= 2048 and bool(parts.hostname)
            and not (parts.username or parts.password) and _public_host(parts.hostname, port))


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not screenshot_url_ok(urllib.parse.urljoin(req.full_url, newurl)):
            raise urllib.error.URLError("redirect refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "Dnfshelf", "Accept": "image/png, image/jpeg"})
    end = time.monotonic() + SHOT_DEADLINE
    size = 0
    with urllib.request.build_opener(_HttpsOnly).open(req, timeout=SHOT_TIMEOUT) as resp, \
            open(dest, "wb") as out:
        if int(resp.headers.get("Content-Length") or 0) > SHOT_MAX_BYTES:
            return False
        while chunk := resp.read(65536):
            size += len(chunk)
            if size > SHOT_MAX_BYTES or time.monotonic() > end:
                return False
            out.write(chunk)
    return size > 0


def _normalize_image(src, dest):
    # Header first: a tiny file can declare a huge image.
    fmt, width, height = GdkPixbuf.Pixbuf.get_file_info(src)
    if fmt is None or fmt.get_name() not in ("png", "jpeg") or width < 1 or height < 1 \
            or width * height > SHOT_MAX_PIXELS:
        return False
    pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(src, min(width, SHOT_MAX_WIDTH), -1, True)
    return pix.savev(dest, "png", [], [])


def _remove(*paths):
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass


def prune_screenshots():
    try:
        files = sorted((os.path.getmtime(p), p) for p in
                       (os.path.join(SHOT_DIR, n) for n in os.listdir(SHOT_DIR) if n.endswith(".png")))
    except OSError:
        return
    _remove(*(p for _, p in files[:-SHOT_KEEP]))


def fetch_screenshot(url):
    """Cached PNG path for a screenshot URL, or "" on any failure."""
    path = screenshot_path(url)
    if os.path.isfile(path):
        return path
    if url in _shot_failed or not screenshot_url_ok(url):
        return ""
    raw = tmp = ""
    try:
        os.makedirs(SHOT_DIR, exist_ok=True)
        fd, raw = tempfile.mkstemp(dir=SHOT_DIR, suffix=".dl")
        os.close(fd)
        tmp = raw + ".png"
        if _download(url, raw) and _normalize_image(raw, tmp):
            os.replace(tmp, path)
            prune_screenshots()
            return path
    except Exception:
        pass
    finally:
        _remove(raw, tmp)
    _shot_failed.add(url)
    return ""


def collect_raw():
    """[[name, arch, avail, inst, repo, summary, upgradable(0/1)]] sorted by name."""
    with ThreadPoolExecutor(max_workers=3) as ex:  # the three queries are independent
        f_avail = ex.submit(query, "--available", "--latest-limit=1")
        f_inst = ex.submit(query, "--installed")
        f_upg = ex.submit(query, "--upgrades", "--latest-limit=1")
        avail, inst, upg = f_avail.result(), f_inst.result(), f_upg.result()
    pk = {}
    for n, e, a, r, s in avail:
        pk[(n, a)] = [n, a, e, "", r, s, 0]
    for n, e, a, r, s in inst:
        pk.setdefault((n, a), [n, a, "", "", "(local)", s, 0])[3] = e
    for n, e, a, r, s in upg:
        d = pk.get((n, a))
        if d:
            d[2], d[6] = e, 1
    if not pk:
        raise RuntimeError("dnf5 returned no packages")
    return sorted(pk.values(), key=lambda d: (d[0], d[1]))


def _fold_cap(c):
    """One readable line per library; drop rpm/loader internals."""
    c = c.strip()
    if not c or c.startswith(("rpmlib(", "rtld(")):
        return ""
    m = SO_CAP.match(c)
    return m.group(1) if m else c


def _section(title, items, limit=300):
    out = [f"{title} ({len(items)}):"]
    out += [f"  {x}" for x in items[:limit]]
    if len(items) > limit:
        out.append(f"  ... and {len(items) - limit} more")
    return "\n".join(out)


def changelog_text(p):
    """Changelog; for upgradable packages only the entries newer than the installed version."""
    attempts = ([["--upgrades"]] if p.upgradable else []) + [["--count=10"]]
    for extra in attempts:
        out = ""
        if USE_CACHEONLY:
            out = run(["dnf5", "--cacheonly", "changelog", *extra, p.spec])
        if "Changelogs for" not in out:  # changelogs (other.xml) may not be cached yet
            out = run(["dnf5", "changelog", *extra, p.spec])
        if "Changelogs for" in out:
            return out.strip()
    return "No changelog available."


def detail_text(p, idx):
    """Text for one tab of the details pane (0 info, 1 dependencies, 2 files, 3 changelog)."""
    if idx == 3:
        return changelog_text(p)
    scope = ["--installed"] if p.installed else []
    if idx == 0:
        info = dnf5("info", p.spec) or "No information available."
        app = appstream_text(p.name)
        return f"{app}\n\n{INFO_RULE}\n\n{info}" if app else info
    if idx == 1:
        with ThreadPoolExecutor(max_workers=3) as ex:
            f_req = ex.submit(dnf5, "repoquery", *scope, "--requires", p.spec)
            f_pk = ex.submit(dnf5, "repoquery", *scope, "--providers-of=requires",
                             "--queryformat", PKG_QF, p.spec)
            f_prov = ex.submit(dnf5, "repoquery", *scope, "--provides", p.spec)
            req, pk, prov = f_req.result(), f_pk.result(), f_prov.result()
        needs = sorted(set(pk.split("\n")) - {""} - {p.name})
        caps = sorted({c for c in map(_fold_cap, req.split("\n")) if c})
        mine = sorted({c.split(" ")[0] for c in prov.split("\n") if c.strip()})[:500]
        by = []
        if mine:  # dependents that need anything this package provides, not just its name
            out = dnf5("repoquery", *scope, "--whatrequires=" + ",".join(mine),
                       "--queryformat", PKG_QF)
            by = sorted(set(out.split("\n")) - {""} - {p.name})
        return "\n\n".join([_section("Depends on (packages)", needs),
                            _section("Requires (libraries and capabilities)", caps),
                            _section("Required by (packages)", by)])
    out = dnf5("repoquery", *scope, "--files", p.spec)
    if not out.strip() and not p.installed:  # filelists metadata may not be cached yet
        out = dnf5("repoquery", *scope, "--files", p.spec, cache=False)
    lines = out.splitlines()
    if not lines:
        return "No file list available."
    if len(lines) > 20000:
        lines = lines[:20000] + ["... list truncated"]
    return "\n".join(lines)


def read_cache():
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) and data else None
    except Exception:
        return None


def write_cache(raw):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(raw, f, separators=(",", ":"))
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass


def parse_repos(text):
    """Parse `dnf5 repo list --all` into [(id, name, enabled)]."""
    repos = []
    for line in text.splitlines():
        t = line.split()
        if len(t) >= 2 and t[-1] in ("enabled", "disabled") and t[:2] != ["repo", "id"]:
            repos.append((t[0], " ".join(t[1:-1]), t[-1] == "enabled"))
    return repos


HISTORY_RE = re.compile(
    r"^\s*(\d+)\s+(.*?)\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(.*?)\s*$")


def parse_history(text):
    """Parse `dnf5 history list` into [(id, command line, date/time, actions)]."""
    out = []
    for line in text.splitlines():
        m = HISTORY_RE.match(line)
        if m:
            out.append((int(m.group(1)), m.group(2), m.group(3), " ".join(m.group(4).split())))
    return out


AUTO_RUNS_SHOWN = 30
PKG_ACTIONS = ("Install", "Upgrade", "Downgrade", "Reinstall", "Remove")
NEVRA_RE = re.compile(r"^(.+)-(?:\d+:)?[^-]+-[^-]+\.[^.]+$")


def auto_run_packages(ids):
    """{transaction id: [package names]} from one `dnf5 history info --json` call."""
    if not ids:
        return {}
    try:
        data = json.loads(run(["dnf5", "history", "info", "--json", *map(str, ids)]))
    except ValueError:
        return {}
    out = {}
    for t in data:
        names = []
        for p in t.get("packages", []):
            if p.get("action") in PKG_ACTIONS:
                m = NEVRA_RE.match(p.get("nevra", ""))
                names.append(m.group(1) if m else p.get("nevra", ""))
        out[t.get("id")] = sorted(set(names))
    return out


def is_auto_run(cmd):
    """True for transactions started by `dnf5 automatic` (the timer runs it with --timer)."""
    t = cmd.split()
    return len(t) > 1 and os.path.basename(t[0]) == "dnf5" and "automatic" in t[1:]


ACTIONS = {
    "install": ("Install", None),
    "remove": ("Remove", None),
    "upgrade": ("Upgrade", None),
    "reinstall": ("Reinstall", "Reinstall the installed version (dnf5 reinstall)"),
    "downgrade": ("Downgrade", "Install the next older available version (dnf5 downgrade)"),
    "pin": ("Pin version", "Lock the installed version (dnf5 versionlock add)"),
    "unpin": ("Unpin", "Remove the lock or block (dnf5 versionlock delete)"),
    "block": ("Block", "Exclude the versions currently available (dnf5 versionlock exclude)"),
}


CHANGE_ACTIONS = {"install", "remove", "upgrade", "reinstall", "downgrade"}


def action_valid(p, action):
    return {
        "install": not p.installed,
        "remove": p.installed,
        "upgrade": p.installed and p.upgradable,
        "reinstall": p.installed and p.latest == p.inst,  # current version is in a repo
        "downgrade": p.installed,
        "pin": p.installed and not p.pinned,
        "unpin": p.pinned,
        "block": not p.pinned,
    }[action]


def pkg_icon(p):
    if _apps is None:  # catalog still loading: don't cache a miss
        return None
    if p.icon is None:
        p.icon = appstream_icon(p.name, ("64x64", "128x128"))
    return texture_from(p.icon) if p.icon else None


class Pkg(GObject.Object):
    """Plain Python attributes on purpose: GObject properties cost a C<->Python round
    trip per access, which is painful with tens of thousands of packages."""
    __gtype_name__ = "Dnf5Pkg"

    def __init__(self, d, pinned):
        super().__init__()
        self.name, self.arch, self.latest, self.inst, self.repo, self.summary, upg = d
        self.installed = bool(self.inst)
        self.upgradable = bool(upg)
        self.pinned = pinned
        self.actions = set()
        self.icon = None  # "" means none, None means not looked up yet
        self.version = self.inst or self.latest
        self.hay = f"{self.name} {self.summary}".lower()
        self.status = self.queued = ""
        self.refresh()

    @property
    def spec(self):
        return f"{self.name}.{self.arch}"

    def refresh(self):
        s = []
        if self.installed:
            s.append("upgradable" if self.upgradable else "installed")
        if self.pinned:
            s.append("pinned")
        self.status = ", ".join(s)
        self.queued = ", ".join(sorted(self.actions))


class PkgModel(GObject.Object, Gio.ListModel):
    """Minimal Python list model. Filtering and sorting happen in plain Python lists
    (milliseconds) instead of Gtk filter/sort models calling back into Python per row."""

    def __init__(self):
        super().__init__()
        self.rows = []

    def do_get_item_type(self):
        return Pkg.__gtype__

    def do_get_n_items(self):
        return len(self.rows)

    def do_get_item(self, position):
        return self.rows[position] if 0 <= position < len(self.rows) else None

    def set_rows(self, rows):
        removed = len(self.rows)
        self.rows = rows
        self.items_changed(0, removed, len(rows))


def build_pkgs(raw):
    pins = load_pins()
    return [Pkg(d, d[0] in pins) for d in raw]


# ---------------------------------------------------------------- run window
class RunWindow(Adw.Window):
    """Shows what will run, then streams output. Run button executes it."""

    def __init__(self, parent, title, argv, preview, on_done=None, autorun=False, dry=None,
                 rebuild=None):
        super().__init__(transient_for=parent, modal=True, title=title,
                         default_width=800, default_height=500)
        self.argv, self.on_done = argv, on_done
        self.rebuild = rebuild   # rebuild(erase) -> (argv, preview, dry), used to retry
        self.erasing = False
        self.tv = Gtk.TextView(editable=False, monospace=True, left_margin=8,
                               top_margin=8, bottom_margin=8, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.buf = self.tv.get_buffer()
        self.buf.set_text(preview + "\n")
        hb = Adw.HeaderBar()
        self.close_btn = Gtk.Button(label="Close")
        self.close_btn.connect("clicked", lambda *_: self.close())
        self.run_btn = Gtk.Button(label="Run", css_classes=["suggested-action"])
        self.run_btn.connect("clicked", lambda *_: self.start())
        self.retry_btn = Gtk.Button(
            label="Retry allowing package removal", visible=False,
            tooltip_text="Run the dry run again with --allowerasing. "
                         "Nothing is removed until you press Run.")
        self.retry_btn.connect("clicked", lambda *_: self.retry())
        hb.pack_start(self.close_btn)
        hb.pack_end(self.run_btn)
        hb.pack_end(self.retry_btn)
        tb = Adw.ToolbarView()
        tb.add_top_bar(hb)
        tb.set_content(Gtk.ScrolledWindow(vexpand=True, child=self.tv))
        self.set_content(tb)
        if dry:
            self.run_btn.set_sensitive(False)
            self.append("Calculating transaction (dry run)...\n")
            threading.Thread(target=self._dry_worker, args=(dry,), daemon=True).start()
        elif autorun:
            self.start()

    def append(self, text):
        self.buf.insert(self.buf.get_end_iter(), text)
        self.buf.place_cursor(self.buf.get_end_iter())
        self.tv.scroll_to_mark(self.buf.get_insert(), 0, False, 0, 0)
        return False

    def _dry_worker(self, dry):
        out = []
        for cmd in dry:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True)
                out.append("$ " + shlex.join(cmd) + "\n" + r.stdout + r.stderr)
            except Exception as e:
                out.append(f"Error running {shlex.join(cmd)}: {e}\n")
        GLib.idle_add(self._dry_done, "\n".join(out).replace("\r", "\n"))

    def _dry_done(self, text):
        failed = "Failed to resolve the transaction" in text
        warn = ""
        if self.erasing and not failed:
            warn = ("WARNING: package removal is allowed. Packages you did not select may be "
                    "removed. Review the 'Removing' entries below.\n\n")
        elif "Removing dependent packages" in text or "Removing unused dependencies" in text:
            warn = "WARNING: this transaction also removes other packages. Review carefully.\n\n"
        if failed:
            can_retry = self.rebuild is not None and not self.erasing
            tail = "\n--- dry run failed; nothing was changed ---\n"
            if can_retry:
                tail += 'Use "Retry allowing package removal" to see what that would do.\n'
            elif self.erasing:
                tail += "Allowing package removal was not enough to resolve this.\n"
            self.append("\n" + text + tail)
            self.retry_btn.set_visible(can_retry)
            return False  # Run stays disabled: it would fail the same way
        self.append("\n" + warn + text + "\n--- dry run finished; press Run to apply ---\n")
        self.run_btn.set_sensitive(True)
        return False

    def retry(self):
        self.erasing = True
        self.retry_btn.set_visible(False)
        self.run_btn.set_sensitive(False)
        self.argv, preview, dry = self.rebuild(True)
        self.append("\n--- retrying with --allowerasing ---\n" + preview +
                    "\nCalculating transaction (dry run)...\n")
        threading.Thread(target=self._dry_worker, args=(dry,), daemon=True).start()

    def start(self):
        self.run_btn.set_sensitive(False)
        self.close_btn.set_sensitive(False)
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        rc = -1
        try:
            p = subprocess.Popen(self.argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            fd = p.stdout.fileno()
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                GLib.idle_add(self.append, chunk.decode(errors="replace").replace("\r", "\n"))
            rc = p.wait()
        except Exception as e:
            GLib.idle_add(self.append, f"\nError: {e}\n")
        GLib.idle_add(self._finish, rc)

    def _finish(self, rc):
        self.append(f"\n--- finished (exit code {rc}) ---\n")
        self.close_btn.set_sensitive(True)
        if self.on_done:
            self.on_done(rc)
        return False


# ---------------------------------------------------------------- main window
class HistoryWindow(Adw.Window):
    """Native transaction history with details, undo and rollback."""

    def __init__(self, parent, auto_only=False):
        super().__init__(transient_for=parent, modal=True, title="Transaction history",
                         default_width=760, default_height=600)
        self.parent_win = parent
        self.placeholder = Gtk.Label(label="Loading\u2026", css_classes=["dim-label"],
                                     margin_top=48, margin_bottom=48)
        self.box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                               css_classes=["boxed-list"], margin_top=12, margin_bottom=12,
                               margin_start=12, margin_end=12, valign=Gtk.Align.START)
        self.box.set_placeholder(self.placeholder)
        self.loaded = False
        self.box.set_filter_func(lambda row: not self.only_auto.get_active() or row.auto)
        self.only_auto = Gtk.ToggleButton(label="Automatic only")
        self.only_auto.set_active(auto_only)
        self.only_auto.connect("toggled", self._filter)
        hb = Adw.HeaderBar()
        hb.pack_start(self.only_auto)
        tb = Adw.ToolbarView()
        tb.add_top_bar(hb)
        tb.set_content(Gtk.ScrolledWindow(vexpand=True, child=self.box))
        self.set_content(tb)
        threading.Thread(target=self._load, daemon=True).start()

    def _filter(self, *_):
        self.box.invalidate_filter()
        if self.loaded:
            self._set_empty_text()

    def _set_empty_text(self):
        self.placeholder.set_text("No automatic runs found" if self.only_auto.get_active()
                                  else "No transactions found")

    def _load(self):
        items = parse_history(run(["dnf5", "history", "list"]))
        recent = [tid for tid, cmd, _w, _a in items if is_auto_run(cmd)]
        recent = sorted(recent)[-AUTO_RUNS_SHOWN:]
        GLib.idle_add(self._populate, items, auto_run_packages(recent))

    def _populate(self, items, pkgs):
        self.loaded = True
        self._set_empty_text()
        for tid, cmd, when, actions in items:
            auto = is_auto_run(cmd)
            sub = f"#{tid} \u00b7 {when} \u00b7 {actions}"
            names = pkgs.get(tid)
            if names:
                sub += "\n" + ", ".join(names[:8]) + (f" +{len(names) - 8} more" if len(names) > 8 else "")
            row = Adw.ActionRow(title="Automatic update" if auto
                                else GLib.markup_escape_text(cmd or "(no command line)"),
                                subtitle=GLib.markup_escape_text(sub), activatable=True)
            row.auto = auto
            row.connect("activated", lambda _r, t=tid: self.details(t))
            for icon, tip, fn in (
                    ("edit-undo-symbolic", "Undo this transaction", self.undo),
                    ("document-revert-symbolic", "Roll back to the state after this one",
                     self.rollback)):
                b = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER,
                               css_classes=["flat"])
                b.connect("clicked", lambda _b, t=tid, f=fn: f(t))
                row.add_suffix(b)
            self.box.append(row)
        return False

    def details(self, tid):
        w = RunWindow(self, f"Transaction {tid}", ["dnf5", "history", "info", str(tid)],
                      f"dnf5 history info {tid}", autorun=True)
        w.run_btn.set_visible(False)
        w.present()

    def _transact(self, verb, tid):
        self.close()
        self.parent_win.run_cmd(
            f"{verb.capitalize()} transaction {tid}",
            ["pkexec", "dnf5", "-y", "history", verb, str(tid)],
            f"dnf5 -y history {verb} {tid} (as root)\n\nThe dry run below shows what would change.",
            reload=True, dry=[["dnf5", "history", verb, str(tid), "--assumeno"]])

    def undo(self, tid):
        self._transact("undo", tid)

    def rollback(self, tid):
        self._transact("rollback", tid)


class PinsWindow(Adw.Window):
    """Native list of versionlock entries with per-row remove."""

    def __init__(self, parent):
        super().__init__(transient_for=parent, modal=True, title="Pinned packages",
                         default_width=640, default_height=520)
        self.parent_win = parent
        hb = Adw.HeaderBar()
        clear = Gtk.Button(label="Remove all", css_classes=["destructive-action"])
        clear.connect("clicked", lambda *_: self.remove_all())
        hb.pack_start(clear)
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"],
                          margin_top=12, margin_bottom=12, margin_start=12, margin_end=12,
                          valign=Gtk.Align.START)
        entries = load_pin_entries()
        clear.set_sensitive(bool(entries))
        for name, detail in entries:
            row = Adw.ActionRow(title=GLib.markup_escape_text(name),
                                subtitle=GLib.markup_escape_text(detail))
            btn = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                             css_classes=["flat"], tooltip_text="Remove pin")
            btn.connect("clicked", lambda _b, n=name: self.remove(n))
            row.add_suffix(btn)
            box.append(row)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        if not entries:
            body.append(Gtk.Label(label="No pinned packages", css_classes=["dim-label"],
                                  margin_top=48))
        body.append(box)
        tb = Adw.ToolbarView()
        tb.add_top_bar(hb)
        tb.set_content(Gtk.ScrolledWindow(vexpand=True, child=body))
        self.set_content(tb)

    def remove(self, name):
        self.close()
        self.parent_win.run_root(f"Unpinned {name}", [["dnf5", "versionlock", "delete", name]])

    def remove_all(self):
        dlg = Adw.AlertDialog(heading="Remove all pins?",
                              body="Every version lock and block will be deleted.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("remove", "Remove all")
        dlg.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda _d, r: r == "remove" and self._clear_pins())
        dlg.present(self)

    def _clear_pins(self):
        self.close()
        self.parent_win.run_root("Removed all pins", [["dnf5", "versionlock", "clear"]])


class AutoUpdatesWindow(Adw.Window):
    def __init__(self, parent):
        super().__init__(transient_for=parent, modal=True, title="Automatic updates",
                         default_width=560, default_height=600)
        self.parent_win = parent
        have, enabled, nxt = timer_state()
        cfg = automatic_settings()
        self.gs = gnome_software_settings()
        self.scopes = list(AUTO_SCOPES) + [v for v in [cfg["scope"]] if v not in AUTO_SCOPES]
        self.reboots = list(AUTO_REBOOTS) + [v for v in [cfg["reboot"]] if v not in AUTO_REBOOTS]
        self.loaded = {"enabled": enabled, "mode": cfg["mode"],
                       "gnome": bool(self.gs and self.gs.get_boolean("download-updates")),
                       "gs_paused": bool(self.gs and gs_paused()),
                       "notify": notify_enabled(),
                       "scope": self.scopes.index(cfg["scope"]),
                       "reboot": self.reboots.index(cfg["reboot"])}

        hb = Adw.HeaderBar()
        self.apply_btn = Gtk.Button(label="Apply", css_classes=["suggested-action"])
        self.apply_btn.connect("clicked", lambda *_: self.apply())
        hb.pack_end(self.apply_btn)

        page = Adw.PreferencesPage()
        if not have:
            row = Adw.ActionRow(title="dnf5 automatic is not installed", subtitle=AUTO_PKG)
            btn = Gtk.Button(label="Install", valign=Gtk.Align.CENTER,
                             css_classes=["suggested-action"])
            btn.connect("clicked", lambda *_: self.install_plugin())
            row.add_suffix(btn)
            grp = Adw.PreferencesGroup()
            grp.add(row)
            page.add(grp)

        self.sw = Adw.SwitchRow(title="Run automatically", subtitle=AUTO_TIMER, active=enabled)
        self.sw.connect("notify::active", lambda *_: self.refresh())
        when = nxt if enabled and nxt not in ("", "n/a") else "Not scheduled"
        sched = Adw.PreferencesGroup(title="Schedule",
                                     description=f"Settings are saved to {AUTO_CONF}")
        sched.add(self.sw)
        sched.add(Adw.ActionRow(title="Next run", subtitle=GLib.markup_escape_text(when)))

        self.mode_row = self.combo("Mode", AUTO_MODE_LABELS, self.loaded["mode"])
        self.scope_row = self.combo("Which updates", [AUTO_SCOPES.get(v, f"{v} (current config)")
                                                for v in self.scopes], self.loaded["scope"])
        self.notify_sw = Adw.SwitchRow(title="Desktop notifications", active=self.loaded["notify"],
                                       subtitle="Shows what was downloaded or installed")
        self.notify_sw.connect("notify::active", lambda *_: self.refresh())
        sched.add(self.notify_sw)
        what = Adw.PreferencesGroup(title="Updates")
        what.add(self.mode_row)
        what.add(self.scope_row)

        self.reboot_row = self.combo("Restart after installing",
                                     [AUTO_REBOOTS.get(v, f"{v} (current config)")
                                      for v in self.reboots], self.loaded["reboot"])
        restart = Adw.PreferencesGroup(
            title="Restart",
            description="Only applies when updates are installed. "
                        "By default dnf5 waits 5 minutes before restarting.")
        restart.add(self.reboot_row)

        for grp in (sched, what, restart):
            grp.set_sensitive(have)
            page.add(grp)
        if self.gs:
            self.gs_row = Adw.SwitchRow(title="Download updates automatically",
                                        subtitle="GNOME Software", active=self.loaded["gnome"])
            self.gs_row.connect("notify::active", lambda *_: self.refresh())
            self.gs_group = Adw.PreferencesGroup(title="GNOME Software", description=GS_NOTE)
            self.gs_group.add(self.gs_row)
            self.gs_pause = Adw.SwitchRow(title="Pause background activity",
                                          subtitle="Stops its background refresh and its own notifications",
                                          active=self.loaded["gs_paused"])
            self.gs_pause.connect("notify::active", lambda *_: self.refresh())
            self.gs_group.add(self.gs_pause)
            page.add(self.gs_group)
        tb = Adw.ToolbarView()
        tb.add_top_bar(hb)
        tb.set_content(page)
        self.set_content(tb)
        self.refresh()

    def combo(self, title, labels, selected):
        row = Adw.ComboRow(title=title)
        row.set_model(Gtk.StringList.new(labels))
        row.set_selected(selected)
        row.connect("notify::selected", lambda *_: self.refresh())
        return row

    def current(self):
        return {"enabled": self.sw.get_active(), "mode": self.mode_row.get_selected(),
                "scope": self.scope_row.get_selected(), "reboot": self.reboot_row.get_selected(),
                "gnome": bool(self.gs and self.gs_row.get_active()),
                "gs_paused": bool(self.gs and self.gs_pause.get_active()),
                "notify": self.notify_sw.get_active()}

    def refresh(self):
        now = self.current()
        self.mode_row.set_subtitle(AUTO_MODE_HINTS[now["mode"]])
        self.reboot_row.set_sensitive(now["mode"] == 2)
        if self.gs:
            self.gs_group.set_description(GS_BOTH if now["enabled"] and now["gnome"] else GS_NOTE)
        self.apply_btn.set_sensitive(now != self.loaded)

    def apply(self):
        now = self.current()
        vals = {}  # only touch what changed, so unrelated values in the file survive
        if now["mode"] != self.loaded["mode"]:
            vals.update(AUTO_MODES[now["mode"]])
        if now["scope"] != self.loaded["scope"]:
            vals["upgrade_type"] = self.scopes[now["scope"]]
        if now["reboot"] != self.loaded["reboot"]:
            vals["reboot"] = self.reboots[now["reboot"]]
        if self.gs and now["gnome"] != self.loaded["gnome"]:
            self.gs.set_boolean("download-updates", now["gnome"])
        if self.gs and now["gs_paused"] != self.loaded["gs_paused"]:
            err = set_gs_paused(now["gs_paused"])
            if err:
                self.close()
                self.parent_win.toast(f"Failed: {err}")
                return
        cmds = []
        edits = [("commands", vals)]
        if now["notify"] != self.loaded["notify"]:
            err = set_notify(now["notify"])
            if err:
                self.close()
                self.parent_win.toast(f"Failed: {err}")
                return
        if now["notify"] and not self.loaded["notify"]:
            sec, via = emit_settings()
            if "command" not in via:
                edits += [(sec, {"emit_via": ",".join(via + ["command"])}),
                          ("command", {"command_format": f"cat > {REPORT_FILE}"})]
            cmds.append(["install", "-d", "-m", "755", os.path.dirname(REPORT_FILE)])
        if any(v for _s, v in edits):
            try:
                with open(AUTO_CONF) as f:
                    old = f.read()
            except OSError:
                old = ""
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = os.path.join(CACHE_DIR, "automatic.conf")
            for name, v in edits:
                if v:
                    old = set_ini_keys(old, name, v)
            with open(tmp, "w") as f:
                f.write(old)
            # install, not mv: the new file gets the SELinux label of /etc/dnf
            cmds.append(["install", "-m", "644", tmp, AUTO_CONF])
        if now["enabled"]:
            unit = guard_unit()
            try:
                with open(GUARD_FILE) as f:
                    have = f.read()
            except OSError:
                have = ""
            if have != unit:
                tmp = os.path.join(CACHE_DIR, "guard.conf")
                os.makedirs(CACHE_DIR, exist_ok=True)
                with open(tmp, "w") as f:
                    f.write(unit)
                cmds += [["install", "-D", "-m", "644", tmp, GUARD_FILE],
                         ["systemctl", "daemon-reload"]]
        if now["enabled"] != self.loaded["enabled"]:
            cmds.append(["systemctl", "enable" if now["enabled"] else "disable", "--now", AUTO_TIMER])
        self.close()
        if cmds:
            self.parent_win.run_root("Automatic updates saved", cmds)
        else:
            self.parent_win.toast("Automatic updates saved")

    def install_plugin(self):
        self.close()
        cmd = ["dnf5", "-y", "install", AUTO_PKG]
        dry = [c for c in cmd if c != "-y"] + ["--assumeno"]
        self.parent_win.run_cmd(f"Install {AUTO_PKG}", ["pkexec", *cmd],
                                shlex.join(cmd) + "\n\nThe dry run below shows what will be installed.",
                                reload=True, dry=[dry])


class CompsPage(Gtk.Box):
    """List of comps groups or environments with info and install/remove."""

    def __init__(self, win, kind):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=8,
                         margin_bottom=8, margin_start=12, margin_end=12)
        self.win, self.kind = win, kind
        self.items, self.row_ids, self.info_cache = {}, {}, {}
        self.gen = self.token = 0
        self.search = Gtk.SearchEntry(hexpand=True, placeholder_text="Filter by name or id")
        self.search.connect("search-changed", lambda *_: self.listbox.invalidate_filter())
        self.installed_only = Gtk.CheckButton(label="Installed only")
        self.installed_only.connect("toggled", lambda *_: self.listbox.invalidate_filter())
        top = Gtk.Box(spacing=12)
        top.append(self.search)
        top.append(self.installed_only)
        self.hidden = None
        if kind == "group":
            self.hidden = Gtk.CheckButton(label="Show hidden")
            self.hidden.connect("toggled", lambda *_: self.load())
            top.append(self.hidden)
        self.append(top)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE,
                                   css_classes=["boxed-list"], valign=Gtk.Align.START)
        self.listbox.set_filter_func(self._match)
        self.listbox.set_placeholder(Gtk.Label(label="Loading\u2026", css_classes=["dim-label"],
                                               margin_top=24, margin_bottom=24))
        self.listbox.connect("row-selected", self.on_select)
        self.info = Gtk.TextView(editable=False, monospace=True, left_margin=8, top_margin=8,
                                 bottom_margin=8, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, vexpand=True,
                          position=300, shrink_start_child=False, shrink_end_child=False)
        paned.set_start_child(Gtk.ScrolledWindow(child=self.listbox))
        paned.set_end_child(Gtk.ScrolledWindow(child=self.info))
        self.append(paned)
        self.optional = Gtk.CheckButton(label="Include optional packages", hexpand=True,
                                        halign=Gtk.Align.START)
        self.remove_btn = Gtk.Button(label="Remove", css_classes=["destructive-action"],
                                     sensitive=False)
        self.remove_btn.connect("clicked", lambda *_: self.act("remove"))
        self.install_btn = Gtk.Button(label="Install", css_classes=["suggested-action"],
                                      sensitive=False)
        self.install_btn.connect("clicked", lambda *_: self.act("install"))
        bar = Gtk.Box(spacing=8)
        bar.append(self.optional)
        bar.append(self.remove_btn)
        bar.append(self.install_btn)
        self.append(bar)
        self.load()

    def hidden_on(self):
        return bool(self.hidden and self.hidden.get_active())

    def load(self):
        self.gen += 1
        gen, hidden = self.gen, self.hidden_on()

        def work():
            try:
                items = parse_comps(comps_text(self.kind, "list", hidden=hidden))
            except OSError:
                items = []
            GLib.idle_add(self._populate, items, gen)

        threading.Thread(target=work, daemon=True).start()

    def _populate(self, items, gen):
        if gen != self.gen:
            return False
        self.listbox.remove_all()
        self.items = {i: (n, inst) for i, n, inst in items}
        self.row_ids, self.info_cache = {}, {}
        for gid, name, inst in items:
            row = Adw.ActionRow(title=GLib.markup_escape_text(name),
                                subtitle=GLib.markup_escape_text(gid))
            if inst:
                row.add_prefix(Gtk.Image.new_from_icon_name("object-select-symbolic"))
            self.row_ids[row] = gid
            self.listbox.append(row)
        self.listbox.set_placeholder(Gtk.Label(
            label="Nothing to show", css_classes=["dim-label"], margin_top=24, margin_bottom=24))
        self.update_buttons()
        return False

    def _match(self, row):
        name, inst = self.items[self.row_ids[row]]
        if self.installed_only.get_active() and not inst:
            return False
        q = self.search.get_text().strip().lower()
        return not q or q in name.lower() or q in self.row_ids[row].lower()

    def selected_id(self):
        row = self.listbox.get_selected_row()
        return self.row_ids.get(row) if row else None

    def update_buttons(self):
        gid = self.selected_id()
        inst = self.items[gid][1] if gid in self.items else None
        self.install_btn.set_sensitive(inst is False)
        self.remove_btn.set_sensitive(inst is True)

    def on_select(self, _lb, row):
        self.token += 1
        self.update_buttons()
        gid = self.row_ids.get(row) if row else None
        if gid is None:
            self.info.get_buffer().set_text("")
        elif gid in self.info_cache:
            self.info.get_buffer().set_text(self.info_cache[gid])
        else:
            self.info.get_buffer().set_text("Loading\u2026")
            GLib.timeout_add(250, self._start_info, gid, self.token)

    def _start_info(self, gid, tok):
        if tok == self.token:
            args = (gid, tok, self.hidden_on())
            threading.Thread(target=self._info_worker, args=args, daemon=True).start()
        return False

    def _info_worker(self, gid, tok, hidden):
        text = comps_text(self.kind, "info", gid, hidden=hidden).strip() or "(no information)"
        GLib.idle_add(self._info_done, gid, tok, text)

    def _info_done(self, gid, tok, text):
        self.info_cache[gid] = text
        if tok == self.token:
            self.info.get_buffer().set_text(text)
        return False

    def act(self, verb):
        gid = self.selected_id()
        if gid is None:
            return
        optional = verb == "install" and self.optional.get_active()
        kind = self.kind

        def build(erase):
            cmd = ["dnf5", "-y", kind, verb, gid]
            if optional:
                cmd.append("--with-optional")
            if erase:
                cmd.append("--allowerasing")
            dry = [c for c in cmd if c != "-y"] + ["--assumeno"]
            preview = shlex.join(cmd) + "\n\nThe dry run below shows what will change."
            return ["pkexec", *cmd], preview, [dry]

        argv, preview, dry = build(False)
        title = f"{verb.capitalize()} {self.items[gid][0]}"
        RunWindow(self.win, title, argv, preview, on_done=lambda rc: self.win.refresh_all(),
                  dry=dry, rebuild=build if verb == "install" else None).present()


class GroupsWindow(Adw.Window):
    """Package groups and environments (dnf5 group / dnf5 environment)."""

    def __init__(self, parent):
        super().__init__(transient_for=parent, modal=True, title="Groups and environments",
                         default_width=760, default_height=680)
        self.parent_win = parent
        self.pages = [CompsPage(self, "group"), CompsPage(self, "environment")]
        stack = Adw.ViewStack()
        stack.add_titled(self.pages[0], "group", "Groups")
        stack.add_titled(self.pages[1], "environment", "Environments")
        hb = Adw.HeaderBar()
        hb.set_title_widget(Adw.ViewSwitcher(policy=Adw.ViewSwitcherPolicy.WIDE, stack=stack))
        tb = Adw.ToolbarView()
        tb.add_top_bar(hb)
        tb.set_content(stack)
        self.set_content(tb)

    def refresh_all(self):
        for page in self.pages:
            page.load()
        self.parent_win.reload()


class RepoWindow(Adw.Window):
    """Enable/disable repositories and add local (file://) repositories."""

    def __init__(self, parent):
        super().__init__(transient_for=parent, modal=True, title="Repositories",
                         default_width=720, default_height=560)
        self.parent_win = parent
        self.rows = {}  # repo id -> (SwitchRow, original enabled state)
        hb = Adw.HeaderBar()
        add_btn = Gtk.Button(label="Add local repo\u2026")
        add_btn.connect("clicked", lambda *_: self.pick_folder())
        hb.pack_start(add_btn)
        self.save_btn = Gtk.Button(label="Apply", sensitive=False,
                                   css_classes=["suggested-action"])
        self.save_btn.connect("clicked", lambda *_: self.apply())
        hb.pack_end(self.save_btn)
        self.box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                               css_classes=["boxed-list"], margin_top=12, margin_bottom=12,
                               margin_start=12, margin_end=12, valign=Gtk.Align.START)
        tb = Adw.ToolbarView()
        tb.add_top_bar(hb)
        self.search = Gtk.SearchEntry(placeholder_text="Filter repositories",
                                      margin_top=12, margin_start=12, margin_end=12)
        self.search.connect("search-changed", lambda *_: self.box.invalidate_filter())
        self.box.set_filter_func(self._filter)
        self.box.set_sort_func(self._sort)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self.search)
        body.append(Gtk.ScrolledWindow(vexpand=True, child=self.box))
        tb.set_content(body)
        self.overlay = Adw.ToastOverlay(child=tb)
        self.set_content(self.overlay)
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        GLib.idle_add(self._populate, parse_repos(run(["dnf5", "repo", "list", "--all"])))

    def _populate(self, repos):
        while (row := self.box.get_first_child()):
            self.box.remove(row)
        self.rows = {}
        for rid, name, enabled in repos:
            row = Adw.SwitchRow(title=GLib.markup_escape_text(rid),
                                subtitle=GLib.markup_escape_text(name), active=enabled)
            row.rid, row.rname, row.orig = rid, name, enabled
            row.connect("notify::active", lambda *_: self.on_toggle())
            self.box.append(row)
            self.rows[rid] = (row, enabled)
        return False

    def changes(self):
        return {rid: row.get_active() for rid, (row, orig) in self.rows.items()
                if row.get_active() != orig}

    def on_toggle(self):
        self.save_btn.set_sensitive(bool(self.changes()))

    def _filter(self, row):
        q = self.search.get_text().strip().lower()
        return not q or q in f"{row.rid} {row.rname}".lower()

    def _sort(self, a, b):
        # enabled repos first (by original state, so rows don't jump on toggle), then by id
        ka, kb = (not a.orig, a.rid.lower()), (not b.orig, b.rid.lower())
        return (ka > kb) - (ka < kb)

    def apply(self):
        cmds = [["dnf5", "config-manager", "setopt", f"{rid}.enabled={int(on)}"]
                for rid, on in self.changes().items()]
        if cmds:
            self.close()
            self.parent_win.run_root("Repositories updated", cmds)

    def pick_folder(self):
        dlg = Gtk.FileDialog(title="Select a folder containing RPMs and repodata/")
        dlg.select_folder(self, None, self._folder_chosen)

    def _folder_chosen(self, dlg, res):
        try:
            folder = dlg.select_folder_finish(res)
        except GLib.Error:
            return
        path = folder.get_path()
        if not path:
            return
        if not os.path.exists(os.path.join(path, "repodata", "repomd.xml")):
            self.overlay.add_toast(Adw.Toast.new(
                "No repodata/ found in that folder. Run createrepo_c on it first."))
            return
        rid = "local-" + re.sub(r"[^A-Za-z0-9_.-]", "-", os.path.basename(path.rstrip("/")) or "repo")
        lines = [f"[{rid}]", f"name=Local repo {rid}", f"baseurl={folder.get_uri()}",
                 "enabled=1", "gpgcheck=0", "skip_if_unavailable=1"]
        script = "printf '%s\\n' " + " ".join(shlex.quote(x) for x in lines) + \
                 f" > /etc/yum.repos.d/{rid}.repo"
        preview = f"Create /etc/yum.repos.d/{rid}.repo (as root):\n\n" + \
                  "\n".join("  " + x for x in lines) + \
                  "\n\nNote: gpgcheck is disabled because local repos are usually unsigned.\n" \
                  "Press Run to execute."
        self.close()
        self.parent_win.run_cmd("Add local repo", ["pkexec", "sh", "-c", script],
                                preview, reload=True)


class Win(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Dnfshelf",
                         default_width=1250, default_height=780)
        self.model = PkgModel()
        self.all = []          # every package, kept sorted
        self.queue = set()
        self.query = ""
        self.info_token = 0
        self.info_timer = 0
        self.gen = 0           # load generation, so stale loads are ignored
        self.live = set()      # currently bound Status/Queued cells
        self.icon_cells = set()  # currently bound Name cells
        self.sort_prop, self.sort_desc = "name", False
        self.col_props = {}
        self.cur = None        # package shown in the details pane
        self.detail_cache = {}
        self.sets = {}         # mode -> {(name, arch)} for the SET_QUERIES modes, lazy
        self.set_loading = set()  # modes currently being queried
        self.loading_main = False
        self.checking = False  # a metadata refresh is running
        self.n_upg = 0         # packages with an upgrade available
        self.after_load = None  # one-shot callback run when the next reload finishes
        self.reboot_checking = False

        # header bar
        hb = Adw.HeaderBar()
        self.mode = Gtk.DropDown.new_from_strings(MODES)
        self.mode.set_selected(M_USER)
        self.mode.connect("notify::selected", lambda *_: self.refilter())
        hb.pack_start(self.mode)
        self.check_btn = Gtk.Button(icon_name="view-refresh-symbolic",
                                    tooltip_text="Check for updates")
        self.check_btn.connect("clicked", lambda *_: self.check_updates())
        hb.pack_start(self.check_btn)
        self.spin = Gtk.Spinner()
        hb.pack_start(self.spin)
        self.search = Gtk.SearchEntry(placeholder_text=f"Search \u00b7 {SEARCH_MODES[0]}",
                                      width_request=420)
        self.search.connect("search-changed", self.on_search)
        self.search.get_delegate().set_margin_end(32)  # room for the mode button
        mode_menu = Gio.Menu()
        for i, label in enumerate(SEARCH_MODES):
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value("win.search-mode", GLib.Variant.new_string(str(i)))
            mode_menu.append_item(item)
        mode_btn = Gtk.MenuButton(icon_name="pan-down-symbolic", menu_model=mode_menu,
                                  tooltip_text="What the search matches", halign=Gtk.Align.END,
                                  valign=Gtk.Align.CENTER, margin_end=32,
                                  css_classes=["flat", "circular"])
        overlay = Gtk.Overlay(child=self.search)
        overlay.add_overlay(mode_btn)
        hb.set_title_widget(overlay)
        self.search_idx = 0
        mode_act = Gio.SimpleAction.new_stateful("search-mode", GLib.VariantType.new("s"),
                                                 GLib.Variant.new_string("0"))
        mode_act.connect("activate", self.on_search_mode)
        self.add_action(mode_act)
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=self.build_menu())
        hb.pack_end(menu_btn)
        self.apply_btn = Gtk.Button(label="Apply", sensitive=False,
                                    css_classes=["suggested-action"])
        self.apply_btn.connect("clicked", lambda *_: self.apply())
        hb.pack_end(self.apply_btn)

        # package table
        self.view = Gtk.ColumnView(show_row_separators=True)
        self.add_col("Status", "status", width=120)
        self.add_col("Queued", "queued", width=110)
        name_col = self.add_col("Name", "name", width=260, icon=True)
        self.add_col("Version", "version", width=190)
        self.add_col("Latest", "latest", width=190)
        self.add_col("Repo", "repo", width=130)
        self.add_col("Summary", "summary", expand=True)

        self.selection = Gtk.MultiSelection(model=self.model)
        self.selection.connect("selection-changed", self.on_select)
        self.view.set_model(self.selection)
        # Header clicks only update the view's sorter; we sort our own list when it changes.
        self.view.get_sorter().connect("changed", self.on_sort_changed)

        # bottom: actions + info
        bar = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        bar.set_size_request(-1, 48)
        self.hint = Gtk.Label(label="Select packages to see available actions",
                              css_classes=["dim-label"])
        bar.append(self.hint)
        self.action_btns = {}
        for act, (label, tip) in ACTIONS.items():
            b = Gtk.Button(label=label, tooltip_text=tip, visible=False)
            b.connect("clicked", lambda _b, a=act: self.mark(a))
            bar.append(b)
            self.action_btns[act] = b
        self.clear_btn = Gtk.Button(label="Clear queue", visible=False)
        self.clear_btn.connect("clicked", lambda *_: self.clear_queue())
        bar.append(self.clear_btn)
        self.count = Gtk.Label(label="", hexpand=True, xalign=1)
        bar.append(self.count)

        self.tab_views = []
        self.notebook = Gtk.Notebook(vexpand=True)
        for title in ("Info", "Dependencies", "Files", "Changelog"):
            tv = Gtk.TextView(editable=False, monospace=True, left_margin=8, top_margin=8,
                              wrap_mode=Gtk.WrapMode.WORD_CHAR)
            self.tab_views.append(tv)
            self.notebook.append_page(
                Gtk.ScrolledWindow(vexpand=True, min_content_height=170, child=tv),
                Gtk.Label(label=title))
        self.notebook.append_page(self.build_shot_tab(), Gtk.Label(label="Screenshots"))
        self.notebook.connect("switch-page", self.on_tab_switch)
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bottom.append(bar)
        bottom.append(self.notebook)

        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, position=480,
                          start_child=Gtk.ScrolledWindow(vexpand=True, child=self.view),
                          end_child=bottom, shrink_start_child=False, shrink_end_child=False)

        tb = Adw.ToolbarView()
        tb.add_top_bar(hb)
        self.banner = Adw.Banner(button_label="Reboot", revealed=False)
        self.banner.connect("button-clicked", lambda *_: self.confirm_reboot())
        tb.add_top_bar(self.banner)
        tb.set_content(paned)
        self.overlay = Adw.ToastOverlay(child=tb)
        self.set_content(self.overlay)
        self.install_actions()
        threading.Thread(target=self.load_catalog, daemon=True).start()
        self.view.sort_by_column(name_col, Gtk.SortType.ASCENDING)  # last: needs self.count

    # ---- UI helpers
    def add_col(self, title, prop, width=None, expand=False, icon=False):
        f = Gtk.SignalListItemFactory()
        live = prop in ("status", "queued")  # the only cells that change after load

        def setup(_f, li):
            lbl = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                            single_line_mode=True, max_width_chars=1,
                            margin_start=6, margin_end=6)
            click = Gtk.GestureClick(button=3)
            click.connect("pressed", lambda g, n, x, y, li=li, w=lbl: self.context_menu(li, w, x, y))
            lbl.add_controller(click)
            if not icon:
                li.set_child(lbl)
                return
            box = Gtk.Box(spacing=4, margin_start=4)
            box.append(Gtk.Image(pixel_size=ICON_PX))
            box.append(lbl)
            li.set_child(box)

        def bind(_f, li):
            text = getattr(li.get_item(), prop)
            child = li.get_child()
            lbl = child.get_last_child() if icon else child
            if lbl.get_label() != text:
                lbl.set_label(text)
            if icon:
                self.icon_cells.add(li)
                self.bind_icon(li)
            if live:
                li.prop = prop
                self.live.add(li)

        def unbind(_f, li):
            if live:
                self.live.discard(li)
            if icon:
                self.icon_cells.discard(li)

        f.connect("setup", setup)
        f.connect("bind", bind)
        f.connect("unbind", unbind)
        col = Gtk.ColumnViewColumn(title=title, factory=f, resizable=True, expand=expand)
        if width:
            col.set_fixed_width(width)
        col.set_sorter(Gtk.CustomSorter.new(lambda a, b, *_: Gtk.Ordering.EQUAL))  # marker only
        self.col_props[title] = prop
        self.view.append_column(col)
        return col

    def bind_icon(self, li):
        img = li.get_child().get_first_child()
        tex = pkg_icon(li.get_item())
        if tex:
            img.set_from_paintable(tex)
        else:
            img.set_from_icon_name("package-x-generic-symbolic")
        img.set_opacity(1.0 if tex else 0.45)

    def load_catalog(self):
        appstream_apps()
        GLib.idle_add(self.icons_ready)

    def icons_ready(self):
        for li in list(self.icon_cells):
            self.bind_icon(li)
        return False

    def refresh_visible(self):
        for li in list(self.live):
            item = li.get_item()
            if item is not None:
                li.get_child().set_label(getattr(item, li.prop))

    def build_menu(self):
        m = Gio.Menu()
        sections = [
            [("Reload package list", "reload"), ("Refresh repo metadata", "metadata")],
            [("Upgrade all", "upgrade_all"),
             ("Upgrade security updates only", "upgrade_security"), ("Remove unneeded (autoremove)", "autoremove"),
             ("Clean caches", "clean")],
            [("Mark selected as user-installed", "mark_user"),
             ("Mark selected as dependency", "mark_auto")],
            [("Transaction history", "history"), ("Repositories", "repos"),
             ("Pinned packages", "pins"), ("Groups and environments", "groups"),
             ("Automatic updates", "auto_updates")],
            [("Install to app menu", "install_launcher"),
             ("Remove from app menu", "remove_launcher")],
        ]
        for sec in sections:
            s = Gio.Menu()
            for label, name in sec:
                s.append(label, f"win.{name}")
            m.append_section(None, s)
        return m

    def install_actions(self):
        def add(name, fn):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_: fn())
            self.add_action(a)

        add("reload", self.reload)
        add("metadata", lambda: self.check_updates(show=False))
        add("upgrade_all", self.upgrade_all)
        add("upgrade_security", self.upgrade_security)
        add("autoremove", self.autoremove)
        add("clean", lambda: self.quiet("Caches cleaned", ["pkexec", "dnf5", "clean", "all"],
                                        reload=False))
        add("mark_user", lambda: self.mark_selected("install", "user-installed"))
        add("mark_auto", lambda: self.mark_selected("remove", "dependency"))
        add("history", lambda: HistoryWindow(self).present())
        add("repos", lambda: RepoWindow(self).present())
        add("pins", lambda: PinsWindow(self).present())
        add("groups", lambda: GroupsWindow(self).present())
        add("auto_updates", lambda: AutoUpdatesWindow(self).present())
        add("install_launcher", lambda: self.launcher(install_launcher))
        add("remove_launcher", lambda: self.launcher(uninstall_launcher))
        mark = Gio.SimpleAction.new("mark", GLib.VariantType.new("s"))
        mark.connect("activate", lambda _a, param: self.mark(param.get_string()))
        self.add_action(mark)

    def upgrade_flow(self, title, extra=(), note=""):
        """Upgrade with a dry run first; offers --allowerasing only if that dry run fails."""
        def build(erase):
            cmd = ["dnf5", "-y", "upgrade", *extra, *(["--allowerasing"] if erase else [])]
            dry = [c for c in cmd if c != "-y"] + ["--assumeno"]
            preview = shlex.join(cmd) + note + "\n\nThe dry run below shows what will change."
            return ["pkexec", *cmd], preview, [dry]

        argv, preview, dry = build(False)
        self.run_cmd(title, argv, preview, reload=True, dry=dry, rebuild=build)

    def upgrade_all(self):
        self.upgrade_flow("Upgrade all")

    def upgrade_security(self):
        self.upgrade_flow("Upgrade security updates", ("--security",),
                          "\n\nUpgrades only to the lowest versions that fix security "
                          "advisories, not necessarily the newest.")

    def toast(self, msg):
        self.overlay.add_toast(Adw.Toast.new(msg))

    def launcher(self, fn):
        try:
            self.toast(fn())
        except Exception as e:
            self.toast(f"Failed: {e}")

    def run_cmd(self, title, argv, preview, reload=False, autorun=False, dry=None,
                rebuild=None):
        RunWindow(self, title, argv, preview,
                  on_done=(lambda rc: self.reload()) if reload else None,
                  autorun=autorun, dry=dry, rebuild=rebuild).present()

    def run_root(self, done, cmds):
        """Short privileged commands, run in the background; `done` is the success toast."""
        self.quiet(done, ["pkexec", "sh", "-c", " && ".join(shlex.join(c) for c in cmds)])

    def quiet(self, done, argv, reload=True):
        """Run a short command without a window: toast on success, Details on failure."""
        self.spin.start()

        def work():
            try:
                r = subprocess.run(argv, capture_output=True, text=True)
                rc, out = r.returncode, r.stdout + r.stderr
            except Exception as e:
                rc, out = -1, str(e)
            GLib.idle_add(self._quiet_done, done, rc, out, reload)

        threading.Thread(target=work, daemon=True).start()

    def _quiet_done(self, done, rc, out, reload):
        if not (self.loading_main or self.checking or self.set_loading):
            self.spin.stop()
        if rc != 0:
            self.toast_details("Action failed", "Output", out)
        else:
            self.toast(done)
            if reload:
                self.reload()
        return False

    def toast_details(self, msg, title, out):
        t = Adw.Toast.new(msg)
        t.set_button_label("Details")
        t.connect("button-clicked", lambda *_: self.show_output(title, out))
        self.overlay.add_toast(t)

    def autoremove(self):
        cmd = ["dnf5", "-y", "autoremove"]
        dry = [c for c in cmd if c != "-y"] + ["--assumeno"]
        self.run_cmd("Remove unneeded packages", ["pkexec", *cmd],
                     shlex.join(cmd) + "\n\nThe dry run below shows what will be removed.",
                     reload=True, dry=[dry])

    def selected(self):
        bs = self.selection.get_selection()
        rows = self.model.rows
        return [rows[bs.get_nth(i)] for i in range(bs.get_size())]

    # ---- loading
    def start(self):
        """Show the cached list immediately, then refresh it in the background."""
        self.gen += 1
        self.loading_main = True
        self.spin.start()
        threading.Thread(target=self._worker, args=(self.gen, True), daemon=True).start()

    def reload(self, clear=True):
        self.gen += 1
        self.loading_main = True
        self.sets.clear()
        self.detail_cache.clear()
        self.spin.start()
        if clear:
            self.clear_queue()
        threading.Thread(target=self._worker, args=(self.gen, False), daemon=True).start()

    def _worker(self, gen, use_cache):
        try:
            cached = read_cache() if use_cache else None
            if cached:
                GLib.idle_add(self._loaded, build_pkgs(cached), gen, False)
            raw = collect_raw()
            if cached is not None and raw == cached:
                GLib.idle_add(self._unchanged, gen)  # nothing new: leave the list alone
                return
            write_cache(raw)
            GLib.idle_add(self._loaded, build_pkgs(raw), gen, True)
        except Exception as e:
            GLib.idle_add(self._load_failed, str(e), gen)

    def _unchanged(self, gen):
        if gen == self.gen:
            self.loading_main = False
            self.spin.stop()
            self._run_after_load()
        return False

    def check_reboot(self):
        if self.reboot_checking:
            return
        self.reboot_checking = True

        def work():
            st = reboot_status()
            GLib.idle_add(self._reboot_checked, st)

        threading.Thread(target=work, daemon=True).start()

    def _reboot_checked(self, st):
        self.reboot_checking = False
        if st and st[0]:
            names = ", ".join(st[1][:3]) + (" and more" if len(st[1]) > 3 else "")
            msg = "Reboot recommended to finish updates" + (f" ({names})" if names else "")
            self.banner.set_title(GLib.markup_escape_text(msg))
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)
        return False

    def confirm_reboot(self):
        dlg = Adw.AlertDialog(heading="Reboot now?",
                              body="Save your work first. The system restarts immediately.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("reboot", "Reboot")
        dlg.set_response_appearance("reboot", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda _d, r: r == "reboot"
                    and self.quiet("Rebooting…", ["systemctl", "reboot"], reload=False))
        dlg.present(self)

    def _run_after_load(self):
        self.check_reboot()
        cb, self.after_load = self.after_load, None
        if cb:
            cb()

    def check_updates(self, show=True):
        """Refresh the user metadata cache quietly (no password needed), then reload."""
        if self.checking:
            return
        self.checking = True
        self.check_btn.set_sensitive(False)
        self.spin.start()

        def work():
            r = subprocess.run(["dnf5", "makecache", "--refresh"], capture_output=True, text=True)
            GLib.idle_add(self._checked, r.returncode, r.stdout + r.stderr, show)

        threading.Thread(target=work, daemon=True).start()

    def _checked(self, rc, out, show):
        self.checking = False
        self.check_btn.set_sensitive(True)
        if rc != 0:
            self.spin.stop()
            self.toast_details("Could not check for updates", "Check for updates", out)
            return False
        self.after_load = self._show_updates if show else self._refreshed
        self.reload()
        return False

    def show_output(self, title, text):
        w = RunWindow(self, title, [], text.strip() or "(no output)")
        w.run_btn.set_visible(False)
        w.present()

    def _refreshed(self):
        self.toast("Package list refreshed")

    def _show_updates(self):
        self.mode.set_selected(M_UPGRADABLE)
        self.refilter()
        self.toast(f"{self.n_upg} updates available" if self.n_upg else "System is up to date")

    def _loaded(self, objs, gen, final):
        if gen != self.gen:
            return False
        old = {(p.name, p.arch): p.actions for p in self.queue}
        self.all = objs
        self.n_upg = sum(1 for p in objs if p.upgradable)
        self.queue = set()
        if old:  # keep queued actions across the cache -> fresh data swap
            for p in objs:
                acts = old.get((p.name, p.arch))
                if acts:
                    p.actions = {a for a in acts if action_valid(p, a)}
                    if p.actions:
                        self.queue.add(p)
                        p.refresh()
            self.update_apply()
        self.sort_all()
        self.refilter()
        if final:
            self.loading_main = False
            self.spin.stop()
            self._run_after_load()
        return False

    def _load_failed(self, msg, gen=None):
        if gen is not None and gen != self.gen:
            return False
        self.loading_main = False
        self.after_load = None
        self.spin.stop()
        self.toast(f"Failed to load packages: {msg}")
        return False

    # ---- sorting / filtering (plain Python on plain lists)
    def on_sort_changed(self, sorter, _change):
        col = sorter.get_primary_sort_column()
        if col is None:
            return
        self.sort_prop = self.col_props[col.get_title()]
        self.sort_desc = sorter.get_primary_sort_order() == Gtk.SortType.DESCENDING
        self.sort_all()
        self.refilter()

    def sort_all(self):
        get = operator.attrgetter(self.sort_prop)
        self.all.sort(key=lambda p: get(p).casefold(), reverse=self.sort_desc)

    def refilter(self):
        mode = self.mode.get_selected()
        rows = self.all
        if mode == 1:
            rows = [p for p in rows if p.installed]
        elif mode == 2:
            rows = [p for p in rows if not p.installed]
        elif mode == M_UPGRADABLE:
            rows = [p for p in rows if p.upgradable]
        elif mode == M_QUEUED:
            rows = [p for p in rows if p in self.queue]
        elif mode in SET_QUERIES:
            keys = self.sets.get(mode)
            if keys is None:
                self.fetch_set(mode)
                rows = []
            else:
                rows = [p for p in rows if p.installed and (p.name, p.arch) in keys]
        q = self.query
        if q:
            rows = self.search_rows(rows, q)
        elif rows is self.all:
            rows = list(rows)
        self.model.set_rows(rows)
        loading = mode in SET_QUERIES and mode not in self.sets
        text = f"{len(rows)} packages" + (f" \u00b7 {self.n_upg} upgradable" if self.n_upg else "")
        self.count.set_text(("Checking security advisories\u2026" if mode == M_SECURITY
                             else "Loading\u2026") if loading else text)

    def on_search_mode(self, act, value):
        act.set_state(value)
        self.search_idx = int(value.get_string())
        self.search.set_placeholder_text(f"Search \u00b7 {SEARCH_MODES[self.search_idx]}")
        self.refilter()

    def search_rows(self, rows, q):
        how = self.search_idx
        if how == 1:
            return [p for p in rows if q in p.name.lower()]
        if how == 2:
            word = re.compile(r"(?<![a-z0-9])" + re.escape(q))
            return [p for p in rows if word.search(p.hay)]
        if how == 3:
            return [p for p in rows if p.name.lower() == q]
        return [p for p in rows if q in p.hay]

    def fetch_set(self, mode):
        if mode in self.set_loading:
            return
        self.set_loading.add(mode)
        self.spin.start()
        gen = self.gen
        flags, cache = SET_QUERIES[mode]

        def work():
            try:
                rows = query(*flags, cache=cache)
            except Exception:
                rows = []
            GLib.idle_add(self._set_done, mode, {(r[0], r[2]) for r in rows}, gen)

        threading.Thread(target=work, daemon=True).start()

    def _set_done(self, mode, keys, gen):
        self.set_loading.discard(mode)
        if not self.loading_main and not self.set_loading:
            self.spin.stop()
        if gen == self.gen:
            self.sets[mode] = keys
            if not keys and mode == M_SECURITY:
                self.toast("No security updates found (or advisory metadata unavailable)")
        if self.mode.get_selected() in SET_QUERIES:
            self.refilter()  # also re-fetches if a reload made this result stale
        return False

    def on_search(self, entry):
        self.query = entry.get_text().strip().lower()
        self.refilter()

    # ---- details pane
    def available_actions(self, sel):
        return [a for a in ACTIONS
                if any(action_valid(p, a) or a in p.actions for p in sel)]

    def update_actions(self, sel=None):
        if sel is None:
            sel = self.selected()
        valid = self.available_actions(sel)
        for act, btn in self.action_btns.items():
            btn.set_visible(act in valid)
        self.hint.set_visible(not sel)

    def context_menu(self, li, widget, x, y):
        pos = li.get_position()
        if not self.selection.is_selected(pos):
            self.selection.select_item(pos, True)
        menu = Gio.Menu()
        for act in self.available_actions(self.selected()):
            item = Gio.MenuItem.new(ACTIONS[act][0], None)
            item.set_action_and_target_value("win.mark", GLib.Variant.new_string(act))
            menu.append_item(item)
        if menu.get_n_items() == 0:
            return
        pop = Gtk.PopoverMenu.new_from_model(menu)
        pop.set_parent(widget)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        pop.set_pointing_to(rect)
        pop.set_has_arrow(False)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    def on_select(self, *_):
        sel = self.selected()
        self.update_actions(sel)
        self.info_token += 1
        if self.info_timer:
            GLib.source_remove(self.info_timer)
            self.info_timer = 0
        self.cur = sel[0] if sel else None
        if not sel:
            self.shot_message("Select a package to see its screenshot.")
            for tv in self.tab_views:
                tv.get_buffer().set_text("Select a package to see its details.")
            return
        # Debounce: holding an arrow key would otherwise spawn one dnf5 process per row.
        self.info_timer = GLib.timeout_add(250, self._start_info, self.cur, self.info_token)

    def _start_info(self, p, token):
        self.info_timer = 0
        self.load_tab(p, token, self.notebook.get_current_page())
        return False

    def on_tab_switch(self, _nb, _page, num):
        if self.cur is not None:
            self.info_token += 1
            self.load_tab(self.cur, self.info_token, num)

    def load_tab(self, p, token, idx):
        if idx == SHOT_TAB:
            self.load_screenshot(p, token)
            return
        key = (p.spec, p.installed, idx)
        buf = self.tab_views[idx].get_buffer()
        if key in self.detail_cache:
            self.show_text(idx, p, self.detail_cache[key])
            return
        buf.set_text("Loading\u2026")

        def work():
            try:
                text = detail_text(p, idx)
            except Exception as e:
                text = f"Error: {e}"
            GLib.idle_add(self.set_detail, token, idx, key, text)

        threading.Thread(target=work, daemon=True).start()

    def set_detail(self, token, idx, key, text):
        if len(self.detail_cache) > 400:
            self.detail_cache.clear()
        self.detail_cache[key] = text
        if token == self.info_token and self.cur is not None:
            self.show_text(idx, self.cur, text)
        return False

    def build_shot_tab(self):
        self.shot_label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, css_classes=["dim-label"])
        note = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        note.append(self.shot_label)
        self.shot_strip = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=12,
                                  margin_end=12)
        self.shot_stack = Gtk.Stack()
        self.shot_stack.add_named(note, "note")
        self.shot_stack.add_named(Gtk.ScrolledWindow(vexpand=True, min_content_height=170,
                                                     vscrollbar_policy=Gtk.PolicyType.NEVER,
                                                     child=self.shot_strip), "strip")
        return self.shot_stack

    def shot_message(self, text):
        self.shot_label.set_label(text)
        self.shot_stack.set_visible_child_name("note")

    def load_screenshot(self, p, token):
        self.shot_message("Loading\u2026")

        def work():
            appstream_apps()
            GLib.idle_add(self.show_shots, token, appstream_screenshots(p.name))

        threading.Thread(target=work, daemon=True).start()

    def show_shots(self, token, shots):
        if token != self.info_token:
            return False
        if not shots:
            self.shot_message("No screenshot is listed for this package.")
            return False
        while child := self.shot_strip.get_first_child():
            self.shot_strip.remove(child)
        slots = []
        for caption, url in shots:
            slot = Gtk.Button(halign=Gtk.Align.START, valign=Gtk.Align.START, css_classes=["flat"],
                              tooltip_text=caption or None)
            slot.set_child(Gtk.Spinner(spinning=True, width_request=SHOT_HEIGHT * 16 // 9,
                                       height_request=SHOT_HEIGHT))
            self.shot_strip.append(slot)
            slots.append((slot, caption, url))
        self.shot_stack.set_visible_child_name("strip")
        threading.Thread(target=self._shot_work, args=(token, slots), daemon=True).start()
        return False

    def _shot_work(self, token, slots):
        for slot, caption, url in slots:
            if token != self.info_token:
                return
            GLib.idle_add(self.shot_ready, token, slot, caption, fetch_screenshot(url))

    def shot_ready(self, token, slot, caption, path):
        if token != self.info_token:
            return False
        tex = texture_from(path) if path else None
        if tex is None:
            slot.set_child(Gtk.Image(icon_name="image-missing-symbolic", pixel_size=48,
                                     width_request=SHOT_HEIGHT * 16 // 9, height_request=SHOT_HEIGHT,
                                     tooltip_text="Could not load this screenshot"))
            slot.set_sensitive(False)
            return False
        pic = Gtk.Picture(paintable=tex, content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        pic.set_size_request(max(1, round(tex.get_width() * SHOT_HEIGHT / tex.get_height())), SHOT_HEIGHT)
        slot.set_child(pic)
        slot.connect("clicked", lambda *_: self.open_shot(tex, caption))
        return False

    def open_shot(self, tex, caption):
        name = self.cur.name if self.cur else "Screenshot"
        dlg = Adw.Dialog(title=f"{name}: {caption}" if caption else name,
                         content_width=min(tex.get_width(), 1100),
                         content_height=min(tex.get_height() + 48, 800))
        body = Adw.ToolbarView()
        body.add_top_bar(Adw.HeaderBar())
        body.set_content(Gtk.Picture(paintable=tex, content_fit=Gtk.ContentFit.CONTAIN,
                                     can_shrink=True))
        dlg.set_child(body)
        dlg.present(self)

    def show_text(self, idx, p, text):
        buf = self.tab_views[idx].get_buffer()
        buf.set_text(text)
        icon = appstream_icon(p.name) if idx == 0 else ""
        if icon:
            try:
                buf.insert_paintable(buf.get_start_iter(), Gdk.Texture.new_from_filename(icon))
                buf.insert(buf.get_iter_at_offset(1), "\n")
            except GLib.Error:
                pass

    # ---- queue
    def mark(self, action):
        valid = lambda p: action_valid(p, action)
        group = CHANGE_ACTIONS if action in CHANGE_ACTIONS else {"pin", "unpin", "block"}
        for p in self.selected():
            if action in p.actions:
                p.actions.discard(action)
            elif valid(p):
                p.actions -= group
                p.actions.add(action)
            if p.actions:
                self.queue.add(p)
            else:
                self.queue.discard(p)
            p.refresh()
        self.refresh_visible()
        self.update_apply()
        if self.mode.get_selected() == M_QUEUED:
            self.refilter()

    def clear_queue(self):
        for p in list(self.queue):
            p.actions.clear()
            p.refresh()
        self.queue = set()
        self.refresh_visible()
        self.update_apply()
        if self.mode.get_selected() == M_QUEUED:
            self.refilter()

    def update_apply(self):
        n = len(self.queue)
        self.apply_btn.set_label(f"Apply ({n})" if n else "Apply")
        self.apply_btn.set_sensitive(n > 0)
        self.clear_btn.set_visible(n > 0)
        self.update_actions()

    def plan(self, erase=False):
        by = lambda a: [p for p in self.queue if a in p.actions]
        flag = ["--allowerasing"] if erase else []
        cmds = []
        if by("unpin"):
            cmds.append(["dnf5", "versionlock", "delete", *[p.name for p in by("unpin")]])
        if by("remove"):
            cmds.append(["dnf5", "-y", "remove", *[p.spec for p in by("remove")]])
        if by("install"):
            cmds.append(["dnf5", "-y", "install", *flag, *[p.spec for p in by("install")]])
        if by("upgrade"):
            cmds.append(["dnf5", "-y", "upgrade", *flag, *[p.spec for p in by("upgrade")]])
        if by("reinstall"):
            cmds.append(["dnf5", "-y", "reinstall", *[p.spec for p in by("reinstall")]])
        if by("downgrade"):
            cmds.append(["dnf5", "-y", "downgrade", *[p.spec for p in by("downgrade")]])
        if by("pin"):
            cmds.append(["dnf5", "versionlock", "add", *[p.name for p in by("pin")]])
        if by("block"):
            cmds.append(["dnf5", "versionlock", "exclude", *[p.name for p in by("block")]])
        return cmds

    def apply(self):
        def build(erase):
            cmds = self.plan(erase)
            script = " && ".join(shlex.join(c) for c in cmds)
            preview = "Planned commands (run in order, as root):\n\n" + \
                      "\n".join("  " + shlex.join(c) for c in cmds) + \
                      "\n\nPress Run to execute."
            dry = [[c for c in cmd if c != "-y"] + ["--assumeno"] for cmd in cmds if "-y" in cmd]
            return ["pkexec", "sh", "-c", script], preview, dry

        cmds = self.plan()
        if not cmds:
            return
        argv, preview, dry = build(False)
        # --allowerasing only exists on install/upgrade here, so only offer a retry for those
        can_erase = any(c[2] in ("install", "upgrade") for c in cmds)
        self.run_cmd("Apply changes", argv, preview, reload=True, dry=dry or None,
                     rebuild=build if can_erase else None)

    def mark_selected(self, kind, label):
        names = [p.spec for p in self.selected() if p.installed]
        if not names:
            self.toast("Select installed packages first")
            return
        self.quiet(f"Marked {len(names)} package(s) as {label}",
                   ["pkexec", "dnf5", "mark", kind, *names], reload=False)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        act = Gio.SimpleAction.new("open", None)
        act.connect("activate", lambda *_: self.activate())
        self.add_action(act)
        act = Gio.SimpleAction.new("history", None)
        act.connect("activate", lambda *_: self.show_history())
        self.add_action(act)

    def show_history(self):
        win = next((w for w in self.get_windows() if isinstance(w, Win)), None)
        if not win:
            self.activate()
            win = next(w for w in self.get_windows() if isinstance(w, Win))
        HistoryWindow(win, auto_only=True).present()

    def do_activate(self):
        win = Win(self)
        win.present()
        win.start()


if __name__ == "__main__":
    if "--install" in sys.argv[1:]:
        print(install_launcher())
    elif "--uninstall" in sys.argv[1:]:
        print(uninstall_launcher())
    elif "--notify" in sys.argv[1:]:
        sys.exit(notify_report())
    elif "--can-update" in sys.argv[1:]:
        why = update_blocker()
        if why:
            print(f"Automatic update skipped: {why}", file=sys.stderr)
        sys.exit(3 if why else 0)
    else:
        App().run(None)
