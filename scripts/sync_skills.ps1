# Sync skills from this repo into the DSH skills directory so sessions can load them.
# Usage: pwsh -ExecutionPolicy Bypass -File scripts\sync_skills.ps1
# Kept ASCII-only on purpose: Windows PowerShell 5.1 reads .ps1 as ANSI.

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$source = Join-Path $repo "skills"
$target = Join-Path $env:USERPROFILE ".dsh\skills"

if (-not (Test-Path $target)) { New-Item -ItemType Directory -Force -Path $target | Out-Null }

Get-ChildItem $source -Directory | ForEach-Object {
    $dest = Join-Path $target $_.Name
    if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
    Copy-Item $_.FullName $dest -Recurse -Force
    Write-Host ("synced: {0} -> {1}" -f $_.Name, $dest)
}
Write-Host "done. Skills apply to new sessions (or after /new in Feishu)."