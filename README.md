# Dnfshelf

A Synaptic-style package manager for Fedora. GTK4, libadwaita, one Python file, no daemon. Everything goes through the `dnf5` command line.

## Requirements

- Fedora with dnf5 (Fedora 41 or newer)
- libadwaita 1.5 or newer

```
sudo dnf install python3-gobject gtk4 libadwaita dnf5 dnf5-plugins polkit
```

## Run

```
python3 dnfshelf.py
```

`--install` adds a launcher to `~/.local/bin` and the app menu, `--uninstall` removes it. Both are also in the app menu.

## Features

- Package list with search, sorting and filter modes: All, Installed, Not installed, Upgradable, Queued changes, Security updates, User-installed, Unneeded, Leaves, Extras
- Details tabs: Info, Dependencies, Files, Changelog
- Queue several changes, then apply them in one transaction: install, remove, upgrade, reinstall, downgrade
- Dry run before Apply, Upgrade all and security-only upgrades
- Pin or block a package (`dnf5 versionlock`), with a window to review and remove pins
- Automatic updates through `dnf5 automatic`: check, download or install, all or security only, restart policy, plus GNOME Software's automatic download switch, and desktop notifications that list what was downloaded or installed
- Groups and environments: install (with optional packages) and remove
- Repositories: enable, disable, add a local repo
- Transaction history with undo and rollback
- Mark packages as user-installed or dependency, autoremove, clean caches
- Check for updates without a password prompt
- Banner with a Reboot button when `dnf5 needs-restarting` recommends one

## How it works

- Read-only queries run as your user, preferring the local metadata cache (`dnf5 --cacheonly`).
- Changes run through `pkexec`, so you get one polkit prompt per transaction.
- The package list is cached in `~/.cache/dnfshelf/packages.json`, shown immediately on start and refreshed in the background.
- `--allowerasing` is never a setting. It is offered only after a dry run fails, for that transaction only.
- Pins live in `/etc/dnf/versionlock.toml`.
- Automatic updates are set in `/etc/dnf/automatic.conf` and run by `dnf5-automatic.timer` (package `dnf5-plugin-automatic`).
- Notifications: `dnf5 automatic` writes its report to `/var/lib/dnfshelf/report.txt` through its `command` emitter, and a systemd user path unit (`dnfshelf-notify.path`) shows it as a desktop notification. They only appear while you are logged in.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
