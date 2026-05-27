$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$probeOutput = & openclaw.cmd gateway probe 2>&1
if ($LASTEXITCODE -ne 0 -or ($probeOutput -join "`n") -match "EPERM|failed") {
  Start-Process -FilePath "openclaw.cmd" -ArgumentList @("gateway", "run", "--force") -WindowStyle Minimized
  Start-Sleep -Seconds 12
}

$dashboardOutput = & openclaw.cmd dashboard --yes --no-open 2>&1
$clipboard = Get-Clipboard -Raw -ErrorAction SilentlyContinue
$joinedOutput = $dashboardOutput -join "`n"

$url = $null
if ($clipboard -match "https?://[^\s]+") {
  $url = $Matches[0]
} elseif ($joinedOutput -match "https?://[^\s]+") {
  $url = $Matches[0]
} else {
  $url = "http://127.0.0.1:18789/"
}

if (-not ($url -match "^https?://")) {
  throw "OpenClaw did not provide a dashboard URL. Output: $joinedOutput"
}

Add-Type -AssemblyName System.Windows.Forms
$screen = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
$width = 520
$height = $screen.Height
$left = [Math]::Max(0, $screen.Right - $width)
$top = $screen.Top

Add-Type @"
using System;
using System.Runtime.InteropServices;

public static class WindowTools {
  [DllImport("user32.dll")]
  public static extern bool MoveWindow(IntPtr hWnd, int X, int Y, int nWidth, int nHeight, bool bRepaint);

  [DllImport("user32.dll")]
  public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
}
"@

$browserPaths = @(
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
  "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
  "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
  "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
)

$browser = $browserPaths | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($browser) {
  $process = Start-Process -FilePath $browser -PassThru -ArgumentList @(
    "--app=$url",
    "--window-size=$width,$height",
    "--window-position=$left,$top"
  )

  Start-Sleep -Seconds 2
  $candidates = @(Get-Process | Where-Object {
    $_.MainWindowHandle -ne 0 -and
    ($_.ProcessName -match "chrome|msedge") -and
    ($_.MainWindowTitle -match "OpenClaw|127.0.0.1|Control")
  })

  if ($candidates.Count -gt 0) {
    $window = $candidates | Select-Object -Last 1
    [void][WindowTools]::ShowWindow($window.MainWindowHandle, 1)
    [void][WindowTools]::MoveWindow($window.MainWindowHandle, $left, $top, $width, $height, $true)
  }
} else {
  Start-Process $url
}
