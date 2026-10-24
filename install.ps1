# Netlithic TEST node installer for Windows. Test coins have no value.
# Usage: right-click > Run with PowerShell, or: powershell -ExecutionPolicy Bypass -File install.ps1
# No administrator needed: if Windows will not let your account create a scheduled task, the node is
# started from your own Startup folder instead (the installer tells you which one it used).
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$py = $null
foreach ($c in @("py", "python", "python3")) {
  if (Get-Command $c -ErrorAction SilentlyContinue) { $py = $c; break }
}
if (-not $py) {
  Write-Host "Python 3.9 or newer is needed. Get it from python.org (tick 'Add to PATH'), then run this again."
  exit 1
}
& $py easy_node.py install @args
if ($LASTEXITCODE -ne 0) {
  Write-Host ""
  Write-Host "The install did not finish (see the message above). Your node was NOT started."
  exit $LASTEXITCODE
}
