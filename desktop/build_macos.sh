#!/bin/sh
# Build Netlithic.app and Netlithic.dmg on a Mac (test network; test coins have no value).
# Run from the repo root:  sh desktop/build_macos.sh
# Data lives in ~/Library/Application Support/Netlithic, never inside the app.
#
# Signing + notarization (needed or macOS will say the app "can't be opened"):
#   NETLITHIC_MAC_IDENTITY  "Developer ID Application: <Name> (<TEAMID>)"  (Apple Developer account, $99/yr)
#   NETLITHIC_NOTARY_PROFILE  a keychain profile made once with `xcrun notarytool store-credentials`
# Nothing is bought or set up by this script. Without them the build is UNSIGNED: testing only.
set -e
PY=${PYTHON:-python3}
$PY -m venv .venv-desktop-build
. .venv-desktop-build/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r desktop/requirements-desktop.txt
python -c "from PIL import Image; Image.open('webwallet/static/logo.png').convert('RGBA').resize((1024,1024)).save('desktop/netlithic.icns')"
pyinstaller --noconfirm --clean --windowed --name Netlithic --icon desktop/netlithic.icns \
  --osx-bundle-identifier org.netlithic.desktop \
  --add-data "config:config" --add-data "webwallet/static:webwallet/static" --add-data "desktop/static:desktop/static" \
  --hidden-import p2p_http --hidden-import node --hidden-import core --hidden-import economics \
  --hidden-import contributions --hidden-import wallet --hidden-import webwallet --hidden-import node_pool --hidden-import netutil --hidden-import pause --hidden-import registry --hidden-import netlithic_version --hidden-import history \
  --paths . desktop/netlithic_desktop.py
if find dist/Netlithic.app \( -name wallets.json -o -name genesis_wallets.json -o -name admin_token.txt -o -name faucet_wallet.json \) | grep -q .; then
  echo "A wallet or token file ended up in the build. Stopping." >&2; exit 1
fi
SIGNED=0
if [ -n "$NETLITHIC_MAC_IDENTITY" ]; then
  codesign --deep --force --options runtime --timestamp --sign "$NETLITHIC_MAC_IDENTITY" dist/Netlithic.app
  SIGNED=1
fi
rm -f dist/Netlithic.dmg
hdiutil create -volname "Netlithic" -srcfolder dist/Netlithic.app -ov -format UDZO dist/Netlithic.dmg
if [ "$SIGNED" = 1 ]; then
  codesign --force --timestamp --sign "$NETLITHIC_MAC_IDENTITY" dist/Netlithic.dmg
  if [ -n "$NETLITHIC_NOTARY_PROFILE" ]; then
    xcrun notarytool submit dist/Netlithic.dmg --keychain-profile "$NETLITHIC_NOTARY_PROFILE" --wait
    xcrun stapler staple dist/Netlithic.dmg
  else
    echo "Signed but NOT notarized: set NETLITHIC_NOTARY_PROFILE before publishing."
  fi
fi
sh desktop/checksum.sh dist/Netlithic.dmg
[ "$SIGNED" = 1 ] && echo "Done (signed): dist/Netlithic.dmg" || echo "Done (UNSIGNED - testing only, do not publish): dist/Netlithic.dmg"
