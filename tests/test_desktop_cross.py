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


def test_desktop_requirements_have_upper_bounds():
    """An unbounded 'cryptography>=42' let a Python 3.15 build pull a wheel that needs
    python3t.dll (not bundled) and the installer crashed on start-up. Keep every
    desktop build dependency bounded so a new major version cannot slip in."""
    lines = [l.strip() for l in read("desktop", "requirements-desktop.txt").splitlines()
             if l.strip() and not l.strip().startswith("#")]
    assert {l.split(">=")[0] for l in lines} >= {"cryptography", "pywebview", "pyinstaller", "pillow"}
    for l in lines:
        assert ">=" in l and "<" in l, f"{l!r} has no upper bound"


def test_windows_build_uses_workflow_python_and_checks_it():
    s = read("desktop", "build_windows.ps1")
    assert "$env:pythonLocation" in s and "& $py -m venv" in s
    assert "py -m venv" not in s.replace("& $py -m venv", "").replace('"py"', "")
    assert "3.11-3.13" in s and r"^3\.(11|12|13)$" in s
    assert "Library not found" in s  # a build that could not bundle a DLL must stop


def test_workflow_has_windows_smoke_test():
    wf = read(".github", "workflows", "desktop.yml")
    assert "Smoke test (Windows)" in wf and "Netlithic.exe" in wf
    assert "--no-window" in wf and "47190" in wf
    assert "contents: read" in wf
