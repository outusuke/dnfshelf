BIN_PATH = os.path.expanduser("~/.local/bin/dnfshelf")
DESKTOP_PATH = os.path.expanduser(f"~/.local/share/applications/{APP_ID}.desktop")
DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=Dnfshelf
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
