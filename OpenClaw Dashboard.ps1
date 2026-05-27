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

Start-Process $url
