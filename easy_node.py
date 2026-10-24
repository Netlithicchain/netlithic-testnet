"""Netlithic easy node: install and run a TEST network node as a background service.

    Linux / macOS:  sh install.sh          Windows:  powershell -File install.ps1
    (both just run:  python easy_node.py install)

What it does, all on YOUR machine:
  1. makes a private Python environment and installs what the node needs
  2. makes a data folder (Linux ~/.local/share/Netlithic/node,
     macOS ~/Library/Application Support/Netlithic/node, Windows %LOCALAPPDATA%\\Netlithic\\node)
  3. registers a background service that starts at login and joins the seed nodes
     (Linux: systemd user service, macOS: launchd agent, Windows: a scheduled task; if Windows will not
     let a normal account create one, a launcher in your own Startup folder instead - no admin needed)

No wallet or key is created or sent anywhere: keys stay on your machine.
Test coins have no value.  Uninstall:  python easy_node.py uninstall
"""
import argparse
import http.client
import os
import shutil
import subprocess
import sys
import time
import venv

ROOT = os.path.dirname(os.path.abspath(__file__))
SEEDS = ["http://137.184.199.238:8000", "http://157.230.63.97:8000"]
SERVICE = "netlithic-node"
LABEL = "org.netlithic.node"
NOTE = "Netlithic TEST network - test coins have no value."
TASK_NAME = "Netlithic Node"
LAUNCHER = "start-node.cmd"   # Windows: tiny file the scheduled task runs; it holds the long command
TR_LIMIT = 261                # schtasks refuses a /TR value longer than this
WAIT_SECONDS = 45             # how long install waits for the node to answer on /status
STARTUP_FILE = "Netlithic Node.cmd"   # Windows no-admin fallback: lives in the per-user Startup folder
DENIED_WORDS = ("access is denied", "access denied", "permission denied", "0x80070005",
                "requested operation requires elevation")
CREATE_NO_WINDOW = 0x08000000        # Windows process-creation flags (numbers, so tests run on Linux)
CREATE_NEW_PROCESS_GROUP = 0x00000200


class InstallError(Exception):
    """A step failed. The text is shown to the person in plain words and the program exits non-zero."""


def run_cmd(cmd):
    """Run a command, never raise. Returns (exit code, its output as text)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", check=False)
    except OSError as e:
        return 127, f"could not start '{cmd[0]}': {e}"
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def platform_name(p=None):
    p = p or sys.platform
    return "windows" if p.startswith("win") else "macos" if p == "darwin" else "linux"


def base_dir(plat, home=None, env=None):
    home = home or os.path.expanduser("~")
    env = os.environ if env is None else env
    if plat == "windows":
        return os.path.join(env.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local"), "Netlithic")
    if plat == "macos":
        return os.path.join(home, "Library", "Application Support", "Netlithic")
    return os.path.join(env.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share"), "Netlithic")


def venv_python(vdir, plat):
    return os.path.join(vdir, "Scripts", "pythonw.exe" if plat == "windows" else "python") if plat == "windows" \
        else os.path.join(vdir, "bin", "python")


def node_command(py, data_dir, port, host, advertise=None, seeds=SEEDS):
    cmd = [py, os.path.join(ROOT, "node.py"), str(port), "--data-dir", data_dir, "--host", host]
    for s in seeds:
        cmd += ["--peer", s]
    if advertise:
        cmd += ["--advertise-url", advertise]
    return cmd


def service_file(plat, cmd, log_path):
    """Return (path_relative_to_home_or_None, text) for the service definition."""
    if plat == "linux":
        q = " ".join(f'"{c}"' if " " in c else c for c in cmd)
        return (os.path.join(".config", "systemd", "user", SERVICE + ".service"),
                f"[Unit]\nDescription={NOTE}\nAfter=network-online.target\n\n"
                f"[Service]\nExecStart={q}\nRestart=on-failure\nRestartSec=10\n\n"
                f"[Install]\nWantedBy=default.target\n")
    if plat == "macos":
        args = "".join(f"    <string>{c}</string>\n" for c in cmd)
        return (os.path.join("Library", "LaunchAgents", LABEL + ".plist"),
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>\n'
                f"  <key>Label</key><string>{LABEL}</string>\n  <key>ProgramArguments</key><array>\n{args}"
                "  </array>\n  <key>RunAtLoad</key><true/>\n  <key>KeepAlive</key><true/>\n"
                f"  <key>StandardOutPath</key><string>{log_path}</string>\n"
                f"  <key>StandardErrorPath</key><string>{log_path}</string>\n</dict></plist>\n")
    # windows: scheduled task at logon, no admin needed. The task runs a short launcher file
    # (see launcher_text); the long command lives in that file, not in schtasks' /TR.
    return (None, launcher_text(cmd))


def launcher_text(cmd):
    """Text of the Windows launcher (.cmd). It starts the node and returns at once."""
    def q(c):
        return '"' + str(c).replace("%", "%%") + '"'     # % is special inside .cmd files
    lines = ["@echo off"]
    if any(ord(ch) > 127 for ch in " ".join(cmd)):
        lines.append("chcp 65001 >nul")                   # folder names with accents
    lines += [f"rem {NOTE}", 'start "" /B ' + " ".join(q(c) for c in cmd)]
    return "\r\n".join(lines) + "\r\n"


def task_command(launcher_path):
    """The schtasks /TR value: just the launcher path, quoted so folders with spaces work."""
    return '"' + launcher_path + '"'


def short_path(p):
    """Windows 8.3 short form of an existing folder (no spaces, shorter). Elsewhere: unchanged."""
    if os.name != "nt":
        return p
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetShortPathNameW(p, buf, len(buf))
        return buf.value if 0 < n < len(buf) else p
    except Exception:
        return p


def fallback_dirs(home):
    return [os.path.join(home, ".netlithic")]


def choose_launcher(base, home):
    """Pick a launcher path whose /TR value fits schtasks' limit: the Netlithic folder first, then its
    8.3 short name, then a short folder in the user profile. Fails clearly if none fits."""
    os.makedirs(base, exist_ok=True)
    cands = [os.path.join(base, LAUNCHER), os.path.join(short_path(base), LAUNCHER)]
    for d in fallback_dirs(home):
        os.makedirs(d, exist_ok=True)
        cands += [os.path.join(d, LAUNCHER), os.path.join(short_path(d), LAUNCHER)]
    for c in cands:
        if len(task_command(c)) <= TR_LIMIT:
            return c
    raise InstallError(
        f"The folder name is too long for Windows' scheduled tasks (the limit is {TR_LIMIT} characters, "
        f"and even the shortest place I tried was longer). Nothing was started.\n"
        f"Try again with a shorter folder, for example:  python easy_node.py install --base-dir C:\\Netlithic")


def startup_dir(home=None, env=None):
    """The per-user Windows Startup folder (no admin needed to write there). Uses %APPDATA%."""
    home = home or os.path.expanduser("~")
    env = os.environ if env is None else env
    roaming = env.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    return os.path.join(roaming, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def startup_text(launcher_path):
    """Text of the Startup-folder file: it just calls start-node.cmd (the path is quoted: spaces are fine)."""
    lines = ["@echo off"]
    if any(ord(ch) > 127 for ch in launcher_path):
        lines.append("chcp 65001 >nul")
    lines += [f"rem {NOTE} Starts your Netlithic node when you log in.",
              'call "' + launcher_path.replace("%", "%%") + '"']
    return "\r\n".join(lines) + "\r\n"


def is_permission_error(out):
    o = (out or "").lower()
    return any(w in o for w in DENIED_WORDS)


def ps_quote(s):
    """A PowerShell single-quoted string (quotes doubled, including the curly ones PowerShell also accepts)."""
    return "'" + "".join(ch * 2 if ch in "'\u2018\u2019\u201a\u201b" else ch for ch in s) + "'"


def stop_node_command(data_dir):
    """PowerShell command that stops any Python process running THIS install's node (matched by its data
    folder in the command line; this installer's own process is skipped). Prints the ids it stopped."""
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -and "
          f"$_.ProcessId -ne {os.getpid()} -and "
          f"$_.CommandLine.IndexOf({ps_quote(data_dir)}, [StringComparison]::OrdinalIgnoreCase) -ge 0 }} | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; $_.ProcessId }")
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps]


def stop_running_node(p):
    """Best effort: stop an already-running node of this install. Returns (worked, plain-words note)."""
    rc, out = run_cmd(stop_node_command(p["data"]))
    if rc != 0:
        return False, out or f"exit code {rc}"
    return True, (f"stopped process {out.split()[0]}" if out.split() else "nothing was running")


def start_detached(path):
    """Run a .cmd file in the background with no window, and without waiting for it."""
    flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    kw = {"creationflags": flags} if flags else {}
    try:
        subprocess.Popen([path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, **kw)
    except OSError as e:
        raise InstallError(f"Could not start your node from {path}: {e}")


def register_commands(plat, svc_path, task_cmd=None):
    if plat == "linux":
        return [["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", SERVICE]]
    if plat == "macos":
        return [["launchctl", "unload", svc_path], ["launchctl", "load", "-w", svc_path]]
    return [["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", TASK_NAME, "/TR", task_cmd],
            ["schtasks", "/End", "/TN", TASK_NAME],      # a re-install: stop the old copy first (may not be running)
            ["schtasks", "/Run", "/TN", TASK_NAME]]


def unregister_commands(plat, svc_path):
    if plat == "linux":
        return [["systemctl", "--user", "disable", "--now", SERVICE]]
    if plat == "macos":
        return [["launchctl", "unload", "-w", svc_path]]
    return [["schtasks", "/End", "/TN", TASK_NAME], ["schtasks", "/Delete", "/F", "/TN", TASK_NAME]]


def plan(args):
    plat = args.platform or platform_name()
    home = args.home or os.path.expanduser("~")
    base = args.base_dir or base_dir(plat, home)
    vdir, data = os.path.join(base, "venv"), os.path.join(base, "node")
    py = sys.executable if args.no_venv else venv_python(vdir, plat)
    cmd = node_command(py, data, args.port, args.host, args.advertise_url)
    rel, text = service_file(plat, cmd, os.path.join(base, "node.log"))
    svc = os.path.join(home, rel) if rel else None
    launcher = os.path.join(base, LAUNCHER) if plat == "windows" else None
    startup = os.path.join(startup_dir(home), STARTUP_FILE) if plat == "windows" else None
    return dict(platform=plat, base=base, venv=vdir, data=data, python=py, command=cmd,
                service_path=svc, service_text=text, home=home, launcher_path=launcher,
                task_cmd=task_command(launcher) if launcher else None, startup_path=startup)


def say(m=""):
    print(m, flush=True)


def node_answers(port):
    """One quick look: does something answer http://127.0.0.1:<port>/status?"""
    try:
        c = http.client.HTTPConnection("127.0.0.1", int(port), timeout=2)
        try:
            c.request("GET", "/status")
            return c.getresponse().status == 200
        finally:
            c.close()
    except (OSError, http.client.HTTPException):
        return False


def wait_for_node(port, seconds=WAIT_SECONDS):
    """True once the node answers http://127.0.0.1:<port>/status, False if it stays silent."""
    end = time.time() + seconds
    while True:
        if node_answers(port):
            return True
        if time.time() >= end:
            return False
        time.sleep(1)


def _failed(what, cmd, rc, out):
    return InstallError(f"{what}\n  command: {subprocess.list2cmdline(cmd)}\n  exit code: {rc}\n  it said: {out or '(nothing)'}")


def start_service(p):
    """Register and start the background service. Raises InstallError on the first real failure.
    Returns "task" (Windows scheduled task, or the Linux/Mac service) or "startup" (Windows, no admin:
    a launcher in the user's Startup folder)."""
    if p["platform"] == "windows":
        return _start_windows(p)
    for c in register_commands(p["platform"], p["service_path"], p["task_cmd"]):
        rc, out = run_cmd(c)
        if rc != 0 and c[:2] != ["launchctl", "unload"]:    # unload fails harmlessly when nothing was loaded
            raise _failed("The background service could not be set up, so your node was NOT started.", c, rc, out)
    return "task"


def _remove_startup_launcher(p):
    f = p.get("startup_path")
    if f and os.path.exists(f):
        os.remove(f)


def _start_windows(p):
    note = "Windows could not set up the background task, so your node was NOT started."
    stop_running_node(p)                                       # re-install: never leave two copies running
    create, end, run = register_commands("windows", None, p["task_cmd"])
    rc, out = run_cmd(create)
    if rc != 0:
        if not is_permission_error(out):
            raise _failed(note, create, rc, out)
        return _start_from_startup_folder(p, create, rc, out)
    try:
        _remove_startup_launcher(p)                            # an earlier no-admin install: one method only
    except OSError as e:
        raise InstallError(f"The task was created, but I could not remove the older Startup-folder file "
                           f"{p['startup_path']} ({e}). Delete it yourself, or your node would start twice.")
    run_cmd(end)                                               # may not be running: fine
    rc, out = run_cmd(run)
    if rc != 0:
        raise _failed(note, run, rc, out)
    say("Installed as a scheduled task (it starts when you log in).")
    return "task"


def _start_from_startup_folder(p, create, rc, out):
    """No-admin fallback: the account is not allowed to create a scheduled task."""
    say("Windows would not let this account create a scheduled task, so I added the node to your Startup "
        "folder instead. It starts when you log in. No admin needed.")
    run_cmd(["schtasks", "/End", "/TN", TASK_NAME])             # an older admin-made task, if any: best effort
    rc2, _ = run_cmd(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])
    if rc2 != 0 and run_cmd(["schtasks", "/Query", "/TN", TASK_NAME])[0] == 0:
        say("Note: an older scheduled task named 'Netlithic Node' still exists and this account cannot remove it. "
            "Run 'python easy_node.py uninstall' as administrator once, or your node may try to start twice.")
    try:
        os.makedirs(os.path.dirname(p["startup_path"]), exist_ok=True)
        with open(p["startup_path"], "w", encoding="utf-8", newline="") as f:
            f.write(startup_text(p["launcher_path"]))
    except OSError as e:
        raise InstallError(
            f"Windows would not let this account create a scheduled task, and I could not write to your Startup "
            f"folder either, so your node was NOT started.\n  Startup file: {p['startup_path']}\n  problem: {e}\n"
            f"  scheduled task said: {out or '(nothing)'}\n"
            f"You can run the installer as administrator instead (right-click PowerShell, 'Run as administrator').")
    start_detached(p["launcher_path"])
    return "startup"


def _build_env(p, args):
    if not args.no_venv:
        if not os.path.exists(p["python"]):
            say("Setting up a private Python environment (one time, about a minute)...")
            try:
                venv.EnvBuilder(with_pip=True).create(p["venv"])
            except Exception as e:
                raise InstallError(f"Could not create the private Python environment in {p['venv']}: {e}")
        if not os.path.exists(p["python"]):
            raise InstallError(f"The private Python environment was not created (missing {p['python']}). "
                               "Nothing was started.")
        c = [p["python"], "-m", "pip", "install", "--quiet", "-r", os.path.join(ROOT, "requirements.txt")]
        rc, out = run_cmd(c)
        if rc != 0:
            raise _failed("Could not install what the node needs (pip failed), so your node was NOT started.", c, rc, out)


def install(args):
    p = plan(args)
    say("=" * 60 + f"\n {NOTE}\n" + "=" * 60)
    say(f"Folder: {p['base']}")
    if args.dry_run:
        say("[dry run] would write service:\n" + p["service_text"])
        return p
    os.makedirs(p["data"], exist_ok=True)
    _build_env(p, args)
    if p["service_path"]:
        os.makedirs(os.path.dirname(p["service_path"]), exist_ok=True)
        with open(p["service_path"], "w", encoding="utf-8") as f:
            f.write(p["service_text"])
    if p["platform"] == "windows":
        p["launcher_path"] = choose_launcher(p["base"], p["home"])
        p["task_cmd"] = task_command(p["launcher_path"])
        os.makedirs(os.path.dirname(p["launcher_path"]), exist_ok=True)
        with open(p["launcher_path"], "w", encoding="utf-8", newline="") as f:
            f.write(p["service_text"])
    method = None
    if not args.no_service:
        method = start_service(p)
        say(f"Started. Waiting up to {WAIT_SECONDS} seconds for your node to answer...")
        if not wait_for_node(args.port):
            where = f" (the file is {p['startup_path']})" if method == "startup" else ""
            raise InstallError(
                f"The background service was created{where}, but your node did not answer at "
                f"http://127.0.0.1:{args.port}/status within {WAIT_SECONDS} seconds, so I can't say it works.\n"
                f"It may just be slow to start: try that address in a browser in a minute. If it stays empty, run "
                f"'python easy_node.py uninstall' and then install again, and send me the text above.")
    say(f"\nDone. Your node runs in the background and joins: {', '.join(SEEDS)}")
    if method == "startup":
        say("It will start again each time you log in (from your Startup folder).")
    say(f"Check it in a browser: http://127.0.0.1:{args.port}/status")
    say("To remove it later: python easy_node.py uninstall (your data folder is kept).")
    return p


def uninstall(args):
    p = plan(args)
    if not args.dry_run:
        problems = []
        for c in unregister_commands(p["platform"], p["service_path"]):
            rc, out = run_cmd(c)
            if rc != 0:
                if p["platform"] == "windows" and c[1] == "/Delete":
                    # "not found" is fine (already gone); anything else only counts if the task is still there
                    qrc, _ = run_cmd(["schtasks", "/Query", "/TN", TASK_NAME])
                    if qrc == 0:
                        problems.append(_failed("Windows would not remove the background task, so your node may "
                                                "still start at login.", c, rc, out))
                # other 'stop' steps fail harmlessly when the service was not running
        if p["platform"] == "windows":
            try:
                _remove_startup_launcher(p)
            except OSError as e:
                problems.append(InstallError(f"I could not remove your Startup-folder file {p['startup_path']} ({e}), "
                                             "so your node may still start at login. Delete that file yourself."))
            worked, note = stop_running_node(p)
            if node_answers(args.port):
                problems.append(InstallError(
                    f"Your node is still running and answering at http://127.0.0.1:{args.port}/status "
                    f"({'could not stop it: ' + note if not worked else note}). Close 'pythonw.exe' in Task Manager, "
                    "or restart your PC, then run uninstall again."))
        if problems:
            raise InstallError("\n\n".join(str(x) for x in problems))
        for f in (p["service_path"], p["launcher_path"]):
            if f and os.path.exists(f):
                os.remove(f)
        if p["platform"] == "windows":
            for d in fallback_dirs(p["home"]):
                for f in (os.path.join(d, LAUNCHER), os.path.join(short_path(d), LAUNCHER)):
                    if os.path.exists(f):
                        os.remove(f)
        if not args.keep_venv and os.path.isdir(p["venv"]):
            shutil.rmtree(p["venv"])
    say(f"Service removed. Data kept in {p['data']} (delete it yourself if you want).")
    return p


def main(argv=None):
    ap = argparse.ArgumentParser(description="Install a Netlithic test network node as a service.")
    ap.add_argument("action", choices=["install", "uninstall", "show"], nargs="?", default="install")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to let other nodes connect to you")
    ap.add_argument("--advertise-url", default=None)
    ap.add_argument("--platform", choices=["linux", "macos", "windows"], default=None)
    ap.add_argument("--home", default=None)
    ap.add_argument("--base-dir", default=None)
    ap.add_argument("--no-venv", action="store_true")
    ap.add_argument("--no-service", action="store_true", help="set up files only, don't start a service")
    ap.add_argument("--keep-venv", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.action == "show":
        p = plan(a)
        say(p["service_text"])
        return p
    try:
        return install(a) if a.action == "install" else uninstall(a)
    except InstallError as e:
        print(f"\nERROR: {e}\n\nYour node was NOT started. Fix the problem above and run the installer again.",
              file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
