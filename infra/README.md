# QuietNet infra: set up the cloud DNS server

The VM runs **only AdGuard Home**. Everything else runs on the laptop and reaches AdGuard through an SSH tunnel:

* the FastAPI engine;
* SQLite;
* Gemini;
* the dashboard;
* the TV companion.

Time: about 1 hour, most of it waiting for Cloudflare and signing up.

**You need:**

* A domain name (about ₹100–800 a year).
* A free Cloudflare account.
* A cloud account: Oracle Always Free (₹0), Google Cloud, or Azure (student credits work).
* This laptop, with OpenSSH. It's built into Windows 10 and 11.

Throughout, replace `example.com` with your domain and `203.0.113.10` with your VM's IP.

---

## 1. Domain and Cloudflare (15 min, then wait)

1. Buy a domain from any registrar. Cloudflare Registrar, Namecheap and GoDaddy all work.
2. Make a free account at Cloudflare. Then **Add a site**, enter the domain, and pick the **Free** plan.
3. Cloudflare shows two nameservers. At your registrar, replace the domain's nameservers with those two.
4. Wait until Cloudflare emails that the site is **Active**. It's usually under an hour.
5. Create an API token for the certificate:
   1. Go to **My Profile → API Tokens → Create Token**, then the **Edit zone DNS** template.
   2. Under **Zone Resources**, choose Include → Specific zone → your domain.
   3. Create it and copy the token. It's shown only once.

**Why Cloudflare:** each device gets its own name, like `pixel-phone.dns.example.com`. That needs a *wildcard* certificate, which Let's Encrypt only issues through a DNS check. Cloudflare's API makes that check automatic, including renewals.

## 2. Create the VM (15 min)

### Option A: Oracle Always Free (recommended: ₹0, servers in India)

1. Sign up at Oracle Cloud. A card is needed for verification only.
   * **Choose your home region carefully:** India West (Mumbai) or India South (Hyderabad).
   * It can't be changed later, and Always Free VMs only run in the home region.
2. Go to **Compute → Instances → Create instance**:
   * **Image:** Canonical Ubuntu 22.04 or 24.04.
   * **Shape:** `VM.Standard.E2.1.Micro` (Always Free) is enough. Ampere A1 with 1 OCPU and 6 GB is roomier if it's offered.
   * **Networking:** create a new VCN with a public subnet, and tick **Assign a public IPv4 address**.
   * **SSH keys:** choose **Generate a key pair** and **download the private key**. Save it as `C:\Users\<you>\.ssh\pahra.key`.
3. Open the DNS ports:
   1. On the instance page, open its subnet, then **Security List → Add Ingress Rules**.
   2. Add two rules with source `0.0.0.0/0`:
      * TCP destination ports `53,443,853`;
      * UDP destination ports `53,443,853`.
   3. **Do not open 3000.**
4. Copy the instance's **Public IP**. The SSH user is `ubuntu`.

### Option B: Google Cloud (Mumbai)

* Create an `e2-small` VM (Ubuntu 22.04/24.04) in `asia-south1`, with network tag `pahra-dns`. It isn't free outside the trial credit. The free `e2-micro` only exists in US regions, which are too far away for DNS.
* Open the DNS ports. Use Cloud Shell, or the console under **VPC network → Firewall**:
  ```bash
  gcloud compute firewall-rules create pahra-dns --target-tags pahra-dns \
    --allow tcp:53,tcp:443,tcp:853,udp:53,udp:443,udp:853
  ```
* The SSH user is your Google username. Pass it as `-User` to `tunnel.ps1` and `dev.ps1`.

### Option C: Azure (Central India)

`azure-vm.ps1` builds everything from the laptop. It's safe to re-run.

```powershell
winget install Microsoft.AzureCLI      # then open a new terminal
az login --use-device-code             # the browser sign-in window doesn't always appear
.\infra\azure-vm.ps1                   # Standard_B2als_v2 (2 vCPU, 4 GB) in centralindia
```

**Azure for Students limits regions and sizes.** It only allows a few regions; Central India is one of them. It also blocks the classic B1s and B2s, so the script uses `Standard_B2als_v2`. `-Size Standard_B2ats_v2` (2 vCPU, 1 GB) is cheaper and still enough for AdGuard. To see which sizes your subscription can use:

```powershell
az vm list-skus -l centralindia --resource-type virtualMachines --query "[?starts_with(name,'Standard_B') && length(restrictions)==``0``].name" -o tsv
```

It creates the resource group `quietnet-rg` with:

* an Ubuntu 24.04 VM, `quietnet-dns`, with a static public IP. The script prints the IP at the end;
* a network security group that opens 53, 443 and 853 (TCP and UDP) to everyone, and SSH only to this laptop's current public IP. **It never opens 3000**;
* a dedicated SSH key, `~\.ssh\quietnet_azure`.

On Azure, the SSH user is `azureuser` and the key is `$HOME\.ssh\quietnet_azure`. In steps 4 and 5, and with `dev.ps1`, use them in place of `ubuntu` and `pahra.key`. For example: `-User azureuser -Key $HOME\.ssh\quietnet_azure`.

**CGNAT moves your home IP now and then.** When SSH or the tunnel starts timing out, re-run `.\infra\azure-vm.ps1`. It points the SSH rule at your new IP and leaves the VM alone.

## 3. Point the names at the VM (2 min)

Go to Cloudflare → your domain → **DNS → Add record**, and add two records:

| Type | Name | Content | Proxy status |
|---|---|---|---|
| A | `dns` | `203.0.113.10` | **DNS only** (grey cloud) |
| A | `*.dns` | `203.0.113.10` | **DNS only** (grey cloud) |

The orange cloud (proxied) breaks DoT. Both records must be grey.

## 4. Install the server (10 min)

**On the laptop (PowerShell).** Copy the setup files to the VM and log in:

```powershell
$key = "$HOME\.ssh\pahra.key"
icacls $key /inheritance:r /grant:r "$($env:USERNAME):(R)"   # ssh refuses keys that others can read
scp -i $key -r C:\Projects\pahra\infra ubuntu@203.0.113.10:~/pahra-infra
ssh -i $key ubuntu@203.0.113.10
```

**On the VM.** Install Docker and certbot, free port 53, and open the firewall:

```bash
cd ~/pahra-infra
bash vm-setup.sh
mkdir -p ~/.secrets && nano ~/.secrets/cloudflare.ini
```

In nano, paste this one line, then save with Ctrl+O, Enter, Ctrl+X:

```
dns_cloudflare_api_token = <the token from step 1>
```

Using nano keeps the token out of your shell history. Then get the certificate and start AdGuard:

```bash
bash issue-cert.sh dns.example.com you@example.com
sudo docker compose up -d
sudo docker ps            # pahra-dns-gateway should be "Up"
exit
```

## 5. Configure AdGuard from the laptop (5 min)

**Start the tunnel** in its own window and leave it running. It reconnects by itself:

```powershell
.\infra\tunnel.ps1 -VmHost 203.0.113.10 -Key $HOME\.ssh\pahra.key
```

**Configure AdGuard.** Choose a strong admin password, then run:

```powershell
python infra\adguard_init.py --dns-host dns.example.com --password '<strong password>'
```

Each step is safe to re-run. It does the following:

* finishes AdGuard's first-run wizard;
* turns on DoT and DoH with the wildcard certificate;
* sets the upstream resolvers and the demo cache times (blocked answers 10 s, allowed answers at most 30 s);
* keeps 24 h of query log;
* limits the global lists to malware and phishing only;
* **closes the resolver to strangers** until QuietNet adds your devices.

## 6. Connect the backend (5 min)

1. Copy `backend\.env.example` to `backend\.env`.
2. Set these values:
   ```
   ADGUARD_URL=http://127.0.0.1:3000
   ADGUARD_USER=admin
   ADGUARD_PASS=<the password from step 5>
   DNS_HOSTNAME=dns.example.com
   VM_IP=203.0.113.10
   API_TOKEN=<a long random string>
   GEMINI_API_KEY=<optional>
   ```
3. Create `frontend\.env.local` containing `VITE_API_TOKEN=<the same API_TOKEN>`.
4. Start everything:
   ```powershell
   .\scripts\dev.ps1 -Real          # the tunnel from step 5 is already running
   ```
5. Open http://localhost:5173. On **Settings**, the DNS gateway row should be green.

## 7. Verify (5 min)

1. In the dashboard, go to **Setup → Android phone**, name it `test-phone`, and set the phone's Private DNS to the hostname shown. The page turns green when the first query arrives.
2. From the laptop, on your home WiFi:
   ```powershell
   companion\.venv\Scripts\python infra\verify.py --dns-host dns.example.com --client-id test-phone --vm-ip 203.0.113.10
   ```
   You want four PASS lines:
   * DoT works with the phone's own name;
   * DoH works;
   * the admin page is hidden;
   * port 53 is **REFUSED** from your home network.
3. If **Admin page hidden** fails, AdGuard is serving its login page on :443. Hide it on the VM:
   ```bash
   cd ~/pahra-infra && bash harden.sh dns.example.com
   ```
   After that, only re-run `adguard_init.py` with `--no-https`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `issue-cert.sh` fails with "unauthorized" or a TXT record error | The token can't edit DNS for this zone, or the nameservers aren't active yet. Check Cloudflare says **Active** and that the token has **Zone → DNS → Edit** for this domain |
| `docker compose up` says port 53 is in use | `systemd-resolved` still holds it. Re-run `vm-setup.sh`. Check with `sudo ss -lunp 'sport = :53'` |
| `verify.py` DoT or DoH time out | The cloud firewall is still closed. On Oracle, check the subnet Security List **and** that `vm-setup.sh` printed no iptables errors. On GCP, check the VM has the `pahra-dns` network tag |
| DoT says "certificate verify failed" | The `*.dns` record or the wildcard certificate is missing. Run `sudo certbot certificates` on the VM; it must list `*.dns.example.com` |
| DoT and DoH return REFUSED | That client id isn't in QuietNet yet. Add the device in the dashboard first: the backend adds it to AdGuard's allowed list |
| Port 53 answers (NOERROR) instead of REFUSED | The backend isn't running, or `ADGUARD_LOCK_ACCESS=false`. Start the backend and check **Settings → Rule sync** |
| Tunnel window loops on "Permission denied (publickey)" | Wrong user or key. Oracle uses `ubuntu`; GCP uses your Google username (`-User`); Azure uses `azureuser` with `quietnet_azure` |
| Azure: SSH or the tunnel times out, but DNS still works | Your home IP changed, so the SSH rule no longer matches it. Re-run `.\infra\azure-vm.ps1` |
| Settings shows the DNS gateway red | The tunnel is down. Look at the tunnel window, then check `curl.exe -u admin:<pw> http://127.0.0.1:3000/control/status` |

## How it's put together

| File | Runs on | Does |
|---|---|---|
| `docker-compose.yml` | VM | AdGuard Home. 53/443/853 are public; the admin API is only on 127.0.0.1:3000 |
| `azure-vm.ps1` | laptop | Azure only: creates the VM, static IP, network security group and SSH key; re-run it to update the SSH rule |
| `vm-setup.sh` | VM | Docker, certbot and the Cloudflare plugin, frees :53, and opens the OS firewall (handles Oracle's iptables) |
| `issue-cert.sh` | VM | Wildcard certificate via DNS-01. It renews itself, and the renewal hook restarts AdGuard |
| `adguard_init.py` | laptop | Configures AdGuard through the API |
| `tunnel.ps1` | laptop | Auto-reconnecting `ssh -L 3000` |
| `verify.py` | laptop | DoT, DoH, hidden admin page and closed port 53 checks (replaces kdig and dig on Windows) |
| `harden.sh`, `docker-compose.harden.yml`, `Caddyfile` | VM | Optional: Caddy in front of :443 so only `/dns-query*` is public |

## Gotchas

* **The access list also gates DoT and DoH.** Once `allowed_clients` is non-empty, every device's ClientID must be on it. The backend keeps it up to date, so don't edit it by hand. The `pahra-closed` entry is a placeholder that matches no device.
* **CGNAT (JioFiber, Airtel, ACT).** Router DNS pointed at the VM can't identify your home. Use per-device DoT/DoH, the laptop relay for locked TVs, or a phone hotspot for the demo.
* **Laptop asleep.** Rules already pushed keep working, because the phone talks to the VM directly. New cards, quarantine and auto-mode pause until it wakes. TVs on the laptop relay lose DNS while it sleeps.
