# Build Netlithic-Setup.exe on a Windows PC (Python 3.11+ and Inno Setup 6 installed).
# Run from the repo root in PowerShell:   powershell -ExecutionPolicy Bypass -File desktop\build_windows.ps1
# Output: dist\Netlithic\Netlithic.exe, desktop\Output\Netlithic-Setup.exe and Netlithic-Setup.exe.sha256
#
# Optional code signing (skipped when not configured; nothing is bought or set up by this script):
#   $env:NETLITHIC_SIGN_CMD  = full signing command, with {file} where the file path goes, e.g. for Azure Artifact Signing:
#       signtool sign /v /fd SHA256 /tr http://timestamp.acs.microsoft.com /td SHA256 /dlib <path>\Azure.CodeSigning.Dlib.dll /dmdf <path>\metadata.json {file}
#   or $env:NETLITHIC_SIGN_PFX (+ $env:NETLITHIC_SIGN_PFX_PASSWORD) for a .pfx certificate file.
#   If neither is set the build is UNSIGNED and says so. Unsigned builds must not be published.
$ErrorActionPreference = "Stop"
py -m venv .venv-desktop-build
.\.venv-desktop-build\Scripts\python -m pip install --upgrade pip
.\.venv-desktop-build\Scripts\python -m pip install -r desktop\requirements-desktop.txt
# icon from the existing logo
.\.venv-desktop-build\Scripts\python -c "from PIL import Image; Image.open('webwallet/static/logo.png').convert('RGBA').save('desktop/netlithic.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"
# Only code, config and page files go in. Never any wallets.json / genesis_wallets.json / data folders.
.\.venv-desktop-build\Scripts\pyinstaller --noconfirm --clean --windowed --name Netlithic `
  --icon desktop\netlithic.ico `
  --add-data "config;config" `
  --add-data "webwallet\static;webwallet\static" `
  --add-data "desktop\static;desktop\static" `
  --hidden-import p2p_http --hidden-import node --hidden-import core --hidden-import economics `
  --hidden-import contributions --hidden-import wallet --hidden-import webwallet --hidden-import node_pool --hidden-import netutil --hidden-import pause --hidden-import netlithic_version --hidden-import history `
  --paths . desktop\netlithic_desktop.py
if (Get-ChildItem -Recurse dist\Netlithic -Include wallets.json,genesis_wallets.json,admin_token.txt) {
  throw "A wallet or token file ended up in the build. Stopping."
}

function Invoke-NetlithicSign([string]$file) {
  if ($env:NETLITHIC_SIGN_CMD) {
    $cmd = $env:NETLITHIC_SIGN_CMD.Replace("{file}", '"' + $file + '"')
    Write-Host "Signing $file"
    cmd /c $cmd
    if ($LASTEXITCODE -ne 0) { throw "Signing failed for $file" }
    return $true
  } elseif ($env:NETLITHIC_SIGN_PFX) {
    Write-Host "Signing $file with the .pfx certificate"
    & signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /f $env:NETLITHIC_SIGN_PFX /p $env:NETLITHIC_SIGN_PFX_PASSWORD $file
    if ($LASTEXITCODE -ne 0) { throw "Signing failed for $file" }
    return $true
  }
  return $false
}
$signed = Invoke-NetlithicSign "dist\Netlithic\Netlithic.exe"
& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" desktop\netlithic.iss
if ($signed) { Invoke-NetlithicSign "desktop\Output\Netlithic-Setup.exe" | Out-Null }
# SHA-256 checksum (people compare it with: certutil -hashfile Netlithic-Setup.exe SHA256)
& "$PSScriptRoot\checksum.ps1" "desktop\Output\Netlithic-Setup.exe"
if ($signed) { Write-Host "Done (signed): desktop\Output\Netlithic-Setup.exe" }
else { Write-Host "Done (UNSIGNED - for testing only, do not publish): desktop\Output\Netlithic-Setup.exe" }
