#!/bin/sh
# Build the Netlithic desktop app for Linux (test network; test coins have no value).
# Run from the repo root:  sh desktop/build_linux.sh
# Output: dist/Netlithic-linux-x86_64.tar.gz (+ .sha256), and dist/Netlithic-x86_64.AppImage
# when `appimagetool` is on PATH. Data lives in ~/.local/share/Netlithic, never in the app.
set -e
PY=${PYTHON:-python3}
$PY -m venv .venv-desktop-build
. .venv-desktop-build/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r desktop/requirements-desktop.txt
python -c "from PIL import Image; Image.open('webwallet/static/logo.png').convert('RGBA').resize((256,256)).save('desktop/netlithic.png')"
# Only code, config and page files go in. Never wallets.json / genesis_wallets.json / data folders.
pyinstaller --noconfirm --clean --windowed --name Netlithic \
  --add-data "config:config" --add-data "webwallet/static:webwallet/static" --add-data "desktop/static:desktop/static" \
  --hidden-import p2p_http --hidden-import node --hidden-import core --hidden-import economics \
  --hidden-import contributions --hidden-import wallet --hidden-import webwallet --hidden-import node_pool --hidden-import netutil --hidden-import pause --hidden-import netlithic_version --hidden-import history \
  --paths . desktop/netlithic_desktop.py
if find dist/Netlithic \( -name wallets.json -o -name genesis_wallets.json -o -name admin_token.txt -o -name faucet_wallet.json \) | grep -q .; then
  echo "A wallet or token file ended up in the build. Stopping." >&2; exit 1
fi
cp desktop/netlithic.png dist/Netlithic/
cat > dist/Netlithic/netlithic.desktop <<'D'
[Desktop Entry]
Type=Application
Name=Netlithic (Test Network)
Comment=Test network wallet. Test coins have no value.
Exec=Netlithic
Icon=netlithic
Categories=Utility;
D
ARCH=$(uname -m)
tar -C dist -czf "dist/Netlithic-linux-$ARCH.tar.gz" Netlithic
sh desktop/checksum.sh "dist/Netlithic-linux-$ARCH.tar.gz"
if command -v appimagetool >/dev/null 2>&1; then
  rm -rf dist/AppDir && mkdir -p dist/AppDir && cp -r dist/Netlithic/. dist/AppDir/
  printf '#!/bin/sh\nexec "$(dirname "$(readlink -f "$0")")/Netlithic" "$@"\n' > dist/AppDir/AppRun
  chmod +x dist/AppDir/AppRun
  ARCH=$ARCH appimagetool dist/AppDir "dist/Netlithic-$ARCH.AppImage"
  sh desktop/checksum.sh "dist/Netlithic-$ARCH.AppImage"
else
  echo "(appimagetool not found: built the tar.gz only)"
fi
echo "Done: dist/Netlithic-linux-$ARCH.tar.gz"
