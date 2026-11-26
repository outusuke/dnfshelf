class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        act = Gio.SimpleAction.new("open", None)
        act.connect("activate", lambda *_: self.activate())
        self.add_action(act)
        act = Gio.SimpleAction.new("history", None)
        act.connect("activate", lambda *_: self.show_history())
        self.add_action(act)

    def show_history(self):
        win = next((w for w in self.get_windows() if isinstance(w, Win)), None)
        if not win:
            self.activate()
            win = next(w for w in self.get_windows() if isinstance(w, Win))
        HistoryWindow(win, auto_only=True).present()

    def do_open(self, files, _n_files, _hint):
        win = next((w for w in self.get_windows() if isinstance(w, Win)), None)
        if not win:
            self.activate()
            win = next(w for w in self.get_windows() if isinstance(w, Win))
        paths = [f.get_path() for f in files if f.get_path() and f.get_path().endswith(".rpm")]
        if not paths:
            win.toast("Only .rpm files can be opened here")
            return
        argv, preview, dry = rpm_install_plan(paths)
        win.run_cmd("Install local package", argv, preview, reload=True, dry=dry)

    def do_activate(self):
        # menu clicks and notification clicks all land here
        win = next((w for w in self.get_windows() if isinstance(w, Win)), None)
        if win:
            win.present()
            return
        win = Win(self)
        win.present()
        win.start()


if __name__ == "__main__":
    if "--install" in sys.argv[1:]:
        print(install_launcher())
    elif "--uninstall" in sys.argv[1:]:
        print(uninstall_launcher())
    elif "--notify" in sys.argv[1:]:
        sys.exit(notify_report())
    elif "--can-update" in sys.argv[1:]:
        why = update_blocker()
        if why:
            print(f"Automatic update skipped: {why}", file=sys.stderr)
        sys.exit(3 if why else 0)
    else:
        App().run(sys.argv)
