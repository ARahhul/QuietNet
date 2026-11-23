<#
Creates (or updates) the QuietNet DNS gateway VM on Azure. Safe to re-run.

  .\infra\azure-vm.ps1                                  # Central India, Standard_B2als_v2 (2 vCPU, 4 GB)
  .\infra\azure-vm.ps1 -Size Standard_B2ats_v2   # 2 vCPU, 1 GB, cheaper
  .\infra\azure-vm.ps1                                  # again later: re-opens SSH for your new home IP

What it makes, all in one resource group:
  * Ubuntu 24.04 VM with a static Standard public IP;
  * an NSG that allows 53, 443 and 853 (TCP and UDP) from anywhere,
    and SSH (22) only from this laptop's current public IP;
  * a dedicated SSH key, ~\.ssh\quietnet_azure (created if missing; no passphrase, so the tunnel can reconnect by itself).
Port 3000 (the AdGuard admin API) is never opened; it is reached through the SSH tunnel.

Needs the Azure CLI, signed in:  winget install Microsoft.AzureCLI;  az login
#>
param(
    [string]$ResourceGroup = "quietnet-rg",
    [string]$Location = "centralindia",
    [string]$VmName = "quietnet-dns",
    [string]$Size = "Standard_B2als_v2",
    [string]$AdminUser = "azureuser",
    [string]$Key = "$HOME\.ssh\quietnet_azure",
    # CIDR allowed to SSH in. Default: this laptop's current public IP (/32).
    [string]$SshFrom = ""
)
$ErrorActionPreference = "Stop"

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    $cli = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin"
    if (Test-Path "$cli\az.cmd") { $env:PATH = "$cli;$env:PATH" } else { throw "Azure CLI not found. Run: winget install Microsoft.AzureCLI" }
}

# Windows PowerShell 5.1 turns any native stderr line into a terminating error under "Stop",
# so az runs with "Continue" and failures are judged by its exit code.
function Invoke-Az {
    $ErrorActionPreference = "Continue"
    $out = & az @args --only-show-errors 2>&1
    if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed:`n$out" }
    $out
}
function Test-Az {
    $ErrorActionPreference = "Continue"
    $out = & az @args --only-show-errors 2>$null
    ($LASTEXITCODE -eq 0) -and [bool]$out
}

$account = Invoke-Az account show --query "{name:name, id:id}" -o tsv
Write-Host "==> Subscription: $account"

# New subscriptions (Azure for Students included) start with these providers unregistered.
foreach ($ns in "Microsoft.Compute", "Microsoft.Network") {
    $state = Invoke-Az provider show -n $ns --query registrationState -o tsv
    if ("$state".Trim() -ne "Registered") {
        Write-Host "==> Registering $ns for this subscription (one time, about a minute)"
        Invoke-Az provider register -n $ns --wait -o none | Out-Null
    }
}

if (-not (Test-Path $Key)) {
    Write-Host "==> Creating SSH key $Key"
    # PowerShell 5.1 and 7.0-7.2 drop an empty native argument; newer versions pass '' as-is.
    $empty = if ($PSNativeCommandArgumentPassing -and $PSNativeCommandArgumentPassing -ne "Legacy") { "" } else { '""' }
    & ssh-keygen -q -t ed25519 -f $Key -N $empty -C "quietnet-azure"
    if ($LASTEXITCODE -ne 0) { throw "ssh-keygen failed" }
}

if (-not $SshFrom) {
    $ip = (Invoke-RestMethod -Uri "https://api.ipify.org" -TimeoutSec 10).Trim()
    $SshFrom = "$ip/32"
}
Write-Host "==> SSH will be allowed from $SshFrom"

Write-Host "==> Resource group $ResourceGroup ($Location)"
Invoke-Az group create -n $ResourceGroup -l $Location --tags app=quietnet -o none | Out-Null

$nsg = "$VmName-nsg"
$pip = "$VmName-ip"
if (Test-Az vm show -g $ResourceGroup -n $VmName --query id -o tsv) {
    Write-Host "==> VM $VmName already exists; leaving it as is"
} else {
    Write-Host "==> Creating VM $VmName ($Size, Ubuntu 24.04). This takes 1-3 minutes."
    Invoke-Az vm create -g $ResourceGroup -n $VmName -l $Location `
        --image Ubuntu2404 --size $Size `
        --admin-username $AdminUser --ssh-key-values "$Key.pub" `
        --public-ip-address $pip --public-ip-sku Standard --public-ip-address-allocation static `
        --nsg $nsg --nsg-rule NONE `
        --os-disk-size-gb 30 --storage-sku StandardSSD_LRS `
        --tags app=quietnet -o none | Out-Null
}

function Set-NsgRule([string]$Name, [int]$Priority, [string]$Protocol, [string[]]$Ports, [string]$Source) {
    $verb = if (Test-Az network nsg rule show -g $ResourceGroup --nsg-name $nsg -n $Name --query id -o tsv) { "update" } else { "create" }
    Invoke-Az network nsg rule $verb -g $ResourceGroup --nsg-name $nsg -n $Name `
        --priority $Priority --direction Inbound --access Allow --protocol $Protocol `
        --source-address-prefixes $Source --source-port-ranges "*" `
        --destination-address-prefixes "*" --destination-port-ranges @Ports -o none | Out-Null
}

Write-Host "==> Firewall: DNS ports open to all, SSH only from $SshFrom"
Set-NsgRule "allow-ssh-home" 100 "Tcp" @("22") $SshFrom
Set-NsgRule "allow-dns" 110 "*" @("53", "443", "853") "*"

$vmIp = Invoke-Az network public-ip show -g $ResourceGroup -n $pip --query ipAddress -o tsv
$vmIp = "$vmIp".Trim()

Write-Host ""
Write-Host "VM ready: $AdminUser@$vmIp   (key: $Key)"
Write-Host ""
Write-Host "Next (infra\README.md, Option C: Azure):"
Write-Host "  scp -i $Key -r infra ${AdminUser}@${vmIp}:~/pahra-infra"
Write-Host "  ssh -i $Key ${AdminUser}@${vmIp}        then: cd ~/pahra-infra && bash vm-setup.sh"
Write-Host "  Tunnel later: .\infra\tunnel.ps1 -VmHost $vmIp -User $AdminUser -Key $Key"
Write-Host ""
Write-Host "Home IP changed (CGNAT does that) and SSH times out? Re-run this script."
