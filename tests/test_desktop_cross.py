import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "desktop"))
import netlithic_desktop as nd  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*p):
    return open(os.path.join(ROOT, *p), encoding="utf-8").read()


def test_data_dirs_per_os():
    h = os.path.join("H")
    assert nd.default_data_dir("darwin", {}, h) == os.path.join(h, "Library", "Application Support", "Netlithic", "testnet")
    assert nd.default_data_dir("linux", {}, h) == os.path.join(h, ".local", "share", "Netlithic", "testnet")
    assert nd.default_data_dir("linux", {"XDG_DATA_HOME": "X"}, h) == os.path.join("X", "Netlithic", "testnet")
    assert nd.default_data_dir("win32", {"LOCALAPPDATA": "L"}, h) == os.path.join("L", "Netlithic", "testnet")


def test_build_scripts_guard_secrets_and_signing():
    for f in ("build_linux.sh", "build_macos.sh"):
        s = read("desktop", f)
        assert "wallets.json" in s and "Stopping" in s
        assert "--add-data" in s and "config:config" in s
    mac = read("desktop", "build_macos.sh")
    assert "notarytool" in mac and "UNSIGNED" in mac and "hdiutil" in mac
    assert "tar.gz" in read("desktop", "build_linux.sh") and "AppImage" in read("desktop", "build_linux.sh")


def test_workflow_matrix_and_no_publish():
    wf = read(".github", "workflows", "desktop.yml")
    for os_ in ("windows-latest", "macos-latest", "ubuntu-22.04"):
        assert os_ in wf
    assert "upload-artifact" in wf and "release" not in wf.lower().replace("releases/download", "")
    assert "--no-window" in wf
