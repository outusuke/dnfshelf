#!/usr/bin/env python3
"""
DNF5 Package Manager: a Synaptic-style front end for Fedora (GTK4 + libadwaita).

Dependencies:
    sudo dnf install python3-gobject gtk4 libadwaita dnf5 dnf5-plugins polkit

Run:
    python3 dnf5-manager.py

Install to ~/.local/bin and the app menu (or use the menu entry inside the app):
    python3 dnf5-manager.py --install
    python3 dnf5-manager.py --uninstall

Everything goes through the dnf5 CLI. Privileged operations use pkexec, so you
get one polkit prompt per transaction. Pins use `dnf5 versionlock`
(state in /etc/dnf/versionlock.toml).
"""
import json
import operator
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

APP_ID = "org.example.Dnf5Manager"
VERSIONLOCK_FILE = "/etc/dnf/versionlock.toml"
QF = r"%{name}|%{evr}|%{arch}|%{repoid}|%{summary}\n"
# List packages from the local metadata cache (no network round trip on every start).
# Falls back to a normal dnf5 run automatically if that fails. Use "Refresh repo
# metadata" in the menu to update the cache.
USE_CACHEONLY = True
CACHE_DIR = os.path.join(GLib.get_user_cache_dir(), "dnf5-manager")
CACHE_FILE = os.path.join(CACHE_DIR, "packages.json")
PKG_QF = r"%{name}\n"
SO_CAP = re.compile(r"^([^()\s]+\.so[^()\s]*)\(.*\)$")
MODES = ["All", "Installed", "Not installed", "Upgradable", "Pinned", "Queued changes",
         "Security updates"]


# ---------------------------------------------------------------- data layer
def run(argv):
    return subprocess.run(argv, capture_output=True, text=True).stdout


def dnf5(*args, cache=True):
    """Read-only dnf5 call, preferring the local metadata cache."""
    if USE_CACHEONLY and cache:
        r = subprocess.run(["dnf5", "--cacheonly", *args], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout
    return run(["dnf5", *args])


def query(*flags, cache=True):
    out = dnf5("repoquery", *flags, "--queryformat", QF, cache=cache)
    rows = []
    for line in out.splitlines():
        parts = line.split("|", 4)
        if len(parts) == 5:
            rows.append(parts)
    return rows


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


def build_pkgs(raw):
    pins = load_pins()
    return [Pkg(d, d[0] in pins) for d in raw]


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


def detail_text(p, idx):
    """Text for one tab of the details pane (0 info, 1 dependencies, 2 files)."""
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

    def __init__(self, parent):
        super().__init__(transient_for=parent, modal=True, title="Transaction history",
                         default_width=760, default_height=600)
        self.parent_win = parent
        self.placeholder = Gtk.Label(label="Loading\u2026", css_classes=["dim-label"],
                                     margin_top=48, margin_bottom=48)
        self.box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                               css_classes=["boxed-list"], margin_top=12, margin_bottom=12,
                               margin_start=12, margin_end=12, valign=Gtk.Align.START)
        self.box.set_placeholder(self.placeholder)
        tb = Adw.ToolbarView()
        tb.add_top_bar(Adw.HeaderBar())
        tb.set_content(Gtk.ScrolledWindow(vexpand=True, child=self.box))
        self.set_content(tb)
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        GLib.idle_add(self._populate, parse_history(run(["dnf5", "history", "list"])))

    def _populate(self, items):
        self.placeholder.set_text("No transactions found")
        for tid, cmd, when, actions in items:
            row = Adw.ActionRow(title=GLib.markup_escape_text(cmd or "(no command line)"),
                                subtitle=GLib.markup_escape_text(f"#{tid} \u00b7 {when} \u00b7 {actions}"),
                                activatable=True)
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
        RunWindow(self, f"Transaction {tid}", ["dnf5", "history", "info", str(tid)],
                  f"dnf5 history info {tid}", autorun=True).present()

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
        self.parent_win.run_root("Remove pin", [["dnf5", "versionlock", "delete", name]])

    def remove_all(self):
        self.close()
        self.parent_win.run_root("Remove all pins", [["dnf5", "versionlock", "clear"]])


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
            self.parent_win.run_root("Change repositories", cmds)

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
        super().__init__(application=app, title="DNF5 Package Manager",
                         default_width=1250, default_height=780)
        self.model = PkgModel()
        self.all = []          # every package, kept sorted
        self.queue = set()
        self.query = ""
        self.info_token = 0
        self.info_timer = 0
        self.gen = 0           # load generation, so stale loads are ignored
        self.live = set()      # currently bound Status/Queued cells
        self.sort_prop, self.sort_desc = "name", False
        self.col_props = {}
        self.cur = None        # package shown in the details pane
        self.detail_cache = {}
        self.security = None   # {(name, arch)} of packages with security upgrades, lazy
        self.security_loading = False
        self.loading_main = False

        # header bar
        hb = Adw.HeaderBar()
        self.mode = Gtk.DropDown.new_from_strings(MODES)
        self.mode.connect("notify::selected", lambda *_: self.refilter())
        hb.pack_start(self.mode)
        self.spin = Gtk.Spinner()
        hb.pack_start(self.spin)
        self.search = Gtk.SearchEntry(placeholder_text="Search name or description",
                                      width_request=420)
        self.search.connect("search-changed", self.on_search)
        hb.set_title_widget(self.search)
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
        name_col = self.add_col("Name", "name", width=260)
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
        for title in ("Info", "Dependencies", "Files"):
            tv = Gtk.TextView(editable=False, monospace=True, left_margin=8, top_margin=8,
                              wrap_mode=Gtk.WrapMode.WORD_CHAR)
            self.tab_views.append(tv)
            self.notebook.append_page(
                Gtk.ScrolledWindow(vexpand=True, min_content_height=170, child=tv),
                Gtk.Label(label=title))
        self.notebook.connect("switch-page", self.on_tab_switch)
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bottom.append(bar)
        bottom.append(self.notebook)

        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, position=480,
                          start_child=Gtk.ScrolledWindow(vexpand=True, child=self.view),
                          end_child=bottom, shrink_start_child=False, shrink_end_child=False)

        tb = Adw.ToolbarView()
        tb.add_top_bar(hb)
        tb.set_content(paned)
        self.overlay = Adw.ToastOverlay(child=tb)
        self.set_content(self.overlay)
        self.install_actions()
        self.view.sort_by_column(name_col, Gtk.SortType.ASCENDING)  # last: needs self.count

    # ---- UI helpers
    def add_col(self, title, prop, width=None, expand=False):
        f = Gtk.SignalListItemFactory()
        live = prop in ("status", "queued")  # the only cells that change after load

        def setup(_f, li):
            lbl = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                            single_line_mode=True, max_width_chars=1,
                            margin_start=6, margin_end=6)
            click = Gtk.GestureClick(button=3)
            click.connect("pressed", lambda g, n, x, y, li=li, w=lbl: self.context_menu(li, w, x, y))
            lbl.add_controller(click)
            li.set_child(lbl)

        def bind(_f, li):
            text = getattr(li.get_item(), prop)
            lbl = li.get_child()
            if lbl.get_label() != text:
                lbl.set_label(text)
            if live:
                li.prop = prop
                self.live.add(li)

        def unbind(_f, li):
            if live:
                self.live.discard(li)

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
             ("Pinned packages", "pins")],
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
        add("metadata", lambda: self.run_cmd(
            "Refresh metadata", ["pkexec", "sh", "-c", "dnf5 clean metadata && dnf5 makecache"],
            "Refresh repository metadata", reload=True))
        add("upgrade_all", self.upgrade_all)
        add("upgrade_security", self.upgrade_security)
        add("autoremove", lambda: self.run_cmd(
            "Autoremove", ["pkexec", "dnf5", "-y", "autoremove"], "dnf5 -y autoremove", reload=True))
        add("clean", lambda: self.run_cmd(
            "Clean caches", ["pkexec", "dnf5", "clean", "all"], "dnf5 clean all"))
        add("mark_user", lambda: self.mark_selected("install", "user-installed"))
        add("mark_auto", lambda: self.mark_selected("remove", "dependency"))
        add("history", lambda: HistoryWindow(self).present())
        add("repos", lambda: RepoWindow(self).present())
        add("pins", lambda: PinsWindow(self).present())
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

    def run_root(self, title, cmds, note=""):
        script = " && ".join(shlex.join(c) for c in cmds)
        preview = "Commands (run as root):\n\n" + "\n".join("  " + shlex.join(c) for c in cmds)
        if note:
            preview += "\n\n" + note
        self.run_cmd(title, ["pkexec", "sh", "-c", script],
                     preview + "\n\nPress Run to execute.", reload=True)

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
        self.security = None
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
        return False

    def _loaded(self, objs, gen, final):
        if gen != self.gen:
            return False
        old = {(p.name, p.arch): p.actions for p in self.queue}
        self.all = objs
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
        return False

    def _load_failed(self, msg, gen=None):
        if gen is not None and gen != self.gen:
            return False
        self.loading_main = False
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
        elif mode == 3:
            rows = [p for p in rows if p.upgradable]
        elif mode == 4:
            rows = [p for p in rows if p.pinned]
        elif mode == 5:
            rows = [p for p in rows if p in self.queue]
        elif mode == 6:
            if self.security is None:
                self.fetch_security()
                rows = []
            else:
                rows = [p for p in rows if (p.name, p.arch) in self.security]
        q = self.query
        if q:
            rows = [p for p in rows if q in p.hay]
        elif rows is self.all:
            rows = list(rows)
        self.model.set_rows(rows)
        loading = mode == 6 and self.security is None
        self.count.set_text("Checking security advisories\u2026" if loading
                            else f"{len(rows)} packages")

    def fetch_security(self):
        if self.security_loading:
            return
        self.security_loading = True
        self.spin.start()
        gen = self.gen

        def work():
            try:  # advisory data may be missing from the offline cache, so allow the network
                rows = query("--upgrades", "--security", "--latest-limit=1", cache=False)
            except Exception:
                rows = []
            GLib.idle_add(self._security_done, {(r[0], r[2]) for r in rows}, gen)

        threading.Thread(target=work, daemon=True).start()

    def _security_done(self, keys, gen):
        self.security_loading = False
        if not self.loading_main:
            self.spin.stop()
        if gen == self.gen:
            self.security = keys
            if not keys:
                self.toast("No security updates found (or advisory metadata unavailable)")
        if self.mode.get_selected() == 6:
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
        key = (p.spec, p.installed, idx)
        buf = self.tab_views[idx].get_buffer()
        if key in self.detail_cache:
            buf.set_text(self.detail_cache[key])
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
        if token == self.info_token:
            self.tab_views[idx].get_buffer().set_text(text)
        return False

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
        if self.mode.get_selected() == 5:
            self.refilter()

    def clear_queue(self):
        for p in list(self.queue):
            p.actions.clear()
            p.refresh()
        self.queue = set()
        self.refresh_visible()
        self.update_apply()
        if self.mode.get_selected() == 5:
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
        cmd = ["dnf5", "mark", kind, *names]
        self.run_cmd(f"Mark as {label}", ["pkexec", *cmd], shlex.join(cmd))


BIN_PATH = os.path.expanduser("~/.local/bin/dnf5-manager")
DESKTOP_PATH = os.path.expanduser(f"~/.local/share/applications/{APP_ID}.desktop")
DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=DNF5 Package Manager
Comment=Synaptic-style package manager for Fedora
Exec="{exec}"
Icon=system-software-install
Terminal=false
Categories=System;PackageManager;GTK;
Keywords=dnf;rpm;packages;software;
StartupWMClass={app_id}
"""


def install_launcher():
    """Copy this script to ~/.local/bin and add an app-menu entry."""
    src = os.path.abspath(__file__)
    os.makedirs(os.path.dirname(BIN_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(DESKTOP_PATH), exist_ok=True)
    if not (os.path.exists(BIN_PATH) and os.path.samefile(src, BIN_PATH)):
        shutil.copyfile(src, BIN_PATH)
    os.chmod(BIN_PATH, 0o755)
    with open(DESKTOP_PATH, "w") as f:
        f.write(DESKTOP_ENTRY.format(exec=BIN_PATH, app_id=APP_ID))
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", os.path.dirname(DESKTOP_PATH)],
                       capture_output=True)
    return f"Installed {BIN_PATH} and added to the app menu"


def uninstall_launcher():
    removed = []
    for path in (BIN_PATH, DESKTOP_PATH):
        if os.path.exists(path):
            os.remove(path)
            removed.append(path)
    return "Removed " + ", ".join(removed) if removed else "Nothing to remove"


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        win = Win(self)
        win.present()
        win.start()


if __name__ == "__main__":
    if "--install" in sys.argv[1:]:
        print(install_launcher())
    elif "--uninstall" in sys.argv[1:]:
        print(uninstall_launcher())
    else:
        App().run(None)
