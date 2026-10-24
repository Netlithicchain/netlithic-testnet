# Desktop app on Mac and Linux

Test coins have no value. Same app as on Windows: one window, no terminal.

| | Windows | Mac | Linux |
|---|---|---|---|
| Download | Netlithic-Setup.exe | Netlithic.dmg (drag to Applications) | Netlithic-linux-x86_64.tar.gz or .AppImage |
| Your data | %LOCALAPPDATA%\Netlithic | ~/Library/Application Support/Netlithic | ~/.local/share/Netlithic |
| Build | desktop/build_windows.ps1 | desktop/build_macos.sh (on a Mac) | desktop/build_linux.sh |

The wallet and chain copy live in the data folder, never in the app, so updating the app keeps
the wallet. Builds refuse to finish if a wallet or token file got bundled.

## Automatic builds
`.github/workflows/desktop.yml` builds all three when run by hand or on a `desktop-v*` tag, and
uploads them as build artifacts (it does not publish a release). Not pushed yet.

## Mac signing (decided: not for now)
Unsigned Mac apps are blocked by macOS ("can't be opened because Apple cannot check it").
To ship to normal users we need an Apple Developer account ($99/year), a "Developer ID
Application" certificate, and notarization. Set NETLITHIC_MAC_IDENTITY and
NETLITHIC_NOTARY_PROFILE (or the matching GitHub secrets). Until then Mac builds are test-only.

**Decision (the founder, 2026-10-22):** no Apple Developer account for now. macOS builds stay
test-only (not offered as a public download) until there is real Windows/Linux demand;
revisit then.
Linux needs no signing; the .sha256 file lets people check the download.

## Tested
Linux tar.gz built and smoke-tested on the box (Python 3.12, PyInstaller): window server and
node start, data goes to ~/.local/share/Netlithic/testnet, node stops with the app. The Mac and
AppImage steps are written but not run (no Mac / appimagetool on the box).
Note: builds need a Python with a shared libpython (python.org, uv, or GitHub's setup-python).
