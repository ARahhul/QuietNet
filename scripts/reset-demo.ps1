<#
Undo every block YOU made (household and per-device) so the next rehearsal starts with all cards back.
Automatic decisions (malware blocks, quarantines, auto-mode) are left alone.

  .\scripts\reset-demo.ps1                     # API on http://127.0.0.1:8000, token dev-token
  .\scripts\reset-demo.ps1 -Token <API_TOKEN>
#>
param([string]$Api = "http://127.0.0.1:8000", [string]$Token = "dev-token")
$ErrorActionPreference = "Stop"
$h = @{ Authorization = "Bearer $Token" }
$active = Invoke-RestMethod -Uri "$Api/api/decisions" -Headers $h
$mine = @($active | Where-Object { $_.decided_by -eq "user" })
if (-not $mine.Count) { Write-Host "Nothing to undo."; return }
$body = @{ ids = @($mine | ForEach-Object { $_.id }) } | ConvertTo-Json
$r = Invoke-RestMethod -Method Post -Uri "$Api/api/decisions/undo-bulk" -Headers $h -ContentType "application/json" -Body $body
Write-Host "Undid $($r.count) decisions: $(($mine | ForEach-Object { $_.domain }) -join ', ')"
Write-Host "Toggle Airplane Mode on the phone for 2 s so it forgets cached blocks."
