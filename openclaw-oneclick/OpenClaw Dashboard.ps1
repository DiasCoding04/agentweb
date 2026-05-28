$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
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

function Test-OpenClawGateway {
  & $openclawPath gateway call health *> $null
  return ($LASTEXITCODE -eq 0)
}

if (-not (Test-Path $openclawPath) -and -not (Get-Command openclaw.cmd -ErrorAction SilentlyContinue)) {
  [System.Windows.Forms.MessageBox]::Show(
    "OpenClaw is not installed yet. Run Install-OpenClaw-OneClick.cmd first.",
    "OpenClaw",
    "OK",
    "Warning"
  ) | Out-Null
  exit 1
}

if (-not (Test-OpenClawGateway)) {
  Start-Process -FilePath $openclawPath -ArgumentList @("gateway", "run", "--force") -WindowStyle Minimized

  $ready = $false
  for ($i = 0; $i -lt 45; $i++) {
    Start-Sleep -Seconds 1
    if (Test-OpenClawGateway) {
      $ready = $true
      break
    }
  }

  if (-not $ready) {
    [System.Windows.Forms.MessageBox]::Show(
      "OpenClaw Gateway started, but did not become ready in time. Try the shortcut again in 20 seconds.",
      "OpenClaw",
      "OK",
      "Information"
    ) | Out-Null
  }
}

& $openclawPath dashboard --yes --no-open | Out-Null
$url = Get-Clipboard

if (-not ($url -match "^https?://")) {
  [System.Windows.Forms.MessageBox]::Show(
    "OpenClaw did not place a dashboard URL on the clipboard. Run openclaw.cmd dashboard in PowerShell to inspect.",
    "OpenClaw",
    "OK",
    "Error"
  ) | Out-Null
  exit 1
}

Start-Process $url
