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
# Use the Python the build machine chose (GitHub's setup-python sets $env:pythonLocation).
# The "py" launcher can pick a newer Python (3.15) whose cryptography build needs a DLL we do not ship.
$py = if ($env:pythonLocation) { Join-Path $env:pythonLocation "python.exe" } else { "py" }
& $py -m venv --clear .venv-desktop-build
if ($LASTEXITCODE -ne 0) { throw "Could not create the build environment with $py." }
$pyVersion = ("" + (& .\.venv-desktop-build\Scripts\python -c "import sys; print('%d.%d' % sys.version_info[:2])")).Trim()
if ($pyVersion -notmatch '^3\.(11|12|13)$') {
  throw "Python $pyVersion is not supported for the desktop build (need 3.11-3.13)."
}
Write-Host "Building with Python $pyVersion"
.\.venv-desktop-build\Scripts\python -m pip install --upgrade pip
.\.venv-desktop-build\Scripts\python -m pip install -r desktop\requirements-desktop.txt
# icon from the existing logo
.\.venv-desktop-build\Scripts\python -c "from PIL import Image; Image.open('webwallet/static/logo.png').convert('RGBA').save('desktop/netlithic.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"
# Only code, config and page files go in. Never any wallets.json / genesis_wallets.json / data folders.
# PyInstaller writes its progress to stderr. Keep the console output, keep the text, and stop the build
# if it could not find a library (that build would crash on the user's PC). Continue (not Stop) while it
# runs so Windows PowerShell 5.1 does not treat the stderr lines as errors.
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$pyiOutput = & .\.venv-desktop-build\Scripts\pyinstaller --noconfirm --clean --windowed --name Netlithic `
  --icon desktop\netlithic.ico `
  --add-data "config;config" `
  --add-data "webwallet\static;webwallet\static" `
  --add-data "desktop\static;desktop\static" `
  --hidden-import p2p_http --hidden-import node --hidden-import core --hidden-import economics `
  --hidden-import contributions --hidden-import wallet --hidden-import webwallet --hidden-import node_pool --hidden-import netutil --hidden-import pause --hidden-import registry --hidden-import netlithic_version --hidden-import history `
  --paths . desktop\netlithic_desktop.py 2>&1 | ForEach-Object { $line = "$_"; Write-Host $line; $line }
$pyiExit = $LASTEXITCODE
$ErrorActionPreference = $prevEap
if ($pyiExit -ne 0) { throw "PyInstaller failed (exit code $pyiExit)." }
if (($pyiOutput | Out-String) -match "Library not found") {
  throw "PyInstaller could not find a library the app needs (see the 'Library not found' line above). Stopping."
}
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
