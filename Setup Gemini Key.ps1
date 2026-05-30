$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

Add-Type -AssemblyName Microsoft.VisualBasic

$hint = "Dan key tu https://aistudio.google.com/apikey`n(Dan 1 lan - app tu luu vao local\gemini.key)"

$key = [Microsoft.VisualBasic.Interaction]::InputBox($hint, "Setup Gemini API Key", "")
if ([string]::IsNullOrWhiteSpace($key)) {
  Write-Host "Da huy - chua luu key." -ForegroundColor Yellow
  exit 1
}

$venvPython = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
  throw "Khong tim thay venv. Chay: python -m venv venv ; .\venv\Scripts\pip install -r requirements.txt"
}

$env:NEW_GEMINI_KEY = $key.Trim()
& $venvPython save_key.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Xong! Bay gio chay Start AI Browser Agent.cmd" -ForegroundColor Green
Start-Sleep -Seconds 2
