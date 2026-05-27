$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Disable-ConsoleQuickEdit {
  if ($env:OS -notlike "*Windows*") {
    return
  }

  Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class ConsoleMode {
  [DllImport("kernel32.dll")]
  public static extern IntPtr GetStdHandle(int nStdHandle);

  [DllImport("kernel32.dll")]
  public static extern bool GetConsoleMode(IntPtr hConsoleHandle, out uint lpMode);

  [DllImport("kernel32.dll")]
  public static extern bool SetConsoleMode(IntPtr hConsoleHandle, uint dwMode);
}
"@

  $handle = [ConsoleMode]::GetStdHandle(-10)
  $mode = 0
  if ([ConsoleMode]::GetConsoleMode($handle, [ref]$mode)) {
    $ENABLE_EXTENDED_FLAGS = 0x0080
    $ENABLE_QUICK_EDIT_MODE = 0x0040
    $newMode = ($mode -bor $ENABLE_EXTENDED_FLAGS) -band (-bnot $ENABLE_QUICK_EDIT_MODE)
    [ConsoleMode]::SetConsoleMode($handle, $newMode) | Out-Null
  }
}

function Write-Step($message) {
  Write-Host ""
  Write-Host "==> $message" -ForegroundColor Cyan
}

function Test-NodeVersion {
  if (-not (Get-Command node.exe -ErrorAction SilentlyContinue)) {
    return $false
  }

  $major = node.exe -p "Number(process.versions.node.split('.')[0])"
  $minor = node.exe -p "Number(process.versions.node.split('.')[1])"

  if ([int]$major -gt 22) {
    return $true
  }

  return ([int]$major -eq 22 -and [int]$minor -ge 16)
}

Disable-ConsoleQuickEdit

Write-Step "Checking Node.js"
if (-not (Test-NodeVersion)) {
  Write-Step "Installing Node.js LTS with winget"
  if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
    throw "winget is not available. Install Node.js 24 LTS from https://nodejs.org, then run this installer again."
  }

  winget.exe install OpenJS.NodeJS.LTS --silent --accept-package-agreements --accept-source-agreements
  $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")

  if (-not (Test-NodeVersion)) {
    throw "Node.js install completed, but node.exe is still not on PATH. Open a new PowerShell window and run this installer again."
  }
}

Write-Host "Node: $(node.exe -v)"

Write-Step "Installing or updating OpenClaw"
Write-Host "Do not click or drag inside this black window while npm is running. If the title says Select, press Esc." -ForegroundColor Yellow
npm.cmd install -g openclaw@latest
Write-Host "OpenClaw: $(openclaw.cmd --version)"

Write-Step "Creating local OpenClaw config"
$geminiKey = Read-Host "Paste Gemini API key, or press Enter to skip AI setup"

$onboardArgs = @(
  "onboard",
  "--non-interactive",
  "--accept-risk",
  "--mode", "local",
  "--gateway-auth", "token",
  "--gateway-bind", "loopback",
  "--skip-channels",
  "--skip-search",
  "--skip-skills",
  "--skip-health",
  "--no-install-daemon",
  "--json"
)

if ($geminiKey.Trim().Length -gt 0) {
  $onboardArgs += @("--auth-choice", "gemini-api-key", "--gemini-api-key", $geminiKey.Trim())
} else {
  $onboardArgs += @("--auth-choice", "skip")
}

openclaw.cmd @onboardArgs | Out-Host

Write-Step "Applying fast local computer-control profile"
$configPath = Join-Path $env:USERPROFILE ".openclaw\openclaw.json"
$cfg = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json

if (-not $cfg.plugins) {
  $cfg | Add-Member -NotePropertyName plugins -NotePropertyValue ([pscustomobject]@{})
}
if (-not $cfg.plugins.entries) {
  $cfg.plugins | Add-Member -NotePropertyName entries -NotePropertyValue ([pscustomobject]@{})
}

$keepPlugins = @("google", "browser", "device-pair", "document-extract", "file-transfer", "web-readability")
$allPlugins = @(
  "active-memory","admin-http-rpc","alibaba","anthropic","arcee","azure-speech","bonjour","browser","byteplus","canvas",
  "cerebras","chutes","clickclack","cloudflare-ai-gateway","comfy","copilot-proxy","deepgram","deepinfra","deepseek",
  "device-pair","document-extract","duckduckgo","elevenlabs","exa","fal","file-transfer","firecrawl","fireworks",
  "github-copilot","google","gradium","groq","huggingface","imessage","inworld","irc","kilocode","kimi","litellm",
  "llm-task","lmstudio","mattermost","memory-core","memory-wiki","microsoft","microsoft-foundry","migrate-claude",
  "migrate-hermes","minimax","mistral","moonshot","nvidia","oc-path","ollama","open-prose","openai","opencode",
  "opencode-go","openrouter","perplexity","phone-control","policy","qianfan","qwen","runway","searxng","senseaudio",
  "sglang","signal","skill-workshop","stepfun","synthetic","talk-voice","tavily","telegram","tencent",
  "thread-ownership","together","tokenjuice","tts-local-cli","venice","vercel-ai-gateway","vllm","volcengine",
  "voyage","vydra","web-readability","webhooks","xai","xiaomi","zai"
)

$cfg.plugins | Add-Member -NotePropertyName allow -NotePropertyValue $keepPlugins -Force
$cfg.plugins | Add-Member -NotePropertyName bundledDiscovery -NotePropertyValue "allowlist" -Force
foreach ($id in $allPlugins) {
  $entry = $cfg.plugins.entries.$id
  if (-not $entry) {
    $entry = [pscustomobject]@{}
    $cfg.plugins.entries | Add-Member -NotePropertyName $id -NotePropertyValue $entry
  }
  $entry | Add-Member -NotePropertyName enabled -NotePropertyValue ($keepPlugins -contains $id) -Force
}

if (-not $cfg.agents) {
  $cfg | Add-Member -NotePropertyName agents -NotePropertyValue ([pscustomobject]@{})
}
if (-not $cfg.agents.defaults) {
  $cfg.agents | Add-Member -NotePropertyName defaults -NotePropertyValue ([pscustomobject]@{})
}
$cfg.agents.defaults | Add-Member -NotePropertyName thinkingDefault -NotePropertyValue "off" -Force
$cfg.agents.defaults | Add-Member -NotePropertyName maxConcurrent -NotePropertyValue 1 -Force
if (-not $cfg.agents.defaults.subagents) {
  $cfg.agents.defaults | Add-Member -NotePropertyName subagents -NotePropertyValue ([pscustomobject]@{})
}
$cfg.agents.defaults.subagents | Add-Member -NotePropertyName maxConcurrent -NotePropertyValue 2 -Force
$cfg.agents.defaults | Add-Member -NotePropertyName timeoutSeconds -NotePropertyValue 300 -Force

$cfg | ConvertTo-Json -Depth 50 | Set-Content -LiteralPath $configPath -Encoding UTF8
openclaw.cmd config validate | Out-Host

Write-Step "Installing launcher files"
$installDir = Join-Path $env:LOCALAPPDATA "OpenClawOneClick"
New-Item -ItemType Directory -Force -Path $installDir | Out-Null
Copy-Item -Force -LiteralPath (Join-Path $PSScriptRoot "OpenClaw Dashboard.cmd") -Destination $installDir
Copy-Item -Force -LiteralPath (Join-Path $PSScriptRoot "OpenClaw Dashboard.ps1") -Destination $installDir
Copy-Item -Force -LiteralPath (Join-Path $PSScriptRoot "Configure OpenClaw AI.cmd") -Destination $installDir

Write-Step "Creating Desktop shortcuts"
$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell

$dashboardShortcut = $shell.CreateShortcut((Join-Path $desktop "OpenClaw Dashboard.lnk"))
$dashboardShortcut.TargetPath = Join-Path $installDir "OpenClaw Dashboard.cmd"
$dashboardShortcut.WorkingDirectory = $installDir
$dashboardShortcut.IconLocation = "C:\Program Files\nodejs\node.exe,0"
$dashboardShortcut.Description = "Start OpenClaw Gateway and open the local dashboard"
$dashboardShortcut.Save()

$configureShortcut = $shell.CreateShortcut((Join-Path $desktop "Configure OpenClaw AI.lnk"))
$configureShortcut.TargetPath = Join-Path $installDir "Configure OpenClaw AI.cmd"
$configureShortcut.WorkingDirectory = $installDir
$configureShortcut.IconLocation = "C:\Windows\System32\shell32.dll,167"
$configureShortcut.Description = "Configure OpenClaw model provider and API keys"
$configureShortcut.Save()

Write-Step "Smoke testing Gateway"
Start-Process -FilePath "openclaw.cmd" -ArgumentList @("gateway", "run", "--force") -WindowStyle Minimized
Start-Sleep -Seconds 12
openclaw.cmd gateway call health | Out-Host

Write-Step "Opening Dashboard"
& (Join-Path $installDir "OpenClaw Dashboard.cmd")

Write-Host ""
Write-Host "Done. Use the Desktop shortcut: OpenClaw Dashboard" -ForegroundColor Green
Write-Host "If chat says missing API key, run: Configure OpenClaw AI" -ForegroundColor Yellow
