"""Netlithic easy node: install and run a TEST network node as a background service.

    Linux / macOS:  sh install.sh          Windows:  powershell -File install.ps1
    (both just run:  python easy_node.py install)

What it does, all on YOUR machine:
  1. makes a private Python environment and installs what the node needs
  2. makes a data folder (Linux ~/.local/share/Netlithic/node,
     macOS ~/Library/Application Support/Netlithic/node, Windows %LOCALAPPDATA%\\Netlithic\\node)
  3. registers a background service that starts at login and joins the seed nodes
     (Linux: systemd user service, macOS: launchd agent, Windows: scheduled task)

No wallet or key is created or sent anywhere: keys stay on your machine.
Test coins have no value.  Uninstall:  python easy_node.py uninstall
"""
import argparse
import os
import shutil
import subprocess
import sys
import venv

ROOT = os.path.dirname(os.path.abspath(__file__))
SEEDS = ["http://137.184.199.238:8000", "http://157.230.63.97:8000"]
SERVICE = "netlithic-node"
LABEL = "org.netlithic.node"
NOTE = "Netlithic TEST network - test coins have no value."


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
    # windows: scheduled task at logon, no admin needed
    q = " ".join(f'"{c}"' for c in cmd)
    return (None, q)


def register_commands(plat, svc_path, task_cmd=None):
    if plat == "linux":
        return [["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", SERVICE]]
    if plat == "macos":
        return [["launchctl", "unload", svc_path], ["launchctl", "load", "-w", svc_path]]
    return [["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", "Netlithic Node", "/TR", task_cmd],
            ["schtasks", "/Run", "/TN", "Netlithic Node"]]


def unregister_commands(plat, svc_path):
    if plat == "linux":
        return [["systemctl", "--user", "disable", "--now", SERVICE]]
    if plat == "macos":
        return [["launchctl", "unload", "-w", svc_path]]
    return [["schtasks", "/End", "/TN", "Netlithic Node"], ["schtasks", "/Delete", "/F", "/TN", "Netlithic Node"]]


def plan(args):
    plat = args.platform or platform_name()
    home = args.home or os.path.expanduser("~")
    base = args.base_dir or base_dir(plat, home)
    vdir, data = os.path.join(base, "venv"), os.path.join(base, "node")
    py = sys.executable if args.no_venv else venv_python(vdir, plat)
    cmd = node_command(py, data, args.port, args.host, args.advertise_url)
    rel, text = service_file(plat, cmd, os.path.join(base, "node.log"))
    svc = os.path.join(home, rel) if rel else None
    return dict(platform=plat, base=base, venv=vdir, data=data, python=py, command=cmd,
                service_path=svc, service_text=text)


def say(m=""):
    print(m, flush=True)


def install(args):
    p = plan(args)
    say("=" * 60 + f"\n {NOTE}\n" + "=" * 60)
    say(f"Folder: {p['base']}")
    if args.dry_run:
        say("[dry run] would write service:\n" + p["service_text"])
        return p
    os.makedirs(p["data"], exist_ok=True)
    if not args.no_venv:
        if not os.path.exists(p["python"]):
            say("Setting up a private Python environment (one time, about a minute)...")
            venv.EnvBuilder(with_pip=True).create(p["venv"])
        subprocess.run([p["python"], "-m", "pip", "install", "--quiet", "-r",
                        os.path.join(ROOT, "requirements.txt")], check=True)
    if p["service_path"]:
        os.makedirs(os.path.dirname(p["service_path"]), exist_ok=True)
        with open(p["service_path"], "w", encoding="utf-8") as f:
            f.write(p["service_text"])
    if not args.no_service:
        for c in register_commands(p["platform"], p["service_path"], p["service_text"]):
            subprocess.run(c, check=False)
    say(f"\nDone. Your node runs in the background and joins: {', '.join(SEEDS)}")
    say(f"Check it in a browser: http://127.0.0.1:{args.port}/status")
    say("To remove it later: python easy_node.py uninstall (your data folder is kept).")
    return p


def uninstall(args):
    p = plan(args)
    if not args.dry_run:
        for c in unregister_commands(p["platform"], p["service_path"]):
            subprocess.run(c, check=False)
        if p["service_path"] and os.path.exists(p["service_path"]):
            os.remove(p["service_path"])
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
    return install(a) if a.action == "install" else uninstall(a)


if __name__ == "__main__":
    main()
