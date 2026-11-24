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
