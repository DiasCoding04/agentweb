$ErrorActionPreference = "Stop"

$agentDir = Join-Path $PSScriptRoot "fast-agent"
$url = "http://127.0.0.1:18792/"

function Test-FastAgent {
  try {
    $res = Invoke-WebRequest -Uri ($url + "health") -UseBasicParsing -TimeoutSec 2
    return $res.StatusCode -eq 200
  } catch {
    return $false
  }
}

if (-not (Test-FastAgent)) {
  Start-Process -FilePath "node.exe" -ArgumentList "server.js" -WorkingDirectory $agentDir -WindowStyle Hidden
  for ($i = 0; $i -lt 12; $i++) {
    Start-Sleep -Milliseconds 500
    if (Test-FastAgent) { break }
  }
}

Start-Process $url
