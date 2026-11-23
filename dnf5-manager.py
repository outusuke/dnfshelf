#!/usr/bin/env python3
"""
DNF5 Package Manager: a Synaptic-style front end for Fedora (GTK4 + libadwaita).

Dependencies:
    sudo dnf install python3-gobject gtk4 libadwaita dnf5 dnf5-plugins polkit

Run:
    python3 dnf5-manager.py

Everything goes through the dnf5 CLI. Privileged operations use pkexec, so you
get one polkit prompt per transaction. Pins use `dnf5 versionlock`
(state in /etc/dnf/versionlock.toml).
"""
import os
import shlex
import subprocess
import threading
import tomllib

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

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
        for label, act in [("Install", "install"), ("Remove", "remove"),
                           ("Upgrade", "upgrade"), ("Pin version", "pin"), ("Unpin", "unpin")]:
            b = Gtk.Button(label=label)
            b.connect("clicked", lambda _b, a=act: self.mark(a))
            bar.append(b)
        clear = Gtk.Button(label="Clear queue")
        clear.connect("clicked", lambda *_: self.clear_queue())
        bar.append(clear)
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
            li.set_child(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                   margin_start=6, margin_end=6))

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
             ("Show all pins", "pins")],
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
        add("history", lambda: self.run_cmd(
            "History", ["dnf5", "history", "list"], "dnf5 history list", autorun=True))
        add("repos", lambda: self.run_cmd(
            "Repositories", ["dnf5", "repo", "list", "--all"], "dnf5 repo list --all", autorun=True))
        add("pins", lambda: self.run_cmd(
            "Pins", ["dnf5", "versionlock", "list"], "dnf5 versionlock list", autorun=True))

    def toast(self, msg):
        self.overlay.add_toast(Adw.Toast.new(msg))

    def run_cmd(self, title, argv, preview, reload=False, autorun=False, dry=None):
        RunWindow(self, title, argv, preview,
                  on_done=(lambda rc: self.reload()) if reload else None,
                  autorun=autorun, dry=dry).present()

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
    def on_select(self, *_):
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
        valid = lambda p: {
            "install": not p.installed,
            "remove": p.installed,
            "upgrade": p.installed and p.upgradable,
            "pin": p.installed and not p.pinned,
            "unpin": p.pinned,
        }[action]
        group = {"install", "remove", "upgrade"} if action in ("install", "remove", "upgrade") \
            else {"pin", "unpin"}
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


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        win = Win(self)
        win.present()
        win.reload()


if __name__ == "__main__":
    App().run(None)
