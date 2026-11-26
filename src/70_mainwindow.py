class Win(DetailsMixin, Adw.ApplicationWindow):
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
        bar = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6,
                      css_classes=["toolbar"])
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

        self.tab_views, self.tab_stacks = [], []
        self.notebook = Gtk.Notebook(vexpand=True)
        for title in ("Info", "Dependencies", "Files", "Changelog"):
            tv = Gtk.TextView(editable=False, monospace=True, left_margin=8, top_margin=8,
                              wrap_mode=Gtk.WrapMode.WORD_CHAR)
            self.tab_views.append(tv)
            stack = Gtk.Stack()
            stack.add_named(Gtk.ScrolledWindow(vexpand=True, min_content_height=170, child=tv), "text")
            self.tab_stacks.append(stack)
            self.notebook.append_page(stack, Gtk.Label(label=title))
        self.info_stack = self.tab_stacks[0]
        self.notebook.append_page(self.build_shot_tab(), Gtk.Label(label="Screenshots"))
        self.notebook.connect("switch-page", self.on_tab_switch)
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bottom.append(bar)
        bottom.append(self.notebook)

        self.empty = Adw.StatusPage(icon_name="edit-find-symbolic")
        self.list_stack = Gtk.Stack(vexpand=True)
        self.list_stack.add_named(Gtk.ScrolledWindow(child=self.view), "list")
        self.list_stack.add_named(self.empty, "empty")
        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, position=480,
                          start_child=self.list_stack,
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
        self.run_cmd(title, argv, preview, reload=True, dry=dry, rebuild=build,
                     note=note.strip())

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
                rebuild=None, note=""):
        RunWindow(self, title, argv, preview,
                  on_done=(lambda rc: self.reload()) if reload else None,
                  autorun=autorun, dry=dry, rebuild=rebuild, note=note).present()

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
        t.connect("button-clicked", lambda *_: self.show_output(msg, out))
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
        text = text.strip() or "(no output)"
        dlg = Adw.AlertDialog(heading=title, body=text.splitlines()[-1][:200])
        tv = Gtk.TextView(editable=False, monospace=True, left_margin=8, right_margin=8,
                          top_margin=8, bottom_margin=8, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        tv.get_buffer().set_text(text)
        dlg.set_extra_child(Gtk.ScrolledWindow(child=tv, min_content_height=200,
                                               min_content_width=420, css_classes=["card"]))
        dlg.add_response("close", "Close")
        dlg.set_close_response("close")
        dlg.present(self)

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
            self.sync_empty()
            self.spin.stop()
            self._run_after_load()
        return False

    def _load_failed(self, msg, gen=None):
        if gen is not None and gen != self.gen:
            return False
        self.loading_main = False
        self.sync_empty()
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
        text = f"{len(rows)} package{'' if len(rows) == 1 else 's'}" + (f" \u00b7 {self.n_upg} upgradable" if self.n_upg else "")
        self.count.set_text(("Checking security advisories\u2026" if mode == M_SECURITY
                             else "Loading\u2026") if loading else text)
        self.sync_empty()

    def sync_empty(self):
        """Swap the table for a status page while it has nothing to show."""
        loading = self.loading_main or self.mode.get_selected() in self.set_loading
        if self.model.get_n_items():
            self.list_stack.set_visible_child_name("list")
            return
        if loading:
            self.empty.set_icon_name("content-loading-symbolic")
            self.empty.set_title("Loading packages\u2026")
            self.empty.set_description("")
        elif self.query:
            self.empty.set_icon_name("edit-find-symbolic")
            self.empty.set_title("No results")
            self.empty.set_description("Try a different search or filter.")
        else:
            self.empty.set_icon_name("package-x-generic-symbolic")
            self.empty.set_title("No packages")
            self.empty.set_description("Nothing matches this filter.")
        self.list_stack.set_visible_child_name("empty")

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
            for tv, stack in zip(self.tab_views, self.tab_stacks):
                tv.get_buffer().set_text("Select a package to see its details.")
                stack.set_visible_child_name("text")
            return
        # Debounce: holding an arrow key would otherwise spawn one dnf5 process per row.
        self.info_timer = GLib.timeout_add(250, self._start_info, self.cur, self.info_token)

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
                      "\n\nPress Apply to execute."
            dry = [[c for c in cmd if c != "-y"] + ["--assumeno"] for cmd in cmds if "-y" in cmd]
            return ["pkexec", "sh", "-c", script], preview, dry

        cmds = self.plan()
        if not cmds:
            return
        argv, preview, dry = build(False)
        other = [shlex.join(c) for c in cmds if "-y" not in c]
        # --allowerasing only exists on install/upgrade here, so only offer a retry for those
        can_erase = any(c[2] in ("install", "upgrade") for c in cmds)
        self.run_cmd("Apply changes", argv, preview, reload=True, dry=dry or None,
                     rebuild=build if can_erase else None,
                     note="Also runs: " + "; ".join(other) if other else "")

    def mark_selected(self, kind, label):
        names = [p.spec for p in self.selected() if p.installed]
        if not names:
            self.toast("Select installed packages first")
            return
        self.quiet(f"Marked {len(names)} package(s) as {label}",
                   ["pkexec", "dnf5", "mark", kind, *names], reload=False)
