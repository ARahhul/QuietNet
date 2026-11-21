<#
Starts Pahra in separate windows.

  .\scripts\dev.ps1                         # local: fake AdGuard (no VM needed) + API + dashboard
  .\scripts\dev.ps1 -Real -VmHost 1.2.3.4   # real: SSH tunnel to the VM + API (backend\.env) + dashboard
  .\scripts\dev.ps1 -Real                   # real, tunnel already running elsewhere

Local mode uses its own database (backend\pahra-dev.db), so it never touches the real one.
#>
param(
    [switch]$Real,
    [string]$VmHost = "",
    [string]$User = "ubuntu",
    [string]$Key = ""
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$be = Join-Path $root "backend"
$fe = Join-Path $root "frontend"
$py = Join-Path $be ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    throw "Backend venv missing. Run: cd backend; poetry install   (Poetry 2: pipx install poetry)"
}
if (-not (Test-Path (Join-Path $fe "node_modules"))) {
    Write-Host "Installing dashboard packages (first run)..."
    Push-Location $fe; npm install; Pop-Location
}
if (-not (Test-Path (Join-Path $root "companion\.venv\Scripts\python.exe"))) {
    Write-Warning "Companion not installed (cd companion; poetry install): the 'Connect my TV' assistant will be off. See companion\README.md."
}

$pw = Get-Command pwsh -ErrorAction SilentlyContinue
$shell = if ($pw) { $pw.Source } else { "powershell.exe" }

function Start-PahraWindow([string]$Title, [string]$Dir, [string]$Command) {
    $full = "`$Host.UI.RawUI.WindowTitle = '$Title'; $Command"
    Start-Process -FilePath $shell -WorkingDirectory $Dir -ArgumentList @("-NoExit", "-Command", $full) | Out-Null
    Write-Host "  started: $Title"
}

Write-Host "Starting Pahra ($(if ($Real) { 'real VM' } else { 'local, fake AdGuard' }))"
if ($Real) {
    if ($VmHost) {
        $tunnel = Join-Path $root "infra\tunnel.ps1"
        $keyArg = if ($Key) { " -Key '$Key'" } else { "" }
        Start-PahraWindow "Pahra SSH tunnel" $root "& '$tunnel' -VmHost '$VmHost' -User '$User'$keyArg"
        Start-Sleep -Seconds 3
    }
    if (-not (Test-Path (Join-Path $be ".env"))) { Write-Warning "backend\.env not found: copy backend\.env.example and fill it in." }
    Start-PahraWindow "Pahra API" $be "& '$py' -m uvicorn pahra.main:create_app --factory --port 8000"
} else {
    Start-PahraWindow "Pahra fake AdGuard" $be "& '$py' -m devtools.fake_adguard --port 3100"
    $envs = "`$env:ADGUARD_URL='http://127.0.0.1:3100'; `$env:OFFLINE_LISTS='true'; `$env:DB_PATH='pahra-dev.db'; " +
            "`$env:ADGUARD_LOCK_ACCESS='false'; "
    Start-PahraWindow "Pahra API (local)" $be "$envs& '$py' -m uvicorn pahra.main:create_app --factory --port 8000"
}
Start-PahraWindow "Pahra dashboard" $fe "npm run dev"
Write-Host ""
Write-Host "Dashboard: http://localhost:5173   (API http://127.0.0.1:8000/api/health, token from backend\.env API_TOKEN)"
Write-Host "Close the windows to stop."
