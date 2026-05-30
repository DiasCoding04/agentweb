$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
  throw "Khong tim thay venv. Chay: python -m venv venv ; .\venv\Scripts\pip install -r requirements.txt"
}

& $venvPython check_key.py
if ($LASTEXITCODE -ne 0) {
  Write-Host ""
  Write-Host "Can thiet lap API key Gemini (1 lan duy nhat):" -ForegroundColor Yellow
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "Setup Gemini Key.ps1")
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

function Test-Server {
  try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/models" -UseBasicParsing -TimeoutSec 2
    return $r.StatusCode -eq 200
  } catch {
    return $false
  }
}

if (-not (Test-Server)) {
  Start-Process -FilePath $venvPython -ArgumentList "server.py" -WorkingDirectory $PSScriptRoot -WindowStyle Normal
  for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 500
    if (Test-Server) { break }
  }
  if (-not (Test-Server)) {
    throw "Server khong khoi dong duoc. Kiem tra cua so python server.py."
  }
}

Start-Process "http://127.0.0.1:8000/"
