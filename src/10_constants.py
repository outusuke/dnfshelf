APP_ID = "org.outusuke.Dnfshelf"
VERSIONLOCK_FILE = "/etc/dnf/versionlock.toml"
AUTO_CONF = "/etc/dnf/automatic.conf"
AUTO_DEFAULTS_CONF = "/usr/share/dnf5/dnf5-plugins/automatic.conf"
AUTO_TIMER = "dnf5-automatic.timer"
AUTO_PKG = "dnf5-plugin-automatic"
AUTO_DEFAULTS = {"apply_updates": "no", "download_updates": "yes",
                 "upgrade_type": "default", "reboot": "never"}
AUTO_MODES = [{"apply_updates": "no", "download_updates": "no"},
              {"apply_updates": "no", "download_updates": "yes"},
              {"apply_updates": "yes", "download_updates": "yes"}]
AUTO_MODE_LABELS = ["Check only", "Download only", "Download and install"]
AUTO_MODE_HINTS = ["Only reports what is available",
                   "Downloads updates, you install them from here",
                   "Installs updates without asking"]
AUTO_SCOPES = {"default": "All updates", "security": "Security updates only"}
AUTO_REBOOTS = {"never": "Never", "when-needed": "When needed", "when-changed": "After any update"}
REPORT_FILE = "/var/lib/dnfshelf/report.txt"
UNIT_DIR = os.path.join(GLib.get_user_config_dir(), "systemd", "user")
NOTIFY_PATH = "dnfshelf-notify.path"
NOTIFY_SERVICE = "dnfshelf-notify.service"
NOTIFY_PATH_UNIT = """[Unit]
Description=Notify when dnf5 automatic has run (Dnfshelf)

[Path]
PathChanged={report}

[Install]
WantedBy=default.target
"""
NOTIFY_SERVICE_UNIT = """[Unit]
Description=Show the dnf5 automatic report (Dnfshelf)

[Service]
Type=oneshot
ExecStart={exe} --notify
"""
GS_NOTE = "Separate from the timer above. It also covers Flatpaks, which dnf5 automatic does not."
GS_BOTH = "Both are on and will update the same packages. Turn one off."
QF = r"%{name}|%{evr}|%{arch}|%{repoid}|%{summary}\n"
# List packages from the local metadata cache (no network round trip on every start).
# Falls back to a normal dnf5 run automatically if that fails. Use "Refresh repo
# metadata" in the menu to update the cache.
USE_CACHEONLY = True
CACHE_DIR = os.path.join(GLib.get_user_cache_dir(), "dnfshelf")
CACHE_FILE = os.path.join(CACHE_DIR, "packages.json")
SHOT_TAB = 4
SHOT_HEIGHT = 160
ICON_PX = 28
INFO_RULE = "\u2500" * 40
PKG_QF = r"%{name}\n"
SO_CAP = re.compile(r"^([^()\s]+\.so[^()\s]*)\(.*\)$")
COMPS_RE = re.compile(r"^(\S+)\s+(.+?)\s+(yes|no)\s*$")
WIDE_ENV = {**os.environ, "COLUMNS": "1000"}  # keep dnf5 tables from truncating names
MODES = ["All", "Installed", "Not installed", "Upgradable", "Queued changes",
         "Security updates", "User-installed", "Unneeded", "Leaves", "Extras"]
M_UPGRADABLE, M_QUEUED, M_SECURITY, M_USER, M_UNNEEDED, M_LEAVES, M_EXTRAS = 3, 4, 5, 6, 7, 8, 9
# Modes filled by one repoquery each, lazily: mode -> (flags, allow cache-only).
# Advisory data may be missing from the offline cache, so security may go online.
# No --installed: --userinstalled rejects it, and these modes only show installed packages.
SET_QUERIES = {
    M_SECURITY: (("--upgrades", "--security", "--latest-limit=1"), False),
    M_USER: (("--userinstalled",), True),
    M_UNNEEDED: (("--unneeded",), True),
    M_LEAVES: (("--leaves",), True),
    M_EXTRAS: (("--extras",), True),
}
