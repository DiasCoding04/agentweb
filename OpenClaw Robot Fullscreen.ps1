$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

openclaw.cmd gateway probe *> $null
if ($LASTEXITCODE -ne 0) {
  Start-Process -FilePath "openclaw.cmd" -ArgumentList @("gateway", "run", "--force") -WindowStyle Minimized
  Start-Sleep -Seconds 12
}

openclaw.cmd dashboard --yes --no-open | Out-Null
$url = Get-Clipboard

if (-not ($url -match "^https?://")) {
  throw "OpenClaw did not place a dashboard URL on the clipboard."
}

$chromePaths = @(
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
  "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
  "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
  "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
)

$browser = $chromePaths | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($browser) {
  Start-Process -FilePath $browser -ArgumentList @("--start-fullscreen", "--app=$url")
} else {
  Start-Process $url
}
