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
# Windows scheduled task: short launcher, loud failures (Aric's first Windows run:
# a user folder with a space made the /TR command longer than schtasks' 261 limit,
# schtasks failed, and the installer still printed "Done").
# ---------------------------------------------------------------------------
import subprocess
import types

TR_LIMIT = 261
LONG_USER = "Aric Vennen"


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
                return res(1, "", "ERROR: Access is denied.")
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
    """A fake 'Aric Vennen' Windows home with a deep base folder, schtasks mocked, node 'comes up'."""
    home = tmp_path / LONG_USER
    base = home / "AppData" / "Local" / "Netlithic"
    fake = FakeSchtasks()
    monkeypatch.setattr(en.subprocess, "run", fake)
    monkeypatch.setattr(en, "wait_for_node", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(en, "short_path", lambda p: p, raising=False)
    args = ["install", "--platform", "windows", "--home", str(home), "--base-dir", str(base), "--no-venv"]
    return types.SimpleNamespace(home=home, base=base, fake=fake, args=args, tmp=tmp_path)


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
    win.fake.fail_create = True
    with pytest.raises(SystemExit) as e:
        en.main(win.args)
    assert e.value.code not in (0, None)
    out = capsys.readouterr()
    assert "Done" not in out.out + out.err
    assert "Access is denied" in out.out + out.err              # exact schtasks output is shown
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
