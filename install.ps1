# Netlithic TEST node installer for Windows. Test coins have no value.
# Usage: right-click > Run with PowerShell, or: powershell -ExecutionPolicy Bypass -File install.ps1
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
