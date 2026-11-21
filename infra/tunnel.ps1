# Keeps the laptop -> VM SSH tunnel up (AdGuard admin API on http://127.0.0.1:3000).
#   .\infra\tunnel.ps1 -VmHost 203.0.113.10 [-User ubuntu] [-Key ~\.ssh\pahra.pem]
param(
    [Parameter(Mandatory = $true)][string]$VmHost,
    [string]$User = "ubuntu",
    [string]$Key = "",
    [int]$LocalPort = 3000
)

$sshArgs = @("-N", "-L", "${LocalPort}:127.0.0.1:3000",
    "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
    "-o", "ExitOnForwardFailure=yes", "-o", "StrictHostKeyChecking=accept-new")
if ($Key) { $sshArgs += @("-i", $Key) }
$sshArgs += "$User@$VmHost"

while ($true) {
    Write-Host "$(Get-Date -Format T) tunnel up: http://127.0.0.1:$LocalPort -> ${VmHost}:3000 (Ctrl+C to stop)"
    & ssh @sshArgs
    Write-Host "$(Get-Date -Format T) tunnel dropped (exit $LASTEXITCODE); reconnecting in 3 s..."
    Start-Sleep -Seconds 3
}
