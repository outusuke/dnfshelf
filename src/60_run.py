# ---------------------------------------------------------------- run window
PLAN_ROW = re.compile(r"^ (\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\d[\d.,]* \S+)\s*$")
REPL_ROW = re.compile(r"^\s+replacing (\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\d[\d.,]* \S+)\s*$")
PLAN_HEAD = re.compile(r"^([A-Z][A-Za-z ]+):\s*$")
PLAN_COUNT = re.compile(r"^ ([A-Z][A-Za-z ]+):\s+(\d+) packages?\s*$")
PLAN_DOWNLOAD = re.compile(r"Need to download ([\d.,]+ \S+?)\.")
PLAN_DISK = re.compile(r"After this operation, ([\d.,]+ \S+) (extra will be used|will be freed)")


def parse_plan(text):
    """Turn a dnf5 dry-run table into (sections, stats); None if it does not add up."""
    sections, cur, counts, in_summary = [], None, {}, False
    for line in text.splitlines():
        if line.startswith("Transaction Summary"):
            in_summary, cur = True, None
        elif in_summary:
            m = PLAN_COUNT.match(line)
            if m:
                counts[m[1]] = int(m[2])
        elif PLAN_HEAD.match(line):
            cur = (PLAN_HEAD.match(line)[1], [])
            sections.append(cur)
        elif cur is not None:
            m = REPL_ROW.match(line)
            if m and cur[1]:
                cur[1][-1][3] = m[3].removeprefix("0:")
                continue
            m = PLAN_ROW.match(line)
            if m:
                cur[1].append([m[1], m[3].removeprefix("0:"), m[5], ""])
    sections = [sec for sec in sections if sec[1]]
    total = sum(len(items) for _, items in sections)
    if not total or total != sum(n for k, n in counts.items() if k != "Replacing"):
        return None
    stats = []
    m = PLAN_DOWNLOAD.search(text)
    if m:
        stats.append(("Download size", m[1]))
    m = PLAN_DISK.search(text)
    if m:
        stats.append(("Disk space", ("+" if m[2].startswith("extra") else "\u2212") + m[1]))
    return sections, stats


def plan_problem(text):
    """The reason lines dnf5 prints after a failed resolve."""
    lines = text.split("Failed to resolve the transaction", 1)[1].splitlines()[1:]
    out = []
    for line in lines:
        if not line.strip() or line.startswith("You can try"):
            break
        out.append(line.strip())
    return "\n".join(out[:5]) or "The package manager could not resolve this transaction."


class RunWindow(Adw.Window):
    """Confirms a transaction as a list of package changes, then runs it. Other callers get plain output."""

    def __init__(self, parent, title, argv, preview, on_done=None, autorun=False, dry=None,
                 rebuild=None, note=""):
        super().__init__(transient_for=parent, modal=True, title=title,
                         default_width=620, default_height=620)
        self.argv, self.on_done, self.note = argv, on_done, note
        self.rebuild = rebuild   # rebuild(erase) -> (argv, preview, dry), used to retry
        self.erasing = self.running = False
        self.friendly = bool(dry)
        self.page, self.plan_page = "log", None
        self.tv = Gtk.TextView(editable=False, monospace=True, left_margin=8,
                               top_margin=8, bottom_margin=8, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.buf = self.tv.get_buffer()
        self.buf.set_text(preview + "\n")
        self.close_btn = Gtk.Button(label="Cancel" if dry else "Close")
        self.close_btn.connect("clicked", lambda *_: self.close())
        self.run_btn = Gtk.Button(label="Apply", css_classes=["suggested-action"])
        self.run_btn.connect("clicked", lambda *_: self.start())
        self.details_btn = Gtk.ToggleButton(icon_name="utilities-terminal-symbolic",
                                            tooltip_text="Show full output", visible=self.friendly)
        self.details_btn.connect("toggled", lambda *_: self.show(self.page))
        self.retry_btn = Gtk.Button(
            label="Retry allowing package removal", visible=False, halign=Gtk.Align.CENTER,
            css_classes=["pill"],
            tooltip_text="Run the dry run again with --allowerasing. "
                         "Nothing is removed until you press Apply.")
        self.retry_btn.connect("clicked", lambda *_: self.retry())
        hb = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        hb.pack_start(self.close_btn)
        hb.pack_end(self.run_btn)
        hb.pack_end(self.details_btn)
        self.banner = Adw.Banner()
        self.busy = Adw.StatusPage(child=Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER,
                                                     width_request=32, height_request=32))
        self.result = Adw.StatusPage(child=self.retry_btn)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self.stack.add_named(Gtk.ScrolledWindow(child=self.tv), "log")
        self.stack.add_named(self.busy, "busy")
        self.stack.add_named(self.result, "result")
        tb = Adw.ToolbarView()
        tb.add_top_bar(hb)
        tb.add_top_bar(self.banner)
        tb.set_content(self.stack)
        self.set_content(tb)
        esc = Gtk.ShortcutController()
        esc.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("Escape"),
                                          Gtk.CallbackAction.new(self._escape)))
        self.add_controller(esc)
        self.connect("close-request", lambda *_: self.running)
        if dry:
            self.run_btn.set_sensitive(False)
            self.set_busy("Calculating changes\u2026")
            self.append("Calculating transaction (dry run)...\n")
            threading.Thread(target=self._dry_worker, args=(dry,), daemon=True).start()
        elif autorun:
            self.start()

    def _escape(self, *_):
        if not self.running:
            self.close()
        return True

    def show(self, name):
        self.page = name
        self.stack.set_visible_child_name("log" if self.details_btn.get_active() else name)

    def set_busy(self, title, text=""):
        self.busy.set_title(title)
        self.busy.set_description(GLib.markup_escape_text(text))
        self.show("busy")

    def set_result(self, icon, title, text, retry=False):
        self.result.set_icon_name(icon)
        self.result.set_title(title)
        self.result.set_description(GLib.markup_escape_text(text))
        self.retry_btn.set_visible(retry)
        self.show("result")

    def append(self, text):
        self.buf.insert(self.buf.get_end_iter(), text)
        self.buf.place_cursor(self.buf.get_end_iter())
        self.tv.scroll_to_mark(self.buf.get_insert(), 0, False, 0, 0)
        if self.running and self.friendly:
            last = next((ln.strip() for ln in reversed(text.splitlines()) if ln.strip()), "")
            if last:
                self.busy.set_description(GLib.markup_escape_text(last[:100]))
        return False

    def _dry_worker(self, dry):
        out = []
        for cmd in dry:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, env=WIDE_ENV)
                out.append("$ " + shlex.join(cmd) + "\n" + r.stdout + r.stderr)
            except Exception as e:
                out.append(f"Error running {shlex.join(cmd)}: {e}\n")
        GLib.idle_add(self._dry_done, "\n".join(out).replace("\r", "\n"))

    def _plan_widget(self, sections, stats):
        page = Adw.PreferencesPage()
        if stats or self.note:
            top = Adw.PreferencesGroup()
            if self.note:
                top.set_description(GLib.markup_escape_text(self.note))
            for label, value in stats:
                row = Adw.ActionRow(title=label, use_markup=False)
                row.add_suffix(Gtk.Label(label=value, css_classes=["dim-label"]))
                top.add(row)
            page.add(top)
        for head, items in sections:
            group = Adw.PreferencesGroup(title=f"{head} ({len(items)})")
            for name, evr, size, old in items:
                row = Adw.ActionRow(title=name, subtitle=f"{old} \u2192 {evr}" if old else evr,
                                    use_markup=False)
                row.add_suffix(Gtk.Label(label=size, css_classes=["dim-label"]))
                group.add(row)
            page.add(group)
        return page

    def _dry_done(self, text):
        failed = "Failed to resolve the transaction" in text
        self.banner.set_revealed(False)
        self.append("\n" + text + "\n")
        if failed:
            can_retry = self.rebuild is not None and not self.erasing
            why = ("Allowing package removal was not enough to resolve this."
                   if self.erasing else plan_problem(text))
            self.set_result("dialog-error-symbolic", "Could not prepare these changes",
                            why + "\n\nNothing was changed.", retry=can_retry)
            return False  # Apply stays disabled: it would fail the same way
        if self.erasing:
            self.banner.set_title("Package removal is allowed. Packages you did not select "
                                  "may be removed. Review the Removing entries.")
            self.banner.set_revealed(True)
        elif "Removing dependent packages" in text or "Removing unused dependencies" in text:
            self.banner.set_title("This also removes other packages. Review before applying.")
            self.banner.set_revealed(True)
        plan = parse_plan(text)
        if self.plan_page:
            self.stack.remove(self.plan_page)
        if plan:
            self.plan_page = self._plan_widget(*plan)
            self.stack.add_named(self.plan_page, "plan")
            self.show("plan")
        else:
            self.append("--- dry run finished; press Apply to apply ---\n")
            self.details_btn.set_visible(False)
            self.show("log")
        self.run_btn.set_sensitive(True)
        return False

    def retry(self):
        self.erasing = True
        self.retry_btn.set_visible(False)
        self.run_btn.set_sensitive(False)
        self.argv, preview, dry = self.rebuild(True)
        self.append("\n--- retrying with --allowerasing ---\n" + preview +
                    "\nCalculating transaction (dry run)...\n")
        self.set_busy("Calculating changes\u2026")
        threading.Thread(target=self._dry_worker, args=(dry,), daemon=True).start()

    def start(self):
        self.running = True
        self.run_btn.set_sensitive(False)
        self.close_btn.set_sensitive(False)
        self.banner.set_revealed(False)
        if self.friendly:
            self.run_btn.set_visible(False)
            self.set_busy("Applying changes\u2026", "Waiting for authentication\u2026")
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
        self.running = False
        self.append(f"\n--- finished (exit code {rc}) ---\n")
        self.close_btn.set_label("Close")
        self.close_btn.set_sensitive(True)
        if self.friendly:
            if rc == 0:
                self.set_result("emblem-ok-symbolic", "All done", "")
            elif rc in (126, 127):
                self.set_result("dialog-password-symbolic", "Authentication failed or cancelled",
                                "Nothing was changed.")
            else:
                self.set_result("dialog-error-symbolic", "Something went wrong",
                                f"The command exited with code {rc}. Open the full output "
                                "for details.")
        if self.on_done:
            self.on_done(rc)
        return False
