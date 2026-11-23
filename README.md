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
- Pin, block or pin to a chosen version (`dnf5 versionlock`), with a window to review and remove pins
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

## Limitations

- RPM only. No Flatpak.
- Not packaged. Run it from a checkout.
- Tested by one person on one machine. Expect rough edges and report them as issues.
