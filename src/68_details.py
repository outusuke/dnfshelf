# ---------------------------------------------------------------- details pane
class DetailsMixin:
    """Details tabs and screenshots for Win."""

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
        self.tab_stacks[idx].set_visible_child_name("text")

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

    def show_info_rows(self, p, intro, rows):
        icon = appstream_icon(p.name)
        facts = dict(rows)
        if facts.get("Release"):
            facts["Version"] = f"{facts.get('Version', '')}-{facts['Release']}"
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12,
                      margin_bottom=12, margin_start=12, margin_end=12)
        if intro or icon:
            head = Gtk.Box(spacing=12)
            if icon:
                head.append(Gtk.Image(file=icon, pixel_size=64, valign=Gtk.Align.START))
            head.append(Gtk.Label(label=intro, wrap=True, xalign=0, hexpand=True, selectable=True,
                                  valign=Gtk.Align.START))
            box.append(head)
        grid = Gtk.Grid(column_spacing=16, row_spacing=4)
        skip = {"Description", "Release"} | ({"Epoch"} if facts.get("Epoch") == "0" else set())
        for n, (key, _) in enumerate(k for k in rows if k[0] not in skip):
            grid.attach(Gtk.Label(label=key, xalign=1, yalign=0, css_classes=["dim-label"]), 0, n, 1, 1)
            grid.attach(Gtk.Label(label=facts[key], xalign=0, yalign=0, hexpand=True, wrap=True,
                                  selectable=True), 1, n, 1, 1)
        box.append(grid)
        if facts.get("Description"):
            box.append(Gtk.Label(label=facts["Description"], wrap=True, xalign=0, selectable=True))
        self.set_rows_page(0, Gtk.ScrolledWindow(child=Adw.Clamp(maximum_size=900, child=box)))

    def set_rows_page(self, idx, page):
        stack = self.tab_stacks[idx]
        old = stack.get_child_by_name("rows")
        if old:
            stack.remove(old)
        stack.add_named(page, "rows")
        stack.set_visible_child_name("rows")

    def deps_page(self, sections):
        page = Adw.PreferencesPage()
        for title, items in sections:
            group = Adw.PreferencesGroup(title=title)
            for item in items:
                group.add(Adw.ActionRow(title=item, use_markup=False, title_selectable=True))
            page.add(group)
        return page

    def files_page(self, lines):
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _f, it: it.set_child(
            Gtk.Label(xalign=0, selectable=True, margin_start=8, margin_end=8, css_classes=["monospace"])))
        factory.connect("bind", lambda _f, it: it.get_child().set_label(it.get_item().get_string()))
        view = Gtk.ListView(model=Gtk.NoSelection(model=Gtk.StringList.new(lines)), factory=factory)
        return Gtk.ScrolledWindow(vexpand=True, child=view)

    def changelog_page(self, parsed):
        heading, entries = parsed
        group = Adw.PreferencesGroup(title=heading)
        for head, body in entries:
            group.add(Adw.ActionRow(title=head, subtitle="\n".join(body), subtitle_lines=0,
                                    use_markup=False, title_selectable=True,
                                    subtitle_selectable=True))
        page = Adw.PreferencesPage()
        page.add(group)
        return page

    def show_text(self, idx, p, text):
        if idx == 0:
            info = parse_info(text)
            if info:
                self.show_info_rows(p, *info)
                return
        else:
            parse, build = {1: (parse_deps, self.deps_page), 2: (parse_files, self.files_page),
                            3: (parse_changelog, self.changelog_page)}[idx]
            parsed = parse(text)
            if parsed:
                self.set_rows_page(idx, build(parsed))
                return
        self.tab_stacks[idx].set_visible_child_name("text")
        buf = self.tab_views[idx].get_buffer()
        buf.set_text(text)
        icon = appstream_icon(p.name) if idx == 0 else ""
        if icon:
            try:
                buf.insert_paintable(buf.get_start_iter(), Gdk.Texture.new_from_filename(icon))
                buf.insert(buf.get_iter_at_offset(1), "\n")
            except GLib.Error:
                pass
