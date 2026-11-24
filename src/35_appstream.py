APPSTREAM_DIRS = ("/usr/share/swcatalog", "/var/cache/swcatalog")
APPSTREAM_CACHE = os.path.join(CACHE_DIR, "appstream.json")
APPSTREAM_FORMAT = 2  # bump when the parsed fields change
# Pre-0.16 catalogs name desktop apps just "desktop".
APP_TYPES = ("desktop-application", "console-application", "desktop")
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
_apps, _apps_lock = None, threading.Lock()


def ui_languages():
    raw = (os.environ.get("LANGUAGE") or os.environ.get("LC_ALL")
           or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or "")
    langs = []
    for part in raw.split(":"):
        part = part.split(".")[0].split("@")[0]
        if part and part not in ("C", "POSIX"):
            langs += [part, part.split("_")[0]]
    return list(dict.fromkeys(langs))


def appstream_files():
    files = []
    for base in APPSTREAM_DIRS:
        folder = os.path.join(base, "xml")
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        files += [os.path.join(folder, n) for n in names if n.endswith((".xml", ".xml.gz"))]
    return files


def _localized(comp, tag, langs):
    found = {e.get(XML_LANG): e for e in comp.findall(tag)}
    for lang in (*langs, None):
        if lang in found:
            return found[lang]
    return None


def _flat(elem):
    return " ".join("".join(elem.itertext()).split())


def _description(elem):
    blocks = []
    for child in elem:
        if child.tag in ("ul", "ol"):
            blocks.append("\n".join("  \u2022 " + _flat(li) for li in child))
        elif _flat(child):
            blocks.append(_flat(child))
    return "\n\n".join(blocks)


def _safe_name(s):
    return bool(s) and os.path.basename(s) == s and s not in (".", "..")


def parse_appstream(stream, langs):
    """{package name: metadata} from one AppStream catalog file."""
    out, origin = {}, ""
    for event, el in ET.iterparse(stream, events=("start", "end")):
        if event == "start" and el.tag == "components":
            origin = el.get("origin", "")
        elif event == "end" and el.tag == "component":
            if el.get("type") in APP_TYPES:
                text = lambda tag: _flat(e) if (e := _localized(el, tag, langs)) is not None else ""
                desc = _localized(el, "description", langs)
                icon = next((i.text for i in el.findall("icon")
                             if i.get("type") == "cached" and i.text), "")
                meta = {"name": text("name"), "summary": text("summary"),
                        "description": _description(desc) if desc is not None else "",
                        "license": el.findtext("project_license") or "",
                        "homepage": next((u.text or "" for u in el.findall("url")
                                          if u.get("type") == "homepage"), ""),
                        "categories": [c.text for c in el.findall("categories/category") if c.text],
                        "icon": [origin, icon] if _safe_name(origin) and _safe_name(icon) else []}
                for pkg in el.findall("pkgname"):
                    if pkg.text:
                        out.setdefault(pkg.text, meta)
            el.clear()
    return out


def build_appstream(langs):
    apps = {}
    for path in appstream_files():
        opener = gzip.open if path.endswith(".gz") else open
        try:
            with opener(path, "rb") as f:
                for pkg, meta in parse_appstream(f, langs).items():
                    apps.setdefault(pkg, meta)
        except (OSError, EOFError, ET.ParseError):
            continue
    return apps


def load_appstream(langs):
    """Catalog index, rebuilt only when a catalog file or the language changed."""
    sig = [APPSTREAM_FORMAT, langs]
    for path in appstream_files():
        try:
            st = os.stat(path)
        except OSError:
            continue
        sig.append([path, st.st_mtime_ns, st.st_size])
    try:
        with open(APPSTREAM_CACHE) as f:
            data = json.load(f)
        if data.get("sig") == sig:
            return data["apps"]
    except (OSError, ValueError, AttributeError, KeyError):
        pass
    apps = build_appstream(langs)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = APPSTREAM_CACHE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"sig": sig, "apps": apps}, f, separators=(",", ":"))
        os.replace(tmp, APPSTREAM_CACHE)
    except OSError:
        pass
    return apps


def appstream_apps():
    global _apps
    with _apps_lock:
        if _apps is None:
            _apps = load_appstream(ui_languages())
        return _apps


def appstream_text(name):
    meta = appstream_apps().get(name)
    if not meta:
        return ""
    head = meta["name"] + (f" \u2014 {meta['summary']}" if meta["summary"] else "")
    facts = "\n".join(f"{k}: {v}" for k, v in (
        ("Homepage", meta["homepage"]), ("License", meta["license"]),
        ("Categories", ", ".join(meta["categories"]))) if v)
    return "\n\n".join(x for x in (head, meta["description"], facts) if x)


def appstream_icon(name):
    """Icon file for a package, only if the catalog is already loaded (never blocks the UI)."""
    meta = (_apps or {}).get(name)
    if not meta or not meta["icon"]:
        return ""
    origin, filename = meta["icon"]
    for base in APPSTREAM_DIRS:
        for size in ("128x128", "64x64"):
            path = os.path.join(base, "icons", origin, size, filename)
            if os.path.isfile(path):
                return path
    return ""
