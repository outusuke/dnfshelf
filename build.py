#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build dnfshelf.py from src/. `build.py --check` fails if it is stale. Edit src/, not dnfshelf.py."""
import ast
import builtins
import os
import symtable
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
OUT = os.path.join(HERE, "dnfshelf.py")

# Lowest layer first. A fragment may only use names from its own or a lower layer.
LAYERS = [
    ("00_header.py", "docstring, imports, gi.require_version (the only imports)"),
    ("10_constants.py", "paths, unit templates, dnf5 query formats, mode tables; no logic"),
    ("20_dnf.py", "run dnf5 as the user: read-only queries and their parsers"),
    ("25_launcher.py", "copy this file to ~/.local/bin, app-menu entry"),
    ("30_system.py", "pins, automatic.conf editing, systemd guard and notify units, "
                     "GameMode, GNOME Software"),
    ("35_appstream.py", "AppStream catalog: package name to app metadata and icon, read from local files; optional screenshot fetch"),
    ("40_data.py", "package list assembly, details text, package cache file, "
                   "history and repo parsers"),
    ("50_model.py", "queue actions, Pkg, PkgModel"),
    ("60_run.py", "RunWindow: preview, dry run, runs the privileged command"),
    ("65_dialogs.py", "history, pins, automatic updates, groups, repositories windows"),
    ("70_mainwindow.py", "Win: package table, queue, plan() and apply()"),
    ("80_app.py", "App and the command-line entry point"),
]
SEPARATOR = "\n\n\n"
BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}


def bound_names(tree):
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        else:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    names.add(sub.id)
    return names


def global_uses(source, filename):
    uses = set()

    def walk(table, is_module):
        for sym in table.get_symbols():
            if not sym.is_referenced():
                continue
            if is_module:
                unresolved = not (sym.is_assigned() or sym.is_imported() or sym.is_namespace())
            else:
                unresolved = sym.is_global()
            if unresolved:
                uses.add(sym.get_name())
        for child in table.get_children():
            walk(child, False)

    walk(symtable.symtable(source, filename, "exec"), True)
    return uses


def check_layers(fragments):
    problems, owner, bound, uses = [], {}, [], []
    for layer, (name, source) in enumerate(fragments):
        try:
            tree = ast.parse(source, name)
        except SyntaxError as e:
            return [f"{name}: {e}"]
        if layer:
            for node in tree.body:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    problems.append(f"{name}:{node.lineno}: imports belong in {fragments[0][0]}")
        names = bound_names(tree)
        for n in sorted(names):
            if n in owner:
                problems.append(f"{name}: {n!r} is already defined in {fragments[owner[n]][0]}")
            else:
                owner[n] = layer
        bound.append(names)
        uses.append(global_uses(source, name))
    for layer, (name, _src) in enumerate(fragments):
        for n in sorted(uses[layer]):
            if n in bound[layer] or n in BUILTINS:
                continue
            if n not in owner:
                problems.append(f"{name}: {n!r} is not defined anywhere")
            elif owner[n] > layer:
                problems.append(f"{name}: layer rule broken, uses {n!r} from the higher "
                                f"layer {fragments[owner[n]][0]}")
    return problems


def build():
    fragments = []
    for name, _purpose in LAYERS:
        with open(os.path.join(SRC, name), encoding="utf-8") as f:
            fragments.append((name, f.read().strip("\n")))
    extra = sorted(set(os.listdir(SRC)) - {n for n, _ in LAYERS} - {"__pycache__"})
    problems = [f"src/{n} is not listed in LAYERS" for n in extra]
    problems += check_layers(fragments)
    if problems:
        sys.exit("build failed:\n  " + "\n  ".join(problems))
    text = SEPARATOR.join(src for _n, src in fragments) + "\n"
    compile(text, "dnfshelf.py", "exec")
    return text


def main(argv):
    text = build()
    if "--check" in argv:
        try:
            with open(OUT, encoding="utf-8") as f:
                current = f.read()
        except OSError:
            current = None
        if current != text:
            sys.exit("dnfshelf.py is out of date: edit src/ and run python3 build.py")
        print("dnfshelf.py matches src/")
        return
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"wrote {os.path.relpath(OUT, HERE)} ({text.count(chr(10))} lines from "
          f"{len(LAYERS)} fragments)")


if __name__ == "__main__":
    main(sys.argv[1:])
