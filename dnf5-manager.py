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
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import tomllib

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

APP_ID = "org.example.Dnf5Manager"
VERSIONLOCK_FILE = "/etc/dnf/versionlock.toml"
QF = r"%{name}|%{evr}|%{arch}|%{repoid}|%{summary}\n"
MODES = ["All", "Installed", "Not installed", "Upgradable", "Pinned", "Queued changes"]


# ---------------------------------------------------------------- data layer
def run(argv):
    return subprocess.run(argv, capture_output=True, text=True).stdout


def query(*flags):
    out = run(["dnf5", "repoquery", *flags, "--queryformat", QF])
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


def collect_packages():
    pk = {}
    for n, e, a, r, s in query("--available", "--latest-limit=1"):
        pk[(n, a)] = dict(name=n, arch=a, avail=e, inst="", repo=r, summary=s, upg=False)
    for n, e, a, r, s in query("--installed"):
        d = pk.setdefault((n, a), dict(name=n, arch=a, avail="", repo="(local)", summary=s, upg=False))
        d["inst"] = e
    for n, e, a, r, s in query("--upgrades", "--latest-limit=1"):
        if (n, a) in pk:
            pk[(n, a)]["avail"] = e
            pk[(n, a)]["upg"] = True
    pins = load_pins()
    return [Pkg(d, d["name"] in pins) for d in pk.values()]


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
    "pin": ("Pin version", "Lock the installed version (dnf5 versionlock add)"),
    "unpin": ("Unpin", "Remove the lock or block (dnf5 versionlock delete)"),
    "block": ("Block", "Exclude the versions currently available (dnf5 versionlock exclude)"),
}


def action_valid(p, action):
    return {
        "install": not p.installed,
        "remove": p.installed,
        "upgrade": p.installed and p.upgradable,
        "pin": p.installed and not p.pinned,
        "unpin": p.pinned,
        "block": not p.pinned,
    }[action]


class Pkg(GObject.Object):
    __gtype_name__ = "Dnf5Pkg"
    name = GObject.Property(type=str, default="")
    version = GObject.Property(type=str, default="")
    latest = GObject.Property(type=str, default="")
    repo = GObject.Property(type=str, default="")
    summary = GObject.Property(type=str, default="")
    status = GObject.Property(type=str, default="")
    queued = GObject.Property(type=str, default="")

    def __init__(self, d, pinned):
        super().__init__()
        self.arch = d["arch"]
        self.inst = d.get("inst", "")
        self.installed = bool(self.inst)
        self.upgradable = d["upg"]
        self.pinned = pinned
        self.actions = set()
        self.name = d["name"]
        self.version = self.inst or d["avail"]
        self.latest = d["avail"]
        self.repo = d["repo"]
        self.summary = d["summary"]
        self.hay = f"{self.name} {self.summary}".lower()
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


# ---------------------------------------------------------------- run window
class RunWindow(Adw.Window):
    """Shows what will run, then streams output. Run button executes it."""

    def __init__(self, parent, title, argv, preview, on_done=None, autorun=False, dry=None):
        super().__init__(transient_for=parent, modal=True, title=title,
                         default_width=800, default_height=500)
        self.argv, self.on_done = argv, on_done
        self.tv = Gtk.TextView(editable=False, monospace=True, left_margin=8,
                               top_margin=8, bottom_margin=8, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.buf = self.tv.get_buffer()
        self.buf.set_text(preview + "\n")
        hb = Adw.HeaderBar()
        self.close_btn = Gtk.Button(label="Close")
        self.close_btn.connect("clicked", lambda *_: self.close())
        self.run_btn = Gtk.Button(label="Run", css_classes=["suggested-action"])
        self.run_btn.connect("clicked", lambda *_: self.start())
        hb.pack_start(self.close_btn)
        hb.pack_end(self.run_btn)
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
        warn = ""
        if "Removing dependent packages" in text or "Removing unused dependencies" in text:
            warn = "WARNING: this transaction also removes other packages. Review carefully.\n\n"
        self.append("\n" + warn + text + "\n--- dry run finished; press Run to apply ---\n")
        self.run_btn.set_sensitive(True)
        return False

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
        self.store = Gio.ListStore.new(Pkg)
        self.queue = set()
        self.query = ""
        self.info_token = 0

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

        self.filter = Gtk.CustomFilter.new(self.match)
        self.filtered = Gtk.FilterListModel(model=self.store, filter=self.filter)
        self.filtered.connect("items-changed", self.on_count)
        sorted_model = Gtk.SortListModel(model=self.filtered, sorter=self.view.get_sorter())
        self.selection = Gtk.MultiSelection(model=sorted_model)
        self.selection.connect("selection-changed", self.on_select)
        self.view.set_model(self.selection)
        self.view.sort_by_column(name_col, Gtk.SortType.ASCENDING)

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

        self.info = Gtk.TextView(editable=False, monospace=True, left_margin=8, top_margin=8,
                                 wrap_mode=Gtk.WrapMode.WORD_CHAR)
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bottom.append(bar)
        bottom.append(Gtk.ScrolledWindow(vexpand=True, min_content_height=170, child=self.info))

        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, position=480,
                          start_child=Gtk.ScrolledWindow(vexpand=True, child=self.view),
                          end_child=bottom, shrink_start_child=False, shrink_end_child=False)

        tb = Adw.ToolbarView()
        tb.add_top_bar(hb)
        tb.set_content(paned)
        self.overlay = Adw.ToastOverlay(child=tb)
        self.set_content(self.overlay)
        self.install_actions()

    # ---- UI helpers
    def add_col(self, title, prop, width=None, expand=False):
        f = Gtk.SignalListItemFactory()

        def setup(_f, li):
            lbl = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                            margin_start=6, margin_end=6)
            click = Gtk.GestureClick(button=3)
            click.connect("pressed", lambda g, n, x, y, li=li, w=lbl: self.context_menu(li, w, x, y))
            lbl.add_controller(click)
            li.set_child(lbl)

        def bind(_f, li):
            li.binding = li.get_item().bind_property(
                prop, li.get_child(), "label", GObject.BindingFlags.SYNC_CREATE)

        def unbind(_f, li):
            li.binding.unbind()

        f.connect("setup", setup)
        f.connect("bind", bind)
        f.connect("unbind", unbind)
        col = Gtk.ColumnViewColumn(title=title, factory=f, resizable=True, expand=expand)
        if width:
            col.set_fixed_width(width)
        col.set_sorter(Gtk.StringSorter(expression=Gtk.PropertyExpression.new(Pkg, None, prop)))
        self.view.append_column(col)
        return col

    def build_menu(self):
        m = Gio.Menu()
        sections = [
            [("Reload package list", "reload"), ("Refresh repo metadata", "metadata")],
            [("Upgrade all", "upgrade_all"), ("Remove unneeded (autoremove)", "autoremove"),
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
        add("upgrade_all", lambda: self.run_cmd(
            "Upgrade all", ["pkexec", "dnf5", "-y", "upgrade"], "dnf5 -y upgrade", reload=True))
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

    def toast(self, msg):
        self.overlay.add_toast(Adw.Toast.new(msg))

    def launcher(self, fn):
        try:
            self.toast(fn())
        except Exception as e:
            self.toast(f"Failed: {e}")

    def run_cmd(self, title, argv, preview, reload=False, autorun=False, dry=None):
        RunWindow(self, title, argv, preview,
                  on_done=(lambda rc: self.reload()) if reload else None,
                  autorun=autorun, dry=dry).present()

    def run_root(self, title, cmds, note=""):
        script = " && ".join(shlex.join(c) for c in cmds)
        preview = "Commands (run as root):\n\n" + "\n".join("  " + shlex.join(c) for c in cmds)
        if note:
            preview += "\n\n" + note
        self.run_cmd(title, ["pkexec", "sh", "-c", script],
                     preview + "\n\nPress Run to execute.", reload=True)

    def selected(self):
        bs = self.selection.get_selection()
        return [self.selection.get_item(bs.get_nth(i)) for i in range(bs.get_size())]

    # ---- loading
    def reload(self):
        self.spin.start()
        self.clear_queue()
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        try:
            objs = collect_packages()
            GLib.idle_add(self._loaded, objs)
        except Exception as e:
            GLib.idle_add(self._load_failed, str(e))

    def _loaded(self, objs):
        self.store.splice(0, self.store.get_n_items(), objs)
        self.spin.stop()
        return False

    def _load_failed(self, msg):
        self.spin.stop()
        self.toast(f"Failed to load packages: {msg}")
        return False

    # ---- filtering
    def match(self, p):
        m = self.mode.get_selected()
        if m == 1 and not p.installed:
            return False
        if m == 2 and p.installed:
            return False
        if m == 3 and not p.upgradable:
            return False
        if m == 4 and not p.pinned:
            return False
        if m == 5 and p not in self.queue:
            return False
        return not self.query or self.query in p.hay

    def refilter(self):
        self.filter.changed(Gtk.FilterChange.DIFFERENT)

    def on_search(self, entry):
        self.query = entry.get_text().strip().lower()
        self.refilter()

    def on_count(self, model, *_):
        self.count.set_text(f"{model.get_n_items()} packages")

    # ---- details pane
    def available_actions(self, sel):
        return [a for a in ACTIONS
                if any(action_valid(p, a) or a in p.actions for p in sel)]

    def update_actions(self):
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
        self.update_actions()
        sel = self.selected()
        self.info_token += 1
        if not sel:
            self.info.get_buffer().set_text("Select a package to see its details.")
            return
        p = sel[0]
        token = self.info_token

        def work():
            out = run(["dnf5", "info", p.spec])
            GLib.idle_add(self.set_info, token, out)

        threading.Thread(target=work, daemon=True).start()

    def set_info(self, token, text):
        if token == self.info_token:
            self.info.get_buffer().set_text(text)
        return False

    # ---- queue
    def mark(self, action):
        valid = lambda p: action_valid(p, action)
        group = {"install", "remove", "upgrade"} if action in ("install", "remove", "upgrade") \
            else {"pin", "unpin", "block"}
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
        self.update_apply()
        if self.mode.get_selected() == 5:
            self.refilter()

    def clear_queue(self):
        for p in list(self.queue):
            p.actions.clear()
            p.refresh()
        self.queue = set()
        self.update_apply()
        if self.mode.get_selected() == 5:
            self.refilter()

    def update_apply(self):
        n = len(self.queue)
        self.apply_btn.set_label(f"Apply ({n})" if n else "Apply")
        self.apply_btn.set_sensitive(n > 0)
        self.clear_btn.set_visible(n > 0)
        self.update_actions()

    def apply(self):
        by = lambda a: [p for p in self.queue if a in p.actions]
        cmds = []
        if by("unpin"):
            cmds.append(["dnf5", "versionlock", "delete", *[p.name for p in by("unpin")]])
        if by("remove"):
            cmds.append(["dnf5", "-y", "remove", *[p.spec for p in by("remove")]])
        if by("install"):
            cmds.append(["dnf5", "-y", "install", *[p.spec for p in by("install")]])
        if by("upgrade"):
            cmds.append(["dnf5", "-y", "upgrade", *[p.spec for p in by("upgrade")]])
        if by("pin"):
            cmds.append(["dnf5", "versionlock", "add", *[p.name for p in by("pin")]])
        if by("block"):
            cmds.append(["dnf5", "versionlock", "exclude", *[p.name for p in by("block")]])
        if not cmds:
            return
        script = " && ".join(shlex.join(c) for c in cmds)
        preview = "Planned commands (run in order, as root):\n\n" + \
                  "\n".join("  " + shlex.join(c) for c in cmds) + \
                  "\n\nPress Run to execute."
        dry = [[c for c in cmd if c != "-y"] + ["--assumeno"] for cmd in cmds if "-y" in cmd]
        self.run_cmd("Apply changes", ["pkexec", "sh", "-c", script], preview,
                     reload=True, dry=dry or None)

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
        win.reload()


if __name__ == "__main__":
    if "--install" in sys.argv[1:]:
        print(install_launcher())
    elif "--uninstall" in sys.argv[1:]:
        print(uninstall_launcher())
    else:
        App().run(None)
