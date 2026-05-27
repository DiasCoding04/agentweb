$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$openclawPath = "openclaw.cmd"
if (Get-Command "openclaw.cmd" -ErrorAction SilentlyContinue) {
  $openclawPath = (Get-Command "openclaw.cmd").Source
} else {
  $possiblePaths = @(
    "$env:APPDATA\npm\openclaw.cmd",
    "$env:USERPROFILE\AppData\Roaming\npm\openclaw.cmd",
    "C:\Users\$env:USERNAME\AppData\Roaming\npm\openclaw.cmd"
  )
  foreach ($p in $possiblePaths) {
    if (Test-Path $p) {
      $openclawPath = $p
      break
    }
  }
}

& $openclawPath gateway probe *> $null
if ($LASTEXITCODE -ne 0) {
  Start-Process -FilePath $openclawPath -ArgumentList @("gateway", "run", "--force") -WindowStyle Minimized
  Start-Sleep -Seconds 12
}

& $openclawPath dashboard --yes --no-open | Out-Null
$url = Get-Clipboard

if (-not ($url -match "^https?://")) {
  throw "OpenClaw did not place a dashboard URL on the clipboard."
}

Start-Process $url
