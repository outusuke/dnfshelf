#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Dnfshelf: a Synaptic-style front end for Fedora (GTK4 + libadwaita).

Dependencies:
    sudo dnf install python3-gobject gtk4 libadwaita dnf5 dnf5-plugins polkit

Run:
    python3 dnfshelf.py

Install to ~/.local/bin and the app menu (or use the menu entry inside the app):
    python3 dnfshelf.py --install
    python3 dnfshelf.py --uninstall

Everything goes through the dnf5 CLI. Privileged operations use pkexec, so you
get one polkit prompt per transaction. Pins use `dnf5 versionlock`
(state in /etc/dnf/versionlock.toml).
"""
import gzip
import json
import operator
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import threading
import tomllib
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402
