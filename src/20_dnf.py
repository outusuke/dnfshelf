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
