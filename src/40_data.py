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
        return dnf5("info", p.spec) or "No information available."
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
