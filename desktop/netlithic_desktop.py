"""Netlithic desktop app (TEST NETWORK): wallet + your own node + a Start helping button.

What runs, all on the user's PC:
  * a node (this same program started again with `--run-node`) that keeps a copy of
    the test network, synced from the public seed node
  * a small local web server on 127.0.0.1 that shows the beginner wallet (same
    screens and backup code as the web wallet) plus an "Earning" card
  * a window (pywebview) showing that page, or the system browser if pywebview is missing

The wallet's secret key is created inside the window, encrypted with the user's
password and kept only in the window's local storage on this PC. The Python side
only ever sees the ADDRESS. The node's admin token is random per run, in memory
only, and is never written to disk or shown.
TEST NETWORK ONLY: test coins have no value.
"""
import argparse
import json
import logging
import logging.handlers
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

FROZEN = getattr(sys, "frozen", False)
CODE_ROOT = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, CODE_ROOT)
import netlithic_version  # noqa: E402
import webwallet  # noqa: E402

DESKTOP_STATIC = os.path.join(CODE_ROOT, "desktop", "static")
PUBLIC_SITE = os.environ.get("NETLITHIC_PUBLIC_SITE", "https://testnet.netlithicchain.com").rstrip("/")
# Public seed nodes, tried in order. NETLITHIC_SEED_URLS (comma-separated) overrides;
# the old single NETLITHIC_SEED_URL still works. Node 1 = netlithic-testnet-1,
# node 2 = netlithic-testnet-2 (relay only).
DEFAULT_SEEDS = ["http://137.184.199.238:8000", "http://157.230.63.97:8000"]


def parse_seeds(value):
    """'a,b' or ['a', 'b'] -> ['a', 'b'] (trimmed, no trailing /, no duplicates)."""
    items = value.split(",") if isinstance(value, str) else list(value or [])
    out = []
    for item in items:
        url = str(item).strip().rstrip("/")
        if url and url not in out:
            out.append(url)
    return out


SEED_NODES = parse_seeds(os.environ.get("NETLITHIC_SEED_URLS") or os.environ.get("NETLITHIC_SEED_URL") or DEFAULT_SEEDS)
SEED_NODE = SEED_NODES[0]
UI_PORT = int(os.environ.get("NETLITHIC_UI_PORT", "47100"))      # fixed so the saved wallet stays found
NODE_PORT = int(os.environ.get("NETLITHIC_NODE_PORT", "47101"))
ADDRESS_RE = re.compile(r"^[0-9a-f]{32}$")
EXPLORER_PATH = re.compile(r"^/explorer(/[A-Za-z0-9._~%-]*)*/?$")
SYNC_SLACK = 1
# Pause between blocks. Test-network difficulty is very low right now, so without a
# pause one PC could add hundreds of empty blocks a minute. The policy is a founder decision.
MINE_PACE_SECONDS = float(os.environ.get("NETLITHIC_MINE_PACE", "60"))  # within one block of the seed counts as "up to date"


def default_data_dir(platform=None, env=None, home=None):
    """Where the app keeps its wallet and chain copy (never inside the app itself):
    Windows %LOCALAPPDATA%\\Netlithic, macOS ~/Library/Application Support/Netlithic,
    Linux ~/.local/share/Netlithic (or $XDG_DATA_HOME/Netlithic)."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    home = home or os.path.expanduser("~")
    if platform.startswith("win"):
        base = env.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    elif platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
    else:
        base = env.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    return os.path.join(base, "Netlithic", "testnet")


def node_command(data_dir, port, seeds):
    """How to start the node: this exe again when frozen, else netlithic.py."""
    if FROZEN:
        base = [sys.executable, "--run-node"]
    else:
        base = [sys.executable, os.path.abspath(__file__), "--run-node"]
    return base + ["--parent-pid", str(os.getpid()), str(port), "--data-dir", data_dir, "--host", "127.0.0.1"] + \
        [arg for seed in parse_seeds(seeds) for arg in ("--peer", seed)]


class DesktopUpstream(webwallet.Upstream):
    """Like the web wallet's upstream, but never forwards a client IP header to the
    public site (the app is the only client, and a made-up header would be noise)."""

    def call(self, url, payload=None, headers=None):
        headers = {k: v for k, v in (headers or {}).items() if k.lower() != "x-forwarded-for"}
        return super().call(url, payload, headers)


class HelpStats:
    """'Helping' stats kept only on this computer (a small JSON file in the data
    folder): time spent helping today and in total, and blocks found. Holds no
    keys, only numbers. Test coins have no value."""

    MAX_GAP = 120  # a sleep/suspend gap longer than this isn't counted as helping

    def __init__(self, path, clock=time.time):
        self.path, self.clock = path, clock
        self.lock = threading.Lock()
        self.total, self.days, self.blocks = 0.0, {}, 0
        self._last, self._saved = None, 0.0
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            self.total = max(0.0, float(d.get("total_seconds", 0)))
            self.blocks = max(0, int(d.get("blocks_found", 0)))
            days = d.get("days") if isinstance(d.get("days"), dict) else {}
            self.days = {k: max(0.0, float(v)) for k, v in days.items()
                         if isinstance(k, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", k)}
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _today(self, t):
        return time.strftime("%Y-%m-%d", time.localtime(t))

    def tick(self, running):
        """Call often. Counts the time since the last call while helping was on."""
        with self.lock:
            now = self.clock()
            if self._last is not None:
                gap = now - self._last
                if 0 < gap <= self.MAX_GAP:
                    self.total += gap
                    day = self._today(now)
                    self.days[day] = self.days.get(day, 0.0) + gap
            self._last = now if running else None
            if now - self._saved > 30:
                self._save(now)

    def add_block(self):
        with self.lock:
            self.blocks += 1
            self._save(self.clock())

    def flush(self):
        with self.lock:
            self._save(self.clock())

    def _save(self, now):
        self._saved = now
        keep = sorted(self.days)[-60:]  # only the last 60 days are kept
        self.days = {k: self.days[k] for k in keep}
        data = {"total_seconds": round(self.total, 1), "blocks_found": self.blocks,
                "days": {k: round(v, 1) for k, v in self.days.items()}}
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("couldn't save helping stats: %s", type(e).__name__)

    def snapshot(self):
        with self.lock:
            today = self.days.get(self._today(self.clock()), 0.0)
            return {"today_seconds": int(today), "total_seconds": int(self.total),
                    "today": duration_words(today), "total": duration_words(self.total),
                    "blocks_found_total": self.blocks}


def upgrade_notice(seed_status, own_version=None):
    """Plain-words 'upgrade needed' line when the seed says this app is too
    old (its min_peer_version, or a newer major/minor release). Review F3."""
    if not isinstance(seed_status, dict):
        return ""
    own = own_version or netlithic_version.VERSION
    need = seed_status.get("min_peer_version")
    if need and netlithic_version.is_outdated(own, need):
        return (f"Update needed: please install Netlithic {need} or newer before block "
                f"{seed_status.get('upgrade_deadline_height', netlithic_version.UPGRADE_DEADLINE_HEIGHT)}.")
    return ""


def duration_words(seconds):
    """3725 -> '1 hour 2 minutes'. Plain words for the Helping card."""
    m = int(seconds) // 60
    h, m = divmod(m, 60)
    if h == 0:
        return "less than a minute" if m == 0 else f"{m} minute{'s' if m != 1 else ''}"
    out = f"{h} hour{'s' if h != 1 else ''}"
    return out + (f" {m} minute{'s' if m != 1 else ''}" if m else "")


class Earner:
    """Owns the local node process and the mining loop. Knows only an address."""

    def __init__(self, local_node, seeds, call, admin_token=None, pace=None, stats=None):
        self.local, self.call = local_node.rstrip("/"), call
        self.stats = stats
        self.seeds = parse_seeds(seeds)
        self.seed = self.seeds[0]  # last seed that answered
        self.seed_status = None  # last /status from a seed (for the upgrade notice)
        self.token = admin_token or secrets.token_urlsafe(32)
        self.address = None
        self.running = False
        self.blocks_found = 0
        self.last_error = ""
        self.lock = threading.Lock()
        self.thread = None
        self.pace = MINE_PACE_SECONDS if pace is None else pace
        self._wake = threading.Event()

    # -- status in plain words
    def heights(self):
        try:
            c1, mine = self.call(self.local + "/status")
            local = int(mine.get("height")) if c1 == 200 else None
        except Exception:
            local = None
        network = self.network_height()
        return local, network

    def network_height(self):
        """Highest height any seed reports. The seed that answered last is asked
        first; if it is down, the others are tried in order (fallback)."""
        order = [self.seed] + [s for s in self.seeds if s != self.seed]
        best = None
        for seed in order:
            try:
                code, net = self.call(seed + "/status")
                h = int(net.get("height")) if code == 200 else None
                if code == 200 and best is None:
                    self.seed_status = net
            except Exception:
                h = None
            if h is not None:
                if best is None:
                    self.seed = seed
                best = h if best is None else max(best, h)
        return best

    def status(self):
        local, network = self.heights()
        if local is None:
            sync, synced = "Starting your node...", False
        elif network is None:
            sync, synced = "Can't reach the test network right now. Check your internet.", False
        elif local + SYNC_SLACK >= network:
            sync, synced = f"Connected and up to date (block {local}).", True
        else:
            sync, synced = f"Catching up with the network: block {local} of {network}.", False
        notice = upgrade_notice(getattr(self, "seed_status", None))
        if notice:
            sync = notice + " " + sync
        out = {"upgrade_needed": bool(notice), "earning": self.running, "address": self.address, "blocks_found": self.blocks_found,
               "synced": synced, "sync": sync, "local_height": local, "network_height": network,
               "message": self.last_error}
        if self.stats is not None:
            self.stats.tick(self.running)
            out["stats"] = self.stats.snapshot()
        return out

    def start(self, address):
        if not ADDRESS_RE.fullmatch(address or ""):
            raise ValueError("That address doesn't look right.")
        if not self.status()["synced"]:
            raise ValueError("Please wait until your node has caught up with the network.")
        with self.lock:
            self.address = address
            if self.running:
                return
            self.running = True
            self.last_error = ""
            self._wake.clear()
            if self.stats is not None:
                self.stats.tick(True)
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()

    def stop(self):
        with self.lock:
            if self.stats is not None and self.running:
                self.stats.tick(True)
                self.stats.tick(False)
                self.stats.flush()
            self.running = False
            self._wake.set()

    def mine_once(self):
        code, res = self.call(self.local + "/mine", {"miner_address": self.address},
                              {"X-Admin-Token": self.token})
        if code == 200:
            self.blocks_found += 1
            if self.stats is not None:
                self.stats.add_block()
            self.last_error = ""
            return True
        # usually someone else found this block first; just try the next one
        self.last_error = "Someone else found that block first. Still trying."
        return False

    def _loop(self):
        while self.running:
            try:
                self.mine_once()
            except Exception:
                self.last_error = "Your node didn't answer. Retrying..."
            if self.stats is not None:
                self.stats.tick(self.running)
            self._wake.wait(self.pace)


def make_desktop_handler(up, earner):
    Base = webwallet.make_handler(up, trust_proxy=False, explorer="/explorer", share_base=PUBLIC_SITE)

    with open(os.path.join(webwallet.STATIC, "index.html"), encoding="utf-8") as f:
        index = build_index(f.read())

    class Handler(Base):
        server_version = "NetlithicDesktop"

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html", "/send"):
                return self.send(200, index, "text/html; charset=utf-8")
            if path in ("/desktop.js", "/desktop.css"):
                ctype = "text/javascript" if path.endswith("js") else "text/css"
                with open(os.path.join(DESKTOP_STATIC, path[1:]), "rb") as f:
                    return self.send(200, f.read(), ctype + "; charset=utf-8")
            if path == "/api/earn/status":
                return self.json(200, earner.status())
            if path == "/explorer" or path.startswith("/explorer/"):
                if not EXPLORER_PATH.match(path):
                    return self.json(404, {"error": "Not found"})
                self.send_response(302)
                self.send_header("Location", PUBLIC_SITE + path)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return Base.do_GET(self)

        def do_POST(self):
            path = urlparse(self.path).path
            if path not in ("/api/earn/start", "/api/earn/stop"):
                return Base.do_POST(self)
            # only our own page may control earning (blocks other websites in the browser)
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://127.0.0.1:{self.server.server_port}",
                                         f"http://localhost:{self.server.server_port}"):
                return self.json(403, {"error": "Not allowed"})
            body = self.read_body()
            if body is None:
                return self.json(400, {"error": "Bad request"})
            if path == "/api/earn/stop":
                earner.stop()
                return self.json(200, earner.status())
            try:
                earner.start(str(body.get("address", "")).strip().lower())
            except ValueError as e:
                return self.json(400, {"error": str(e)})
            return self.json(200, earner.status())

    return Handler


EARN_CARD = """<div class="card earn">
    <h2>Help run the network</h2>
    <p class="label">While this is on, your computer helps keep the test network going. Each block it makes adds 10 test coins (no value) to your wallet.</p>
    <p class="label"><b>Test coins have no value.</b> They are for practice on the test network only.</p>
    <button id="e-toggle" class="big earn-btn">Start helping</button>
    <p id="e-sync" class="label">Starting your node...</p>
    <p class="label">Blocks found by this computer: <b id="e-blocks">0</b></p>
    <div class="stats" id="e-stats">
      <div><span class="label">Helping today</span><b id="e-today">0 minutes</b></div>
      <div><span class="label">Helping in total</span><b id="e-total">0 minutes</b></div>
      <div><span class="label">Blocks found (all time)</span><b id="e-blocks-all">0</b></div>
      <div><span class="label">Points earned</span><b id="e-points">0</b></div>
    </div>
    <p id="e-next" class="label"></p>
    <p class="label">Points come from blocks with transfers in them and from sending coins to others, so they can stay at 0 while you help.</p>
    <p id="e-note" class="label"></p>
  </div>
  <div class="card">"""


def build_index(html):
    """The web wallet page plus the desktop Earning card and script."""
    out = html.replace("<title>Netlithic Wallet (Test Network)</title>", "<title>Netlithic (Test Network)</title>", 1)
    out = out.replace("<h1>Netlithic Wallet</h1>", "<h1>Netlithic</h1>", 1)
    out = out.replace('<link rel="stylesheet" href="/wallet.css">',
                      '<link rel="stylesheet" href="/wallet.css">\n<link rel="stylesheet" href="/desktop.css">', 1)
    marker = '\n  <div class="card">\n    <p class="small" id="h-stay"'
    out = out.replace(marker, '\n  ' + EARN_CARD + '\n    <p class="small" id="h-stay"', 1)
    out = out.replace("a secret key kept only in this browser", "a secret key kept only on this computer")
    # The desktop window stays open, so "Stay unlocked for 15 minutes" isn't offered there.
    out = re.sub(r'\s*<label class="check" id="u-stay-row">.*?</label>', "", out, count=1, flags=re.S)
    out = out.replace('<script src="/wallet.js"></script>',
                      '<script src="/wallet.js"></script>\n<script src="/desktop.js"></script>', 1)
    for needed in ("/desktop.js", "/desktop.css", 'id="e-toggle"', "TEST NETWORK"):
        if needed not in out:
            raise RuntimeError("desktop page could not be built from the web wallet page")
    out = out.replace("</body>", f'<p class="small" id="app-version">Netlithic {netlithic_version.VERSION}</p>\n</body>', 1)
    return out


BACKUP_KEYS = {"format", "version", "address", "kdf", "iterations", "salt", "iv", "ciphertext"}
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def check_backup(text):
    """Accept only an ENCRYPTED wallet backup (the web wallet's format). Anything
    else, such as a bare key, is refused so it can never be written out by mistake."""
    if not isinstance(text, str) or len(text) > 8192:
        raise ValueError("That isn't a Netlithic backup file.")
    blob = json.loads(text)
    if (not isinstance(blob, dict) or set(blob) != BACKUP_KEYS or blob.get("format") != "netlithic-wallet-backup"
            or blob.get("kdf") != "PBKDF2-SHA256" or not ADDRESS_RE.fullmatch(str(blob.get("address")))
            or not re.fullmatch(r"[0-9a-f]{64,}", str(blob.get("ciphertext")))):
        raise ValueError("That isn't a Netlithic backup file.")
    return blob


def documents_dir():
    d = os.path.join(os.path.expanduser("~"), "Documents")
    return d if os.path.isdir(d) else os.path.expanduser("~")


class WindowApi:
    """Functions the desktop window's page may call (pywebview js_api).

    pywebview walks every PUBLIC attribute of this object when the page loads and
    recurses into objects it finds. A public reference to the Window made it walk
    the window's own properties (size, position...), which ask the GUI thread for
    answers while the GUI thread is waiting: the window froze at startup. So every
    attribute here is private (leading _) and only plain methods are public."""

    def __init__(self, ask_save_path=None, set_clipboard=None, show_notification=None):
        self._window = None
        self._notify = show_notification or _os_notify
        self._ask = ask_save_path or self._native_save_dialog
        self._clip = set_clipboard or _set_clipboard

    def _native_save_dialog(self, name):
        import webview
        kind = getattr(getattr(webview, "FileDialog", None), "SAVE", None) or webview.SAVE_DIALOG
        res = self._window.create_file_dialog(kind, directory=documents_dir(), save_filename=name,
                                             file_types=("Backup file (*.json)",))
        if isinstance(res, (list, tuple)):
            res = res[0] if res else None
        return res

    def save_backup(self, text, name="netlithic-backup.json"):
        try:
            check_backup(text)
        except Exception:
            return {"ok": False, "error": "That isn't a Netlithic backup file."}
        name = SAFE_NAME.sub("", str(name or ""))[:80] or "netlithic-backup.json"
        if not name.lower().endswith(".json"):
            name += ".json"
        path = self._ask(name)
        if not path:
            log.info("backup save cancelled")
            return {"ok": False}  # the user cancelled
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            log.warning("backup save failed: %s", type(e).__name__)
            return {"ok": False, "error": "Couldn't save the file there. Try another folder."}
        log.info("backup file saved")
        return {"ok": True, "path": str(path)}

    def notify(self, title, body):
        """Best-effort OS notification. Returns False when the computer can't show
        one, and the page then shows it inside the window instead."""
        title, body = clean_note(title, 60), clean_note(body, 200)
        if not body:
            return False
        try:
            return bool(self._notify(title, body))
        except Exception as e:
            log.warning("notification failed: %s", type(e).__name__)
            return False

    def copy_text(self, text):
        text = str(text or "")
        if not ADDRESS_RE.fullmatch(text):
            return False  # only addresses are ever copied
        try:
            self._clip(text)
            return True
        except Exception as e:
            log.warning("copy failed: %s", type(e).__name__)
            return False


def clean_note(text, limit):
    """Notification text: printable characters only, short."""
    return re.sub(r"[\x00-\x1f\x7f\"\\]", "", str(text or ""))[:limit].strip()


def _os_notify(title, body):
    """Show a notification with what the OS already has. No shell is used and the
    text is passed as plain arguments."""
    if sys.platform == "darwin":
        script = f'display notification "{body}" with title "{title}"'
        return subprocess.run(["osascript", "-e", script], timeout=5).returncode == 0
    if sys.platform.startswith("linux"):
        import shutil
        exe = shutil.which("notify-send")
        return bool(exe) and subprocess.run([exe, "--app-name=Netlithic", title, body], timeout=5).returncode == 0
    if os.name == "nt":
        return False  # no built-in notifier without extra packages: shown in the window instead
    return False


def _set_clipboard(text):
    if os.name == "nt":
        subprocess.run(["clip.exe"], input=text.encode("utf-16-le"), check=True, timeout=5,
                       creationflags=0x08000000)
    else:  # Linux/macOS (only for trying the app; Windows is the target)
        for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
            try:
                subprocess.run(cmd, input=text.encode(), check=True, timeout=5)
                return
            except (OSError, subprocess.SubprocessError):
                continue
        raise OSError("no clipboard tool")


log = logging.getLogger("netlithic.desktop")


def setup_logging(log_dir):
    """%LOCALAPPDATA%\\Netlithic\\netlithic.log. Only events and errors: never keys,
    backup contents, passwords or the admin token."""
    os.makedirs(log_dir, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(os.path.join(log_dir, "netlithic.log"),
                                             maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(h)
    logging.getLogger("pywebview").setLevel(logging.INFO)
    return h


HARD_EXIT_SECONDS = 5


def parent_alive(pid):
    """True while the process `pid` (the app window) is still running."""
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
        if not h:
            return False
        try:
            return k.WaitForSingleObject(h, 0) == 0x00000102  # WAIT_TIMEOUT: still running
        finally:
            k.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def watch_parent(pid, interval=1.0, on_gone=None):
    """Node side: if the app window's process disappears (closed, crashed or killed),
    stop this node too, so no Netlithic.exe is left behind."""
    def loop():
        while parent_alive(pid):
            time.sleep(interval)
        (on_gone or (lambda: os._exit(0)))()
    t = threading.Thread(target=loop, name="parent-watch", daemon=True)
    t.start()
    return t


def run_node_mode(argv):
    if len(argv) >= 2 and argv[0] == "--parent-pid":
        watch_parent(int(argv[1]))
        argv = argv[2:]
    import node as node_mod
    node_mod.start(node_mod.parse_args(argv))


def shutdown(earner, server, proc, node_log, hard_exit=True):
    """Stop helping, the local page server and the node; never hang. A timer forces
    the process to end after HARD_EXIT_SECONDS if anything (e.g. a GUI thread) is stuck."""
    if hard_exit:
        t = threading.Timer(HARD_EXIT_SECONDS, lambda: (log.warning("forced exit"), os._exit(0)))
        t.daemon = True
        t.start()
    log.info("shutting down")
    earner.stop()
    threading.Thread(target=server.shutdown, daemon=True).start()
    try:
        server.server_close()
    except Exception:
        pass
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(3)
        except Exception:
            proc.kill()
            try:
                proc.wait(2)
            except Exception:
                pass
    try:
        node_log.close()
    except Exception:
        pass
    log.info("stopped")


def open_window(url, data_dir):
    try:
        import webview
    except Exception as e:
        log.warning("pywebview unavailable (%s); using the system browser", type(e).__name__)
        import webbrowser
        webbrowser.open(url)
        print(f"Netlithic is open in your browser at {url}. Keep this window open while helping.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return
    log.info("opening window (pywebview %s)", getattr(webview, "__version__", "?"))
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    api = WindowApi()
    api._window = webview.create_window("Netlithic (Test Network)", url, width=640, height=900,
                                       min_size=(420, 600), js_api=api)
    def on_closed():
        log.info("window closed")
        # if webview.start() doesn't return promptly, end the app anyway
        t = threading.Timer(HARD_EXIT_SECONDS * 2, lambda: os._exit(0))
        t.daemon = True
        t.start()
    api._window.events.closed += on_closed
    webview.start(private_mode=False, storage_path=os.path.join(data_dir, "window"))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--run-node":
        return run_node_mode(argv[1:])
    p = argparse.ArgumentParser(description="Netlithic desktop app (test network; coins have no value)")
    p.add_argument("--version", action="version", version=netlithic_version.version_string("netlithic-desktop"))
    p.add_argument("--data-dir", default=default_data_dir())
    p.add_argument("--seed", action="append", default=None,
                   help="seed node URL; repeat or comma-separate for several (default: built-in list)")
    p.add_argument("--site", default=PUBLIC_SITE, help="public site for free test coins and the explorer")
    p.add_argument("--ui-port", type=int, default=UI_PORT)
    p.add_argument("--node-port", type=int, default=NODE_PORT)
    p.add_argument("--no-window", action="store_true", help="serve only (for tests and screenshots)")
    a = p.parse_args(argv)
    a.seed = parse_seeds(",".join(a.seed)) if a.seed else SEED_NODES
    os.makedirs(a.data_dir, exist_ok=True)
    setup_logging(os.path.dirname(os.path.abspath(a.data_dir)))
    log.info("starting %s (frozen=%s, ui port %s, node port %s, seed %s)", netlithic_version.VERSION, FROZEN, a.ui_port, a.node_port, a.seed)

    local_node = f"http://127.0.0.1:{a.node_port}"
    up = DesktopUpstream(local_node, a.site)  # free coins come from the public site's /api/drip
    earner = Earner(local_node, a.seed, up.call, stats=HelpStats(os.path.join(a.data_dir, "helping-stats.json")))
    env = {**os.environ, "NETLITHIC_ADMIN_TOKEN": earner.token, "PYTHONUNBUFFERED": "1"}
    node_log = open(os.path.join(a.data_dir, "node.log"), "a", encoding="utf-8")
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: no black console box
    # Reviewed false positive: argv list (no shell), our own executable, and
    # seeds pass parse_seeds() URL validation; env only adds our own token.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
    proc = subprocess.Popen(node_command(os.path.join(a.data_dir, "node"), a.node_port, a.seed),
                            env=env, stdout=node_log, stderr=subprocess.STDOUT, creationflags=flags)
    server = ThreadingHTTPServer(("127.0.0.1", a.ui_port), make_desktop_handler(up, earner))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{a.ui_port}/"
    if a.no_window:
        import signal
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        if a.no_window:
            print(f"Netlithic desktop serving {url}", flush=True)
            while proc.poll() is None:
                time.sleep(1)
        else:
            open_window(url, a.data_dir)
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("app crashed")
        raise
    finally:
        shutdown(earner, server, proc, node_log)
        logging.shutdown()
        if not a.no_window:
            os._exit(0)  # don't wait on leftover GUI/webview threads


if __name__ == "__main__":
    main()
