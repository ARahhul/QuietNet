# Upload backend/ and infra/ to the VM (~/quietnet) and, with -Up, rebuild and restart the stack there.
#   .\infra\deploy-azure.ps1              # upload only
#   .\infra\deploy-azure.ps1 -Up          # upload, then run cloud-up.sh on the VM
# First time only, between the two: store the secrets on the VM (you paste them; they never pass through here)
#   ssh -t -i $HOME\.ssh\quietnet_azure azureuser@20.219.129.91 bash quietnet/infra/cloud-secrets.sh quietnet.in
# State on the VM is never overwritten: backend.env, .env, agd-work/, agd-conf/ and the Docker volumes stay.
param(
    [string]$Domain = "quietnet.in",
    [string]$VmHost = "20.219.129.91",
    [string]$User = "azureuser",
    [string]$Key = "$HOME\.ssh\quietnet_azure",
    [switch]$Up
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$target = "$User@$VmHost"
$ssh = @("-i", $Key, "-o", "ConnectTimeout=15")
$archive = Join-Path ([IO.Path]::GetTempPath()) "quietnet-deploy.tgz"

Write-Host "==> Packing backend/ and infra/"
# Windows' bsdtar; leave out local state, secrets and caches
tar -czf $archive -C $repo `
    --exclude=.venv --exclude=.cache --exclude=.pytest_cache --exclude=__pycache__ `
    --exclude=*.db --exclude=*.db-* --exclude=.env --exclude=backend.env `
    --exclude=agd-work --exclude=agd-conf `
    backend infra
if ($LASTEXITCODE -ne 0) { throw "tar failed" }
"{0:N0} KB" -f ((Get-Item $archive).Length / 1KB) | Write-Host

Write-Host "==> Uploading to ${target}:~/quietnet"
scp @ssh -q $archive "${target}:/tmp/quietnet-deploy.tgz"
if ($LASTEXITCODE -ne 0) { throw "scp failed. Is SSH allowed from this network? (re-run infra\azure-vm.ps1 if your home IP changed)" }
ssh @ssh $target "mkdir -p ~/quietnet && tar -xzf /tmp/quietnet-deploy.tgz -C ~/quietnet && rm /tmp/quietnet-deploy.tgz && chmod 755 ~/quietnet ~/quietnet/infra ~/quietnet/backend"
if ($LASTEXITCODE -ne 0) { throw "unpacking on the VM failed" }
Remove-Item $archive

if ($Up) {
    Write-Host "==> Running cloud-up.sh on the VM"
    ssh @ssh -t $target "bash ~/quietnet/infra/cloud-up.sh $Domain"
    if ($LASTEXITCODE -ne 0) { throw "cloud-up.sh failed (see above)" }
} else {
    Write-Host "Uploaded. Next on the VM:  bash ~/quietnet/infra/cloud-up.sh $Domain   (or re-run this with -Up)"
}
