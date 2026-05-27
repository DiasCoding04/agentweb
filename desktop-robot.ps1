param(
  [Parameter(Position = 0)]
  [ValidateSet("bounds", "position", "screenshot", "move", "click", "doubleclick", "rightclick", "scroll", "type", "hotkey", "wait")]
  [string]$Action = "position",

  [int]$X = 0,
  [int]$Y = 0,
  [int]$Delta = 0,
  [double]$Seconds = 0.2,
  [string]$Text = "",
  [string]$Out = ""
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

Add-Type @"
using System;
using System.Runtime.InteropServices;

public static class NativeMouse {
  [DllImport("user32.dll")]
  public static extern bool SetCursorPos(int X, int Y);

  [DllImport("user32.dll")]
  public static extern bool GetCursorPos(out POINT lpPoint);

  [DllImport("user32.dll")]
  public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);

  public struct POINT {
    public int X;
    public int Y;
  }
}
"@

$MouseLeftDown = 0x0002
$MouseLeftUp = 0x0004
$MouseRightDown = 0x0008
$MouseRightUp = 0x0010
$MouseWheel = 0x0800

function Write-Json($value) {
  $value | ConvertTo-Json -Compress
}

function Get-BoundsInfo {
  $bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
  [ordered]@{
    left = $bounds.Left
    top = $bounds.Top
    width = $bounds.Width
    height = $bounds.Height
    right = $bounds.Right
    bottom = $bounds.Bottom
  }
}

function Get-MousePosition {
  $point = New-Object NativeMouse+POINT
  [void][NativeMouse]::GetCursorPos([ref]$point)
  [ordered]@{ x = $point.X; y = $point.Y }
}

function Save-Screenshot {
  param([string]$Path)

  $bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
  if ([string]::IsNullOrWhiteSpace($Path)) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $Path = Join-Path $PWD "desktop-screen-$stamp.png"
  }

  $bitmap = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
  $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
  try {
    $graphics.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bitmap.Size)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    $bitmap.Save($resolved, [System.Drawing.Imaging.ImageFormat]::Png)
    Write-Json ([ordered]@{ ok = $true; path = $resolved; bounds = Get-BoundsInfo })
  }
  finally {
    $graphics.Dispose()
    $bitmap.Dispose()
  }
}

function Move-Mouse {
  param([int]$TargetX, [int]$TargetY)
  [void][NativeMouse]::SetCursorPos($TargetX, $TargetY)
}

function Click-Mouse {
  param([int]$TargetX, [int]$TargetY, [string]$Button = "left")
  Move-Mouse $TargetX $TargetY
  Start-Sleep -Milliseconds 80
  if ($Button -eq "right") {
    [NativeMouse]::mouse_event($MouseRightDown, 0, 0, 0, [UIntPtr]::Zero)
    [NativeMouse]::mouse_event($MouseRightUp, 0, 0, 0, [UIntPtr]::Zero)
  } else {
    [NativeMouse]::mouse_event($MouseLeftDown, 0, 0, 0, [UIntPtr]::Zero)
    [NativeMouse]::mouse_event($MouseLeftUp, 0, 0, 0, [UIntPtr]::Zero)
  }
}

function Send-Text {
  param([string]$Value)
  if ($null -eq $Value) { $Value = "" }
  Set-Clipboard -Value $Value
  Start-Sleep -Milliseconds 80
  $shell = New-Object -ComObject WScript.Shell
  $shell.SendKeys("^v")
}

function Convert-Hotkey {
  param([string]$Value)
  $parts = $Value.ToLowerInvariant().Split("+", [System.StringSplitOptions]::RemoveEmptyEntries)
  if ($parts.Count -eq 0) { throw "Hotkey is empty." }

  $prefix = ""
  $key = $parts[$parts.Count - 1]
  if ($parts.Count -gt 1) {
    foreach ($part in $parts[0..($parts.Count - 2)]) {
      if ($part -eq "ctrl" -or $part -eq "control") { $prefix += "^" }
      elseif ($part -eq "shift") { $prefix += "+" }
      elseif ($part -eq "alt") { $prefix += "%" }
    }
  }

  $special = @{
    "enter"="{ENTER}"; "tab"="{TAB}"; "esc"="{ESC}"; "escape"="{ESC}";
    "backspace"="{BACKSPACE}"; "delete"="{DELETE}"; "del"="{DELETE}";
    "space"=" "; "up"="{UP}"; "down"="{DOWN}"; "left"="{LEFT}"; "right"="{RIGHT}";
    "home"="{HOME}"; "end"="{END}"; "pgup"="{PGUP}"; "pgdn"="{PGDN}";
    "f1"="{F1}"; "f2"="{F2}"; "f3"="{F3}"; "f4"="{F4}"; "f5"="{F5}"; "f6"="{F6}";
    "f7"="{F7}"; "f8"="{F8}"; "f9"="{F9}"; "f10"="{F10}"; "f11"="{F11}"; "f12"="{F12}"
  }

  if ($special.ContainsKey($key)) { return $prefix + $special[$key] }
  return $prefix + $key
}

switch ($Action) {
  "bounds" {
    Write-Json (Get-BoundsInfo)
  }
  "position" {
    Write-Json (Get-MousePosition)
  }
  "screenshot" {
    Save-Screenshot $Out
  }
  "move" {
    Move-Mouse $X $Y
    Write-Json ([ordered]@{ ok = $true; position = Get-MousePosition })
  }
  "click" {
    Click-Mouse $X $Y "left"
    Write-Json ([ordered]@{ ok = $true; action = "click"; position = Get-MousePosition })
  }
  "doubleclick" {
    Click-Mouse $X $Y "left"
    Start-Sleep -Milliseconds 90
    Click-Mouse $X $Y "left"
    Write-Json ([ordered]@{ ok = $true; action = "doubleclick"; position = Get-MousePosition })
  }
  "rightclick" {
    Click-Mouse $X $Y "right"
    Write-Json ([ordered]@{ ok = $true; action = "rightclick"; position = Get-MousePosition })
  }
  "scroll" {
    [NativeMouse]::mouse_event($MouseWheel, 0, 0, [uint32]$Delta, [UIntPtr]::Zero)
    Write-Json ([ordered]@{ ok = $true; action = "scroll"; delta = $Delta })
  }
  "type" {
    Send-Text $Text
    Write-Json ([ordered]@{ ok = $true; action = "type"; length = $Text.Length })
  }
  "hotkey" {
    $shell = New-Object -ComObject WScript.Shell
    $shell.SendKeys((Convert-Hotkey $Text))
    Write-Json ([ordered]@{ ok = $true; action = "hotkey"; hotkey = $Text })
  }
  "wait" {
    Start-Sleep -Milliseconds ([int]($Seconds * 1000))
    Write-Json ([ordered]@{ ok = $true; action = "wait"; seconds = $Seconds })
  }
}
