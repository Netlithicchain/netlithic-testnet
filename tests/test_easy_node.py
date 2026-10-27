import argparse
import os
import plistlib

import pytest

import easy_node as en
import node


@pytest.mark.parametrize("plat,part", [("linux", ".local/share/Netlithic"),
                                       ("macos", "Library/Application Support/Netlithic"),
                                       ("windows", "Netlithic")])
def test_plan_per_os(tmp_path, plat, part):
    p = en.main(["show", "--platform", plat, "--home", str(tmp_path)])
    assert part.replace("/", os.sep) in p["base"]
    for s in en.SEEDS:
        assert s in p["service_text"]
    assert "wallet" not in p["service_text"].lower() and "token" not in p["service_text"].lower()


def test_seeds_are_the_two_testnet_nodes():
    assert en.SEEDS == ["http://137.184.199.238:8000", "http://157.230.63.97:8000"]


def test_command_is_valid_node_args(tmp_path):
    p = en.plan(argparse.Namespace(
        platform="linux", home=str(tmp_path), base_dir=None, no_venv=True, port=8123,
        host="0.0.0.0", advertise_url="http://1.2.3.4:8123"))
    a = node.parse_args(p["command"][2:])
    assert a.port == 8123 and a.peer == en.SEEDS and a.host == "0.0.0.0"
    assert a.advertise_url == "http://1.2.3.4:8123" and a.data_dir == p["data"]


def test_macos_plist_parses(tmp_path):
    p = en.main(["show", "--platform", "macos", "--home", str(tmp_path)])
    d = plistlib.loads(p["service_text"].encode())
    assert d["Label"] == en.LABEL and d["RunAtLoad"] and en.SEEDS[1] in d["ProgramArguments"]


def test_linux_unit_and_install_files_only(tmp_path):
    p = en.main(["install", "--platform", "linux", "--home", str(tmp_path), "--no-venv", "--no-service"])
    unit = tmp_path / ".config/systemd/user/netlithic-node.service"
    text = unit.read_text()
    assert "ExecStart=" in text and "Restart=on-failure" in text and "WantedBy=default.target" in text
    assert os.path.isdir(p["data"])
    assert not any(f.endswith(".json") for f in os.listdir(p["data"]))  # no wallet/keys created


def test_windows_task_commands(tmp_path):
    p = en.main(["show", "--platform", "windows", "--home", str(tmp_path)])
    cmds = en.register_commands("windows", None, p["task_cmd"])
    assert cmds[0][:2] == ["schtasks", "/Create"] and "ONLOGON" in cmds[0]
    assert "pythonw.exe" in p["python"]


def test_dry_run_writes_nothing(tmp_path):
    en.main(["install", "--platform", "linux", "--home", str(tmp_path), "--dry-run"])
    assert list(tmp_path.iterdir()) == []


def test_docker_files():
    root = os.path.dirname(en.__file__)
    df = open(os.path.join(root, "Dockerfile")).read()
    for s in en.SEEDS:
        assert s in df
    assert "USER netlithic" in df and "--data-dir /data" in df
    ign = open(os.path.join(root, ".dockerignore")).read()
    assert "*.json" in ign  # wallets/keys never copied into the image
    assert "127.0.0.1:8000:8000" in open(os.path.join(root, "docker-compose.yml")).read()


def test_installers_call_easy_node():
    root = os.path.dirname(en.__file__)
    assert "easy_node.py install" in open(os.path.join(root, "install.sh")).read()
    assert "easy_node.py install" in open(os.path.join(root, "install.ps1")).read()


# ---------------------------------------------------------------------------
# Windows scheduled task: short launcher, loud failures (the founder's first Windows run:
# a user folder with a space made the /TR command longer than schtasks' 261 limit,
# schtasks failed, and the installer still printed "Done").
# ---------------------------------------------------------------------------
import subprocess
import types

TR_LIMIT = 261
LONG_USER = "Sam Example"


class FakeSchtasks:
    """Stands in for subprocess.run: enforces schtasks' real /TR limit, records every call."""

    def __init__(self, fail_create=False, fail_run=False, fail_delete=False, task_exists_after_delete=False):
        self.calls = []
        self.fail_create, self.fail_run = fail_create, fail_run
        self.fail_delete, self.task_exists_after_delete = fail_delete, task_exists_after_delete

    def __call__(self, cmd, *a, **kw):
        self.calls.append(list(cmd))

        def res(rc, out="", err=""):
            return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)

        if cmd[0] != "schtasks":
            return res(0)
        op = cmd[1].lower()
        if op == "/create":
            tr = cmd[cmd.index("/TR") + 1]
            if len(tr) > TR_LIMIT:
                return res(1, "", "ERROR: Value for '/TR' option cannot be more than 261 character(s).")
            if self.fail_create:
                return res(1, "", self.fail_create if isinstance(self.fail_create, str) else "ERROR: Access is denied.")
            return res(0, "SUCCESS: The scheduled task was created.")
        if op == "/run":
            return res(1, "", "ERROR: The system cannot find the file specified.") if self.fail_run \
                else res(0, "SUCCESS: Attempted to run the scheduled task.")
        if op == "/delete":
            return res(1, "", "ERROR: Access is denied.") if self.fail_delete else res(0, "SUCCESS")
        if op == "/query":
            return res(0, "Netlithic Node") if self.task_exists_after_delete else res(1, "", "ERROR: not found")
        return res(0)


@pytest.fixture
def win(tmp_path, monkeypatch):
    """A fake 'Sam Example' Windows home with a deep base folder, schtasks mocked, node 'comes up'."""
    home = tmp_path / LONG_USER
    base = home / "AppData" / "Local" / "Netlithic"
    fake = FakeSchtasks()
    monkeypatch.setattr(en.subprocess, "run", fake)
    monkeypatch.setattr(en, "wait_for_node", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(en, "short_path", lambda p: p, raising=False)
    monkeypatch.setattr(en, "node_answers", lambda port: False, raising=False)
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setattr(en, "_sleep", lambda s: None, raising=False)    # never really wait in tests
    started = []                                      # every detached start (nothing real is ever started)
    monkeypatch.setattr(en.subprocess, "Popen", lambda cmd, *a, **k: started.append((list(cmd), k)), raising=False)
    args = ["install", "--platform", "windows", "--home", str(home), "--base-dir", str(base), "--no-venv"]
    return types.SimpleNamespace(home=home, base=base, fake=fake, args=args, tmp=tmp_path, started=started)


def _create_call(fake):
    return [c for c in fake.calls if c[:2] == ["schtasks", "/Create"]][0]


def test_windows_tr_is_short_even_with_long_spaced_paths(win, capsys):
    # the old code put the whole python + node.py + seeds command line into /TR (> 261 chars)
    en.main(win.args)
    tr = _create_call(win.fake)[_create_call(win.fake).index("/TR") + 1]
    assert len(tr) < TR_LIMIT
    assert "--peer" not in tr and "node.py" not in tr          # the long command is NOT in /TR
    assert tr.startswith('"') and tr.endswith('"')              # one quoted path, spaces safe
    assert tr.strip('"').endswith("start-node.cmd")
    assert os.path.isfile(tr.strip('"'))


def test_windows_tr_stays_short_with_very_long_user_folder(win):
    deep = win.tmp / ("X" * 40 + " " + "Y" * 40) / ("Z" * 60 + " name")
    args = ["install", "--platform", "windows", "--home", str(deep), "--base-dir", str(deep / "Netlithic"), "--no-venv"]
    en.main(args)
    tr = _create_call(win.fake)[_create_call(win.fake).index("/TR") + 1]
    assert len(tr) < TR_LIMIT


def test_launcher_file_contents_and_quoting(win):
    p = en.main(win.args)
    text = open(os.path.join(win.base, "start-node.cmd"), encoding="utf-8").read()
    assert text.startswith("@echo off")
    assert f'"{p["python"]}"' in text                           # interpreter quoted (path has a space)
    assert f'"{os.path.join(en.ROOT, "node.py")}"' in text
    assert f'"{p["data"]}"' in text
    for s in en.SEEDS:
        assert s in text
    assert "wallet" not in text.lower() and "token" not in text.lower()
    assert "start" in text.lower()                              # returns at once, node keeps running


def test_launcher_escapes_percent_signs(tmp_path):
    text = en.launcher_text(["C:\\Users\\50%off user\\pythonw.exe", "node.py"])
    assert "50%%off user" in text and "50%off" not in text.replace("%%", "")


def test_install_runs_task_after_creating_it(win, capsys):
    en.main(win.args)
    ops = [c[1] for c in win.fake.calls if c[0] == "schtasks"]
    assert ops.index("/Create") < ops.index("/Run")
    assert "Done" in capsys.readouterr().out


def test_schtasks_create_failure_is_loud(win, capsys):
    win.fake.fail_create = "ERROR: The parameter is incorrect."      # not a permission problem
    with pytest.raises(SystemExit) as e:
        en.main(win.args)
    assert e.value.code not in (0, None)
    out = capsys.readouterr()
    assert "Done" not in out.out + out.err
    assert "parameter is incorrect" in out.out + out.err        # exact schtasks output is shown
    assert not any(c[1] == "/Run" for c in win.fake.calls if c[0] == "schtasks")


def test_old_failure_mode_long_tr_is_not_swallowed(win, capsys, monkeypatch):
    # even if the launcher path could not be shortened, schtasks' 261 error must stop the install
    monkeypatch.setattr(en, "task_command", lambda p: '"' + "A" * 300 + '"', raising=False)
    with pytest.raises(SystemExit) as e:
        en.main(win.args)
    assert e.value.code not in (0, None)
    assert "Done" not in capsys.readouterr().out


def test_first_run_failure_is_loud(win, capsys):
    win.fake.fail_run = True
    with pytest.raises(SystemExit) as e:
        en.main(win.args)
    assert e.value.code not in (0, None)
    out = capsys.readouterr()
    assert "Done" not in out.out + out.err
    assert "cannot find the file specified" in out.out + out.err


def test_node_not_coming_up_is_loud(win, capsys, monkeypatch):
    monkeypatch.setattr(en, "wait_for_node", lambda *a, **k: False, raising=False)
    with pytest.raises(SystemExit) as e:
        en.main(win.args)
    assert e.value.code not in (0, None)
    out = capsys.readouterr()
    assert "Done" not in out.out + out.err
    assert "/status" in out.out + out.err


def test_missing_schtasks_program_is_loud(win, capsys, monkeypatch):
    def boom(cmd, *a, **k):
        raise FileNotFoundError(2, "no such file", cmd[0])
    monkeypatch.setattr(en.subprocess, "run", boom)
    with pytest.raises(SystemExit) as e:
        en.main(win.args)
    assert e.value.code not in (0, None)
    assert "Done" not in capsys.readouterr().out


def test_launcher_path_too_long_falls_back_to_short_name(win, monkeypatch):
    long_base = win.tmp / ("L" * 120) / ("M" * 120) / "Netlithic"
    shortened = []

    def fake_short(p):
        shortened.append(p)
        return str(win.tmp / "SHORT~1" / "NETLIT~1")
    monkeypatch.setattr(en, "short_path", fake_short, raising=False)
    args = ["install", "--platform", "windows", "--home", str(win.home), "--base-dir", str(long_base), "--no-venv"]
    en.main(args)
    tr = _create_call(win.fake)[_create_call(win.fake).index("/TR") + 1]
    assert len(tr) < TR_LIMIT and "SHORT~1" in tr and shortened


def test_launcher_path_too_long_and_no_fallback_fails_clearly(win, capsys, monkeypatch):
    long_base = win.tmp / ("L" * 120) / ("M" * 120) / "Netlithic"
    monkeypatch.setattr(en, "short_path", lambda p: p, raising=False)
    monkeypatch.setattr(en, "fallback_dirs", lambda home: [str(long_base)], raising=False)
    args = ["install", "--platform", "windows", "--home", str(win.home), "--base-dir", str(long_base), "--no-venv"]
    with pytest.raises(SystemExit) as e:
        en.main(args)
    assert e.value.code not in (0, None)
    out = capsys.readouterr()
    assert "Done" not in out.out + out.err and "261" in out.out + out.err
    assert not any(c[:2] == ["schtasks", "/Create"] for c in win.fake.calls)


def test_missing_venv_python_is_loud(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(en.subprocess, "run", FakeSchtasks())
    monkeypatch.setattr(en.venv.EnvBuilder, "create", lambda self, d: None)   # venv "built" nothing
    with pytest.raises(SystemExit) as e:
        en.main(["install", "--platform", "windows", "--home", str(tmp_path), "--base-dir", str(tmp_path / "N")])
    assert e.value.code not in (0, None)
    assert "Done" not in capsys.readouterr().out


def test_pip_failure_is_plain_words_and_nonzero(tmp_path, monkeypatch, capsys):
    def run(cmd, *a, **k):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="ERROR: no matching distribution")
    monkeypatch.setattr(en.subprocess, "run", run)
    py = tmp_path / "bin" / "python"
    (tmp_path / "bin").mkdir()
    py.write_text("")
    monkeypatch.setattr(en, "venv_python", lambda v, p: str(py))
    with pytest.raises(SystemExit) as e:
        en.main(["install", "--platform", "linux", "--home", str(tmp_path), "--base-dir", str(tmp_path / "N")])
    assert e.value.code not in (0, None)
    out = capsys.readouterr()
    assert "Done" not in out.out and "no matching distribution" in out.out + out.err


def test_linux_service_start_failure_is_loud(tmp_path, monkeypatch, capsys):
    def run(cmd, *a, **k):
        bad = cmd[:3] == ["systemctl", "--user", "enable"]
        return types.SimpleNamespace(returncode=1 if bad else 0, stdout="", stderr="Failed to connect to bus" if bad else "")
    monkeypatch.setattr(en.subprocess, "run", run)
    monkeypatch.setattr(en, "wait_for_node", lambda *a, **k: True, raising=False)
    with pytest.raises(SystemExit) as e:
        en.main(["install", "--platform", "linux", "--home", str(tmp_path), "--no-venv"])
    assert e.value.code not in (0, None)
    out = capsys.readouterr()
    assert "Done" not in out.out and "Failed to connect to bus" in out.out + out.err


def test_uninstall_failure_is_loud_and_success_removes_launcher(win, capsys):
    en.main(win.args)
    launcher = win.base / "start-node.cmd"
    assert launcher.exists()
    win.fake.fail_delete = True
    win.fake.task_exists_after_delete = True
    with pytest.raises(SystemExit) as e:
        en.main(["uninstall", "--platform", "windows", "--home", str(win.home), "--base-dir", str(win.base), "--no-venv"])
    assert e.value.code not in (0, None)
    assert "Service removed" not in capsys.readouterr().out
    win.fake.fail_delete = False
    win.fake.task_exists_after_delete = False
    en.main(["uninstall", "--platform", "windows", "--home", str(win.home), "--base-dir", str(win.base), "--no-venv"])
    assert not launcher.exists()
    assert "Service removed" in capsys.readouterr().out


def test_uninstall_when_task_never_existed_is_fine(win, capsys):
    win.fake.fail_delete = True            # schtasks says "cannot find", and a query confirms it is gone
    en.main(["uninstall", "--platform", "windows", "--home", str(win.home), "--base-dir", str(win.base), "--no-venv"])
    assert "Service removed" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# No-admin fallback. On a normal (non-admin) Windows account `schtasks /Create` says "Access is denied"
# (seen on the founder's real PC). Then the node is started from the per-user Startup folder instead.
# ---------------------------------------------------------------------------
STARTUP = os.path.join("Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def _startup_file(win):
    return win.home / "AppData" / "Roaming" / STARTUP / "Netlithic Node.cmd"


def _uninstall_args(win):
    return ["uninstall", "--platform", "windows", "--home", str(win.home), "--base-dir", str(win.base), "--no-venv"]


def test_access_denied_falls_back_to_startup_folder(win, capsys):
    win.fake.fail_create = True
    en.main(win.args)                                             # exits 0: no SystemExit
    out = capsys.readouterr().out
    f = _startup_file(win)
    assert f.is_file()                                            # right folder, resolved from %APPDATA%
    text = f.read_bytes().decode("utf-8")                         # bytes: keep the Windows line endings
    launcher = str(win.base / "start-node.cmd")
    assert f'call "{launcher}"' in text                           # path has a space and is quoted
    assert text.startswith("@echo off") and "\r\n" in text
    assert "Windows would not let this account create a scheduled task" in out
    assert "Startup folder instead" in out and "No admin needed" in out
    assert "Installed as a scheduled task" not in out
    assert "Done" in out
    assert (win.base / "start-node.cmd").is_file()
    # node started now, detached, via the launcher, with no console window
    assert [c for c, k in win.started] == [[launcher]]
    assert win.started[0][1].get("stdin") is not None
    assert not any(c[:2] == ["schtasks", "/Run"] for c in win.fake.calls)


def test_access_denied_text_variants_are_permission_errors():
    for t in ("ERROR: Access is denied.", "access denied", "ERROR: Permission denied", "error 0x80070005"):
        assert en.is_permission_error(t)
    assert not en.is_permission_error("ERROR: The parameter is incorrect.")
    assert not en.is_permission_error("")


def test_access_denied_but_node_never_answers_is_loud(win, capsys, monkeypatch):
    win.fake.fail_create = True
    monkeypatch.setattr(en, "wait_for_node", lambda *a, **k: False, raising=False)
    with pytest.raises(SystemExit) as e:
        en.main(win.args)
    assert e.value.code not in (0, None)
    out = capsys.readouterr()
    assert "Done" not in out.out + out.err
    assert "/status" in out.out + out.err and "Netlithic Node.cmd" in out.out + out.err


def test_non_permission_schtasks_failure_does_not_fall_back(win, capsys):
    win.fake.fail_create = "ERROR: The task name is invalid."
    with pytest.raises(SystemExit) as e:
        en.main(win.args)
    assert e.value.code not in (0, None)
    out = capsys.readouterr()
    assert "task name is invalid" in out.out + out.err and "Done" not in out.out + out.err
    assert not _startup_file(win).exists() and win.started == []


def test_startup_folder_not_writable_is_loud(win, capsys, monkeypatch):
    win.fake.fail_create = True
    (win.home / "AppData" / "Roaming").mkdir(parents=True)
    (win.home / "AppData" / "Roaming" / "Microsoft").write_text("a file, so the folder cannot be made")
    with pytest.raises(SystemExit) as e:
        en.main(win.args)
    assert e.value.code not in (0, None)
    out = capsys.readouterr()
    assert "Done" not in out.out + out.err and "Access is denied" in out.out + out.err
    assert "administrator" in out.out + out.err and win.started == []


def test_detached_start_failure_is_loud(win, capsys, monkeypatch):
    win.fake.fail_create = True
    def boom(*a, **k):
        raise OSError("blocked")
    monkeypatch.setattr(en.subprocess, "Popen", boom)
    with pytest.raises(SystemExit):
        en.main(win.args)
    assert "Done" not in capsys.readouterr().out


def test_admin_install_says_scheduled_task_and_adds_no_startup_file(win, capsys):
    en.main(win.args)
    out = capsys.readouterr().out
    assert "Installed as a scheduled task" in out and "Startup folder" not in out
    assert not _startup_file(win).exists() and win.started == []


def test_startup_launcher_percent_and_accents():
    t = en.startup_text("C:\\Users\\50%off user\\Netlithic\\start-node.cmd")
    assert "50%%off" in t
    assert "chcp 65001" in en.startup_text("C:\\Users\\Zo\u00eb\\start-node.cmd")


def test_startup_dir_uses_appdata_env_not_hardcoded(tmp_path):
    d = en.startup_dir(str(tmp_path / "home"), {"APPDATA": str(tmp_path / "Odd Roaming")})
    assert d == str(tmp_path / "Odd Roaming" / STARTUP)
    assert en.startup_dir(str(tmp_path / "home"), {}) == str(tmp_path / "home" / "AppData" / "Roaming" / STARTUP)


def test_reinstall_is_idempotent_on_the_startup_path(win, capsys):
    win.fake.fail_create = True
    en.main(win.args)
    en.main(win.args)
    files = list(_startup_file(win).parent.iterdir())
    assert files == [_startup_file(win)]                          # still exactly one launcher
    stops = [c for c in win.fake.calls if c[0] == "powershell"]
    assert len(stops) == 2                                        # the old node is stopped before each start
    assert all(str(win.base / "node") in c[-1] for c in stops)    # matched by THIS install's data folder
    assert [c for c, k in win.started] == [[str(win.base / "start-node.cmd")]] * 2


def test_switching_to_admin_task_removes_old_startup_file(win, capsys):
    win.fake.fail_create = True
    en.main(win.args)
    assert _startup_file(win).exists()
    win.fake.fail_create = False
    en.main(win.args)                                             # now run as admin
    assert not _startup_file(win).exists()                        # never both methods: no double start
    assert "Installed as a scheduled task" in capsys.readouterr().out


def test_stop_command_is_safe_for_odd_paths():
    c = en.stop_node_command("C:\\Users\\O'Brien\\Netlithic\\node")
    assert c[0] == "powershell" and "'C:\\Users\\O''Brien" in c[-1]
    assert '"' not in c[-1]                                       # no double quotes to confuse the command line


def test_uninstall_removes_both_task_and_startup_file(win, capsys):
    win.fake.fail_create = True
    en.main(win.args)
    assert _startup_file(win).exists() and (win.base / "start-node.cmd").exists()
    win.fake.calls.clear()
    en.main(_uninstall_args(win))
    assert not _startup_file(win).exists() and not (win.base / "start-node.cmd").exists()
    ops = [c[1] for c in win.fake.calls if c[0] == "schtasks"]
    assert "/Delete" in ops and "/End" in ops                     # the task path is cleaned too
    assert any(c[0] == "powershell" for c in win.fake.calls)      # running node stopped
    assert "Service removed" in capsys.readouterr().out


def test_uninstall_does_not_claim_removal_if_node_still_runs(win, capsys, monkeypatch):
    win.fake.fail_create = True
    en.main(win.args)
    monkeypatch.setattr(en, "node_answers", lambda port: True, raising=False)
    with pytest.raises(SystemExit) as e:
        en.main(_uninstall_args(win))
    assert e.value.code not in (0, None)
    out = capsys.readouterr()
    assert "Service removed" not in out.out and "still running" in out.out + out.err
    assert not _startup_file(win).exists()                        # the login entry is gone anyway


def test_uninstall_does_not_claim_removal_if_startup_file_stays(win, capsys, monkeypatch):
    win.fake.fail_create = True
    en.main(win.args)
    real_remove = os.remove
    def deny(path, *a, **k):
        if str(path).endswith("Netlithic Node.cmd"):
            raise PermissionError("denied")
        return real_remove(path, *a, **k)
    monkeypatch.setattr(os, "remove", deny)
    with pytest.raises(SystemExit) as e:
        en.main(_uninstall_args(win))
    assert e.value.code not in (0, None)
    out = capsys.readouterr()
    assert "Service removed" not in out.out and "Startup-folder file" in out.out + out.err
    assert _startup_file(win).exists()


def test_uninstall_reports_stop_failure_only_if_node_still_answers(win, capsys, monkeypatch):
    win.fake.fail_create = True
    en.main(win.args)
    real = en.run_cmd
    monkeypatch.setattr(en, "run_cmd", lambda c: (1, "powershell is blocked") if c[0] == "powershell" else real(c))
    en.main(_uninstall_args(win))                                 # node is not answering: nothing remains
    assert "Service removed" in capsys.readouterr().out
    win.fake.fail_create = True
    en.main(win.args)
    monkeypatch.setattr(en, "node_answers", lambda port: True, raising=False)
    with pytest.raises(SystemExit):
        en.main(_uninstall_args(win))
    assert "powershell is blocked" in capsys.readouterr().err


def test_docs_and_installer_mention_both_methods():
    root = os.path.dirname(en.__file__)
    doc = open(os.path.join(root, "docs", "EASY_NODE.md"), encoding="utf-8").read()
    assert "Startup folder" in doc and "No admin" in doc
    assert "Startup" in open(os.path.join(root, "install.ps1"), encoding="utf-8").read()


# ---------------------------------------------------------------------------
# Uninstall hardening. Real bug (the founder's PC): `uninstall` crashed with a raw traceback, PermissionError
# [WinError 5] on venv\Lib\site-packages\cryptography\hazmat\bindings\_rust.pyd, because the node (pythonw from
# that venv) was still running and holding the file open.
# ---------------------------------------------------------------------------
import shutil as _shutil


@pytest.fixture
def venv_win(win, monkeypatch):
    """Like `win`, but with a real (fake-content) venv folder and a data folder holding a pretend wallet."""
    win.sleeps = []
    monkeypatch.setattr(en, "_sleep", lambda s: win.sleeps.append(s), raising=False)
    monkeypatch.setattr(en.time, "sleep", lambda s: win.sleeps.append(s))   # old code: never really wait
    venv = win.base / "venv" / "Lib" / "site-packages" / "cryptography"
    venv.mkdir(parents=True)
    (venv / "_rust.pyd").write_text("locked on Windows while the node runs")
    data = win.base / "node"
    data.mkdir(parents=True, exist_ok=True)
    (data / "chain.json").write_text("chain")
    (data / "wallet.json").write_text("pretend wallet - must survive uninstall")
    win.venv, win.data = win.base / "venv", data
    win.uargs = ["uninstall", "--platform", "windows", "--home", str(win.home), "--base-dir", str(win.base)]
    return win


def _data_intact(w):
    return (w.data / "chain.json").read_text() == "chain" and \
        (w.data / "wallet.json").read_text() == "pretend wallet - must survive uninstall"


def _flaky_rmtree(monkeypatch, fail_times, log=None):
    real = _shutil.rmtree
    state = {"n": 0}

    def fake(path, *a, **k):
        state["n"] += 1
        if log is not None:
            log.append(("rmtree", str(path)))
        if state["n"] <= fail_times:
            raise PermissionError(5, "Access is denied", str(path) + "\\_rust.pyd")
        return real(path, *a, **k)
    monkeypatch.setattr(en.shutil, "rmtree", fake)
    return state


def test_uninstall_venv_locked_a_few_times_then_succeeds(venv_win, capsys, monkeypatch):
    st = _flaky_rmtree(monkeypatch, 3)
    en.main(venv_win.uargs)
    assert st["n"] == 4 and not venv_win.venv.exists()
    assert "Service removed" in capsys.readouterr().out
    assert _data_intact(venv_win)


def test_uninstall_venv_always_locked_is_plain_words_not_a_traceback(venv_win, capsys, monkeypatch):
    _flaky_rmtree(monkeypatch, 10 ** 6)
    with pytest.raises(SystemExit) as e:
        en.main(venv_win.uargs)
    assert e.value.code not in (0, None)
    out = capsys.readouterr()
    text = out.out + out.err
    assert "Traceback" not in text and "PermissionError" not in text.split("problem:")[0]
    assert "Service removed" not in text and "Removed" not in text
    assert str(venv_win.venv) in text and "may still be running" in text and "uninstall" in text
    assert "log out" in text
    assert venv_win.venv.exists() and _data_intact(venv_win)
    assert "NOT started" not in text                       # that wording is for install, not uninstall


def test_uninstall_stops_node_before_deleting_venv(venv_win, monkeypatch):
    events = []
    real = en.run_cmd
    monkeypatch.setattr(en, "run_cmd", lambda c: (events.append(("stop" if c[0] == "powershell" else c[0], 0)), real(c))[1])
    _flaky_rmtree(monkeypatch, 0, log=events)
    en.main(venv_win.uargs)
    kinds = [k for k, _ in events]
    assert "stop" in kinds and "rmtree" in kinds
    assert kinds.index("stop") < kinds.index("rmtree")
    assert kinds.index("schtasks") < kinds.index("rmtree")
    stop_cmd = [c for c in venv_win.fake.calls if c[0] == "powershell"][0][-1]
    assert str(venv_win.venv).replace("'", "''") in stop_cmd and str(venv_win.data) in stop_cmd   # matches venv AND data folder


def test_uninstall_waits_for_node_to_go_quiet_before_deleting(venv_win, monkeypatch):
    answers = iter([True, True, True, False] + [False] * 20)
    monkeypatch.setattr(en, "node_answers", lambda port: next(answers))
    log = []
    _flaky_rmtree(monkeypatch, 0, log=log)
    en.main(venv_win.uargs)
    assert len(venv_win.sleeps) >= 3 and not venv_win.venv.exists()
    assert log                                              # deleted only after it went quiet


def test_uninstall_node_never_quiet_stops_without_deleting(venv_win, capsys, monkeypatch):
    monkeypatch.setattr(en, "node_answers", lambda port: True)
    log = []
    _flaky_rmtree(monkeypatch, 0, log=log)
    with pytest.raises(SystemExit):
        en.main(venv_win.uargs)
    assert log == [] and venv_win.venv.exists() and _data_intact(venv_win)
    assert "Traceback" not in capsys.readouterr().err
    assert sum(venv_win.sleeps) <= 15                       # a short, bounded wait


def test_rmtree_handler_clears_read_only_flag(tmp_path):
    f = tmp_path / "ro.pyd"
    f.write_text("x")
    os.chmod(f, 0o444)
    seen = []

    def func(path):
        if not os.access(path, os.W_OK):
            raise PermissionError(13, "read-only", path)
        seen.append(path)
    en._unlock_and_retry(func, str(f), PermissionError())
    assert seen == [str(f)]
    en._unlock_and_retry(func, str(tmp_path / "gone"), FileNotFoundError())   # already gone: quietly fine


def test_remove_tree_removes_a_normal_folder_with_read_only_files(tmp_path):
    d = tmp_path / "v" / "sub"
    d.mkdir(parents=True)
    (d / "a.txt").write_text("a")
    os.chmod(d / "a.txt", 0o444)
    en.remove_tree(str(tmp_path / "v"), attempts=2, delay=0)
    assert not (tmp_path / "v").exists()


def test_uninstall_other_file_failures_are_plain_words(venv_win, capsys, monkeypatch):
    en.main(venv_win.args)                                  # install (files only; the venv folder already exists)
    real_remove = os.remove

    def deny(path, *a, **k):
        if str(path).endswith("start-node.cmd"):
            raise PermissionError(13, "Access is denied", str(path))
        return real_remove(path, *a, **k)
    monkeypatch.setattr(os, "remove", deny)
    with pytest.raises(SystemExit) as e:
        en.main(venv_win.uargs)
    assert e.value.code not in (0, None)
    out = capsys.readouterr()
    assert "Traceback" not in out.out + out.err and "start-node.cmd" in out.err
    assert "Service removed" not in out.out and _data_intact(venv_win)


def test_main_turns_unexpected_oserror_into_plain_error(monkeypatch, capsys):
    for action in ("install", "uninstall"):
        monkeypatch.setattr(en, action, lambda a: (_ for _ in ()).throw(PermissionError(13, "Access is denied", "/some/file")))
        with pytest.raises(SystemExit) as e:
            en.main([action, "--platform", "windows"])
        assert e.value.code == 1
        out = capsys.readouterr()
        assert "ERROR" in out.err and "Traceback" not in out.err + out.out and "/some/file" in out.err


def test_uninstall_refuses_to_delete_venv_that_overlaps_data(tmp_path, monkeypatch, capsys):
    base = tmp_path / "b"
    (base / "venv" / "node").mkdir(parents=True)
    (base / "venv" / "node" / "wallet.json").write_text("w")
    monkeypatch.setattr(en.subprocess, "run", FakeSchtasks())
    monkeypatch.setattr(en, "node_answers", lambda p: False)
    monkeypatch.setattr(en, "plan", lambda a, _p=en.plan: {**_p(a), "data": str(base / "venv" / "node")})
    with pytest.raises(SystemExit):
        en.main(["uninstall", "--platform", "windows", "--home", str(tmp_path), "--base-dir", str(base)])
    assert (base / "venv" / "node" / "wallet.json").exists()
