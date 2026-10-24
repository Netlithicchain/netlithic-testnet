# Write <file>.sha256 next to a file, in the usual "<hash>  <name>" format.
# Usage: powershell -ExecutionPolicy Bypass -File desktop\checksum.ps1 desktop\Output\Netlithic-Setup.exe
param([Parameter(Mandatory=$true)][string]$Path)
$ErrorActionPreference = "Stop"
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLower()
$name = Split-Path -Leaf $Path
"$hash  $name" | Set-Content -Encoding ascii -LiteralPath ($Path + ".sha256")
Write-Host "SHA-256 of ${name}: $hash"
Write-Host "Saved $Path.sha256"
