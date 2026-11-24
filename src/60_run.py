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
