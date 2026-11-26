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


def repo_names(pkgs):
    return sorted({p.repo for p in pkgs if p.repo}, key=str.casefold)


def filter_rows(rows, repo=None):
    return rows if repo is None else [p for p in rows if p.repo == repo]


def build_pkgs(raw):
    pins = load_pins()
    return [Pkg(d, d[0] in pins) for d in raw]
