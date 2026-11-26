# ---------------------------------------------------------------- main window
class HistoryWindow(Adw.Window):
    """Native transaction history with details, undo and rollback."""

    def __init__(self, parent, auto_only=False):
        super().__init__(transient_for=parent, modal=True, title="Transaction history",
                         default_width=760, default_height=600)
        self.parent_win = parent
        self.placeholder = Adw.StatusPage(title="Loading\u2026", css_classes=["compact"])
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
        self.placeholder.set_title("No automatic runs found" if self.only_auto.get_active()
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
        w = RunWindow(self, f"Transaction {tid}", [], f"dnf5 history info {tid}",
                      dry=[["dnf5", "history", "info", str(tid)]], parse=parse_transaction,
                      busy="Loading transaction\u2026")
        w.close_btn.set_label("Close")
        w.run_btn.set_visible(False)
        for label, tip, fn in (("Undo", "Undo this transaction", self.undo),
                               ("Roll back", "Roll back to the state after this one", self.rollback)):
            b = Gtk.Button(label=label, tooltip_text=tip)
            b.connect("clicked", lambda _b, f=fn: (w.close(), f(tid)))
            w.header.pack_end(b)
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
            body.append(Adw.StatusPage(title="No pinned packages", css_classes=["compact"]))
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
        self.listbox.set_placeholder(Adw.StatusPage(title="Loading\u2026",
                                                    css_classes=["compact"]))
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
        self.listbox.set_placeholder(Adw.StatusPage(title="Nothing to show",
                                                    css_classes=["compact"]))
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
        GLib.idle_add(self._populate, parse_repos(run(["dnf5", "repo", "list", "--all"], env=ENGLISH_ENV)))

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
        if repo_id_taken(rid, self.rows):
            self.overlay.add_toast(Adw.Toast.new(f"A repository named {rid} already exists."))
            return
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
