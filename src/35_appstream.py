APPSTREAM_DIRS = ("/usr/share/swcatalog", "/var/cache/swcatalog")
APPSTREAM_CACHE = os.path.join(CACHE_DIR, "appstream.json")
APPSTREAM_FORMAT = 4  # bump when the parsed fields change
# Pre-0.16 catalogs name desktop apps just "desktop".
APP_TYPES = ("desktop-application", "console-application", "desktop")
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
SHOT_DIR = os.path.join(CACHE_DIR, "screenshots")
SHOT_MAX_BYTES = 5 * 1024 * 1024
SHOT_MAX_PIXELS = 16_000_000
SHOT_MAX_WIDTH = 640
SHOT_TIMEOUT = 10
SHOT_DEADLINE = 20
SHOT_KEEP = 200
SHOT_LIMIT = 8
_apps, _apps_lock = None, threading.Lock()
_shot_failed = set()
_textures = {}


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


def _screenshots(comp, langs):
    shots = comp.findall("screenshots/screenshot")
    shots.sort(key=lambda s: s.get("type") != "default")
    out = []
    for shot in shots:
        for img in shot.findall("image"):
            url = (img.text or "").strip()
            if img.get("type", "source") == "source" and url.startswith("https://") and len(url) <= 2048:
                cap = _localized(shot, "caption", langs)
                out.append([_flat(cap) if cap is not None else "", url])
                break
    return out[:SHOT_LIMIT]


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
                        "icon": [origin, icon] if _safe_name(origin) and _safe_name(icon) else [],
                        "screenshots": _screenshots(el, langs)}
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


def appstream_icon(name, sizes=("128x128", "64x64")):
    """Icon file for a package, only if the catalog is already loaded (never blocks the UI)."""
    meta = (_apps or {}).get(name)
    if not meta or not meta["icon"]:
        return ""
    origin, filename = meta["icon"]
    for base in APPSTREAM_DIRS:
        for size in sizes:
            path = os.path.join(base, "icons", origin, size, filename)
            if os.path.isfile(path):
                return path
    return ""


def appstream_screenshots(name):
    meta = (_apps or {}).get(name)
    return meta["screenshots"] if meta else []


def texture_from(path):
    if path not in _textures:
        if len(_textures) > 600:
            _textures.clear()
        try:
            _textures[path] = Gdk.Texture.new_from_filename(path)
        except GLib.Error:
            _textures[path] = None
    return _textures[path]


def screenshot_path(url):
    return os.path.join(SHOT_DIR, hashlib.sha256(url.encode()).hexdigest() + ".png")


def _public_host(host, port):
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addrs = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]
    except (OSError, ValueError):
        return False
    return bool(addrs) and all((getattr(a, "ipv4_mapped", None) or a).is_global for a in addrs)


def screenshot_url_ok(url):
    """Catalog URLs are untrusted: public https hosts only."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port or 443
    except ValueError:
        return False
    return (parts.scheme == "https" and len(url) <= 2048 and bool(parts.hostname)
            and not (parts.username or parts.password) and _public_host(parts.hostname, port))


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not screenshot_url_ok(urllib.parse.urljoin(req.full_url, newurl)):
            raise urllib.error.URLError("redirect refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "Dnfshelf", "Accept": "image/png, image/jpeg"})
    end = time.monotonic() + SHOT_DEADLINE
    size = 0
    with urllib.request.build_opener(_HttpsOnly).open(req, timeout=SHOT_TIMEOUT) as resp, \
            open(dest, "wb") as out:
        if int(resp.headers.get("Content-Length") or 0) > SHOT_MAX_BYTES:
            return False
        while chunk := resp.read(65536):
            size += len(chunk)
            if size > SHOT_MAX_BYTES or time.monotonic() > end:
                return False
            out.write(chunk)
    return size > 0


def _normalize_image(src, dest):
    # Header first: a tiny file can declare a huge image.
    fmt, width, height = GdkPixbuf.Pixbuf.get_file_info(src)
    if fmt is None or fmt.get_name() not in ("png", "jpeg") or width < 1 or height < 1 \
            or width * height > SHOT_MAX_PIXELS:
        return False
    pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(src, min(width, SHOT_MAX_WIDTH), -1, True)
    return pix.savev(dest, "png", [], [])


def _remove(*paths):
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass


def prune_screenshots():
    try:
        files = sorted((os.path.getmtime(p), p) for p in
                       (os.path.join(SHOT_DIR, n) for n in os.listdir(SHOT_DIR) if n.endswith(".png")))
    except OSError:
        return
    _remove(*(p for _, p in files[:-SHOT_KEEP]))


def fetch_screenshot(url):
    """Cached PNG path for a screenshot URL, or "" on any failure."""
    path = screenshot_path(url)
    if os.path.isfile(path):
        return path
    if url in _shot_failed or not screenshot_url_ok(url):
        return ""
    raw = tmp = ""
    try:
        os.makedirs(SHOT_DIR, exist_ok=True)
        fd, raw = tempfile.mkstemp(dir=SHOT_DIR, suffix=".dl")
        os.close(fd)
        tmp = raw + ".png"
        if _download(url, raw) and _normalize_image(raw, tmp):
            os.replace(tmp, path)
            prune_screenshots()
            return path
    except Exception:
        pass
    finally:
        _remove(raw, tmp)
    _shot_failed.add(url)
    return ""
