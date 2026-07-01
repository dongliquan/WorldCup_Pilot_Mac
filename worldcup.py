"""
World Cup Pilot — macOS desktop launcher.

Runs the local HTTP server (server.py) in a background thread and opens a
native WebKit window pointing at the single-page UI. Same feature set as the
Windows edition (WorldCup_Pilot); platform-specific chrome adapts here —
desktop notifications via osascript, Dock icon, WebKit GUI. All data comes
from ESPN (no API token required). Personal use only.

Dev run:
    python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/python worldcup.py

Build a .app bundle (see worldcup.spec / build.sh):
    ./build.sh          # -> dist/World Cup Pilot.app  (icon from icon.icns)
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

import webview


def app_root():
    if getattr(sys, "frozen", False):
        return sys._MEIPASS  # bundled, read-only assets
    return os.path.dirname(os.path.abspath(__file__))


def writable_root():
    """Where config.json / cache / logs live (next to the .app when bundled)."""
    if getattr(sys, "frozen", False):
        p = sys.executable
        while p and not p.endswith(".app") and os.path.dirname(p) != p:
            p = os.path.dirname(p)
        return os.path.dirname(p) if p.endswith(".app") else os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


sys.path.insert(0, app_root())
import server  # noqa: E402

# bundled (read-only) assets vs writable state next to the app
server.ROOT = app_root()
server.HTML = os.path.join(app_root(), "worldcup.html")
server.ASSETS_DIR = os.path.join(app_root(), "assets")
server.CONFIG_PATH = os.path.join(writable_root(), "config.json")
server.CACHE_DIR = os.path.join(writable_root(), "cache")
os.makedirs(server.CACHE_DIR, exist_ok=True)
server.seed_cache()   # inherit any committed AI picks (dist/cache) into the runtime cache
server.CONFIG = server.load_config()
server.VENUES = server.load_venues()
server.RANKING = server.load_ranking()
server.COUNTRY = server.load_country_info()

HOST, PORT = "127.0.0.1", 8770


def serve():
    server.ThreadingHTTPServer((HOST, PORT), server.Handler).serve_forever()


def wait_until_ready(timeout=8.0):
    deadline = time.time() + timeout
    url = f"http://{HOST}:{PORT}/api/status"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.1)
    return False


def set_app_icon():
    """Dev runs show Python's default Dock icon (the built .app uses icon.icns from
    the spec); set the running app's Dock icon to the FIFA emblem at runtime."""
    try:
        from AppKit import NSApplication, NSImage
        for name in ("icon.icns", "icon.ico"):
            p = os.path.join(app_root(), name)
            if os.path.exists(p):
                img = NSImage.alloc().initByReferencingFile_(p)
                NSApplication.sharedApplication().setApplicationIconImage_(img)
                break
    except Exception as e:
        print(f"[warn] could not set dock icon: {e}")


class Api:
    """Exposed to JS as window.pywebview.api.*"""
    def __init__(self):
        self._hl_window = None

    def open_highlight(self, url, title="하이라이트"):
        # single reused popup: load into the existing window if open, else create one
        try:
            w = self._hl_window
            if w is not None:
                try:
                    w.load_url(url)
                    return True
                except Exception:
                    self._hl_window = None
            w = webview.create_window(title=title, url=url, width=880, height=600, min_size=(560, 360))
            self._hl_window = w
            try:
                w.events.closed += lambda: setattr(self, "_hl_window", None)
            except Exception:
                pass
        except Exception as e:
            print(f"[warn] open_highlight: {e}")
        return True

    def open_external(self, url):
        try:
            webbrowser.open(url)
        except Exception as e:
            print(f"[warn] open_external: {e}")
        return True

    def set_live(self, on):
        """Windows shows a blinking taskbar LIVE badge; macOS has no taskbar overlay,
        so this is a no-op — the in-page LIVE badge conveys live state on macOS."""
        return True

    def notify(self, title, body, image=""):
        """Native macOS desktop notification via AppleScript (no extra deps)."""
        try:
            script = (f'display notification {json.dumps(str(body))} '
                      f'with title {json.dumps(str(title))}')
            subprocess.run(["osascript", "-e", script], check=False)
        except Exception as e:
            print(f"[warn] notify: {e}")
        return True


def prebuild_static():
    """Warm + persist all static team data (country, squad, photos, club) in the
    background so detail popups open instantly. Permanent cache → runs once for real;
    later launches find everything cached and just zip through."""
    base = f"http://{HOST}:{PORT}"
    # Warm the standings first so the Groups view is ready the moment it's opened
    # (a fresh .app has an empty cache → the first click would otherwise cold-fetch).
    try:
        urllib.request.urlopen(f"{base}/api/standings", timeout=20).read()
    except Exception as e:
        print(f"[warn] prebuild standings: {e}")
    try:
        with urllib.request.urlopen(f"{base}/api/matches", timeout=15) as r:
            matches = json.loads(r.read()).get("matches", [])
    except Exception:
        return
    ids = []
    for m in matches:
        for side in ("home", "away"):
            tid = (m.get(side) or {}).get("id")
            if tid and tid not in ids:
                ids.append(tid)
    for tid in ids:
        try:
            urllib.request.urlopen(f"{base}/api/team?id={tid}", timeout=90).read()
        except Exception:
            pass
        time.sleep(3)   # gentle on the free photo source


def main():
    threading.Thread(target=serve, daemon=True).start()
    if not wait_until_ready():
        print("[error] Server failed to start. Exiting.")
        return
    threading.Thread(target=prebuild_static, daemon=True).start()
    webview.create_window(
        title="World Cup",
        url=f"http://{HOST}:{PORT}/",
        width=1680, height=1040, min_size=(1024, 680),
        maximized=True,
        background_color="#050505", confirm_close=False,
        js_api=Api(),
    )
    # macOS/Linux use the default GUI (WebKit/GTK); set the Dock icon once the
    # Cocoa event loop is up. The bundled .app also carries icon.icns via the spec.
    webview.start(set_app_icon)


if __name__ == "__main__":
    main()
