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
    cmds = en.register_commands("windows", None, p["service_text"])
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
