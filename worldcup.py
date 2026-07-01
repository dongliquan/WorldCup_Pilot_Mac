"""
World Cup Pilot — macOS desktop launcher.

Runs the local HTTP server (server.py) in a background thread and opens a
native WebKit window pointing at the single-page UI. Same feature set as the
Windows edition; platform-specific bits (notifications, window chrome) adapt
at runtime. All data comes from ESPN (no API token required). Personal use only.

Dev run:
    python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/python worldcup.py

Build a .app bundle (see worldcup.spec / build.sh):
    ./build.sh          # -> dist/World Cup Pilot.app  (icon from icon.icns)
"""
import ctypes
import errno
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from ctypes import byref, c_void_p, wintypes

import webview


# ---- Windows taskbar overlay icon (ITaskbarList3) ---------------------------
# Puts a small red "LIVE" badge on the taskbar button when a match is live.
class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def _guid(s):
    g = _GUID()
    ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(s), byref(g))
    return g


_CLSID_TaskbarList = "{56FDF344-FD6D-11D0-958A-006097C9A090}"
_IID_ITaskbarList3 = "{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}"
_taskbar_state = {"on": None}

def _is_windows():
    return sys.platform == "win32"


def _find_main_hwnd():
    """Top-level visible window whose title contains 'World Cup'."""
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _):
        if ctypes.windll.user32.IsWindowVisible(hwnd):
            n = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, n + 1)
                if "World Cup" in buf.value:
                    found.append(hwnd)
        return True

    ctypes.windll.user32.EnumWindows(_cb, 0)
    return found[0] if found else None


def _vtbl_call(ptr, index, *args):
    vtbl = ctypes.cast(ptr, ctypes.POINTER(c_void_p))[0]
    fn = ctypes.cast(vtbl, ctypes.POINTER(c_void_p))[index]
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, *[type(a) for a in args])
    return proto(fn)(ptr, *args)


def set_taskbar_overlay(on):
    """Set (on=True) or clear the taskbar overlay icon. Best-effort; silent on failure."""
    if not _is_windows():
        return
    if _taskbar_state["on"] == on:
        return
    try:
        ctypes.windll.ole32.CoInitialize(None)
        hwnd = webview.windows[0].hwnd if webview.windows else _find_main_hwnd()
        if not hwnd:
            return
        pptr = c_void_p()
        if ctypes.windll.ole32.CoCreateInstance(
                byref(_guid(_CLSID_TaskbarList)), None, 1,
                byref(_guid(_IID_ITaskbarList3)), byref(pptr)) != 0 or not pptr.value:
            return
        try:
            _vtbl_call(pptr, 3)   # HrInit()
            hicon = 0
            desc = None
            if on:
                ico = os.path.join(app_root(), "assets", "live_dot.ico")
                hicon = ctypes.windll.user32.LoadImageW(
                    None, ctypes.c_wchar_p(ico), 1, 0, 0, 0x00000010 | 0x00000040)  # IMAGE_ICON | LR_LOADFROMFILE | LR_DEFAULTSIZE
                desc = ctypes.c_wchar_p("LIVE")
            # SetOverlayIcon(hwnd, hicon, desc) — vtable index 18
            _vtbl_call(pptr, 18, wintypes.HWND(hwnd), wintypes.HANDLE(hicon),
                       desc if desc else ctypes.c_wchar_p(None))
            _taskbar_state["on"] = on
        finally:
            _vtbl_call(pptr, 2)   # Release()
    except Exception as e:
        print(f"[warn] taskbar overlay: {e}")


# ---- blinking 🔴 LIVE window title ------------------------------------------
_TITLE_PLAIN = "World Cup"
_TITLE_ON = "🔴 LIVE · World Cup"
_TITLE_OFF = "       LIVE · World Cup"   # red dot hidden → blink effect
_blink_stop = threading.Event()
_blink_thread = [None]


def _get_main_window():
    """pywebview의 uid를 사용하여 메인 윈도우 객체를 안전하게 가져옵니다."""
    try:
        return webview.get_window('main_window')
    except Exception:
        if webview.windows:
            return webview.windows[0]
    return None


def _set_title(text):
    try:
        win = _get_main_window()
        if win:
            win.set_title(text)
    except Exception:
        pass


def start_live_blink():
    """Blink the red dot in the window title AND the taskbar overlay while live."""
    if _blink_thread[0] and _blink_thread[0].is_alive():
        return
    _blink_stop.clear()

    def loop():
        shown = True
        while not _blink_stop.is_set():
            _set_title(_TITLE_ON if shown else _TITLE_OFF)   # 🔴 dot blinks in the title…
            try:
                set_taskbar_overlay(shown)                   # …in sync with the taskbar badge
            except Exception:
                pass
            shown = not shown
            _blink_stop.wait(0.8)

    _blink_thread[0] = threading.Thread(target=loop, daemon=True)
    _blink_thread[0].start()


def stop_live_blink():
    _blink_stop.set()
    _set_title(_TITLE_PLAIN)
    try:
        set_taskbar_overlay(False)   # clear the red dot
    except Exception:
        pass


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
server.CONFIG = server.load_config()
server.VENUES = server.load_venues()
server.RANKING = server.load_ranking()
server.COUNTRY = server.load_country_info()

HOST, PORT = "127.0.0.1", 8770


def serve():
    for attempt in range(3):
        try:
            server.ThreadingHTTPServer((HOST, PORT), server.Handler).serve_forever()
            return
        except OSError as e:
            if e.errno == errno.EADDRINUSE and attempt < 2:
                print(f"[warn] Port {PORT} busy, retrying...")
                time.sleep(0.2)
                continue
            raise


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
    """Windows: the taskbar/window icon comes from the .exe icon (icon.ico, set in
    the PyInstaller spec), so nothing to do at runtime.
    macOS: dev runs show Python's default Dock icon (the built .app uses icon.icns
    from the spec); set the running app's Dock icon to the FIFA emblem at runtime."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSImage
        for name in ("icon.icns", os.path.join("assets", "icon.png")):
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
                    w.activate()
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

    def _toast_image(self, image):
        """Local file path for the toast image. Remote http(s) URLs (e.g. a scorer's
        photo) are downloaded first — WinRT unpackaged toasts can't load web images.
        Falls back to the bundled app icon."""
        default = os.path.join(app_root(), "assets", "logo.png")   # app/World Cup logo
        if not image:
            return default
        
        path_from_uri = image[8:] if image.startswith("file:///") else image
        if os.path.isfile(path_from_uri):
            return path_from_uri

        if image.startswith("http"):
            try:
                import tempfile
                low = image.lower()
                ext = ".jpg" if (".jpg" in low or ".jpeg" in low) else ".png"
                fd, path = tempfile.mkstemp(prefix="wcp_goal_", suffix=ext)
                os.close(fd)
                req = urllib.request.Request(image, headers={"User-Agent": "WorldCupPilot/1.0"})
                with urllib.request.urlopen(req, timeout=8) as r, open(path, "wb") as f:
                    f.write(r.read())
                return path
            except Exception as e:
                print(f"[warn] toast image download: {e}")
        return default

    def set_live(self, on):
        """Reflect live state: blinking 🔴 LIVE window title + taskbar icon badge."""
        if not _is_windows():
            return True
        try:
            start_live_blink() if on else stop_live_blink()   # blinks title + taskbar overlay
        except Exception as e:
            print(f"[warn] set_live: {e}")
        return True

    def notify(self, title, body, image=""):
        """Show a native desktop notification. Cross-platform."""
        try:
            if sys.platform == "win32":
                self._notify_windows(title, body, image)
            elif sys.platform == "darwin":
                self._notify_macos(title, body)
            else:
                self._notify_linux(title, body, image)
        except Exception as e:
            print(f"[warn] notify: {e}")
        return True

    def _notify_macos(self, title, body):
        """macOS notification via AppleScript."""
        # The 'image' parameter is not easily supported in simple AppleScript notifications.
        # The notification will use the icon of the running application.
        script = f'display notification "{body}" with title "{title}"'
        subprocess.run(["osascript", "-e", script], check=False)

    def _notify_linux(self, title, body, image):
        """Linux notification via notify-send."""
        icon_path = self._toast_image(image)
        # Check if notify-send is available
        import shutil
        if not shutil.which("notify-send"):
            print("[warn] notify-send command not found, cannot show notification.")
            return

        command = ["notify-send", title, body, "-i", icon_path, "-a", "World Cup"]
        subprocess.run(command, check=False)

    def _notify_windows(self, title, body, image):
        """Windows toast notification via PowerShell (no extra pip deps).
        `image` (optional): a photo to show in front instead of the app icon."""
        try:
            # ToastImageAndText02 → image shown in front (appLogoOverride)
            ps = (
                "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,"
                "ContentType=WindowsRuntime] | Out-Null;"
                "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
                "[Windows.UI.Notifications.ToastTemplateType]::ToastImageAndText02);"
                "$img=$t.GetElementsByTagName('image').Item(0);"
                "$img.SetAttribute('src',$env:WCP_ICON); $img.SetAttribute('placement','appLogoOverride');"
                "$x=$t.GetElementsByTagName('text');"
                "$x.Item(0).AppendChild($t.CreateTextNode($env:WCP_TITLE)) | Out-Null;"
                "$x.Item(1).AppendChild($t.CreateTextNode($env:WCP_BODY)) | Out-Null;"
                "$toast=[Windows.UI.Notifications.ToastNotification]::new($t);"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
                "'World Cup').Show($toast);"
            )
            icon_uri = "file:///" + self._toast_image(image).replace("\\", "/")
            env = dict(os.environ, WCP_TITLE=str(title), WCP_BODY=str(body), WCP_ICON=icon_uri)
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                env=env, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            print(f"[warn] _notify_windows: {e}")


from concurrent.futures import ThreadPoolExecutor


def _fetch_team_data(team_id, base_url):
    """지정된 팀 ID에 대한 데이터를 가져오는 워커 함수."""
    try:
        urllib.request.urlopen(f"{base_url}/api/team?id={team_id}", timeout=90).read()
    except Exception as e:
        print(f"[warn] prebuild failed for team {team_id}: {e}")


def prebuild_static():
    """Warm + persist all static team data (country, squad, photos, club) in the
    background so detail popups open instantly. Permanent cache → runs once for real;
    later launches find everything cached and just zip through."""
    base = f"http://{HOST}:{PORT}"
    try:
        print("[info] Pre-building static data cache...")
        with urllib.request.urlopen(f"{base}/api/matches", timeout=15) as r:
            matches = json.loads(r.read()).get("matches", [])
    except Exception as e:
        print(f"[warn] prebuild failed to get matches: {e}")
        return

    unique_ids = set()
    for m in matches:
        for side in ("home", "away"):
            tid = (m.get(side) or {}).get("id")
            if tid:
                unique_ids.add(tid)

    # ThreadPoolExecutor를 사용하여 병렬로 팀 데이터를 가져옵니다.
    # max_workers=4는 외부 API의 속도 제한을 고려한 설정입니다.
    with ThreadPoolExecutor(max_workers=4) as executor:
        executor.map(lambda tid: _fetch_team_data(tid, base), unique_ids)
    print(f"[info] Pre-build complete for {len(unique_ids)} teams.")


def main():
    threading.Thread(target=serve, daemon=True).start()
    if not wait_until_ready():
        print("[error] Server failed to start. Exiting.")
        return
    threading.Thread(target=prebuild_static, daemon=True).start() # 서버가 준비된 후에 캐시 빌드를 시작합니다.
    webview.create_window(
        title="World Cup",
        url=f"http://{HOST}:{PORT}/",
        width=1680, height=1040, min_size=(1024, 680),
        maximized=False,
        background_color="#050505", confirm_close=False,
        js_api=Api(),
    )
    # Windows: gui defaults to EdgeChromium (WebView2); the taskbar icon comes
    # from the .exe (icon.ico). set_app_icon is a no-op kept for parity.
    # macOS/Linux에서는 기본 GUI(WebKit/GTK)를 사용하도록 gui 파라미터를 제거합니다.
    if _is_windows():
        webview.start(set_app_icon, gui="edgechromium")
    else:
        webview.start(set_app_icon)


if __name__ == "__main__":
    main()
