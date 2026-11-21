# Pahra demo runbook

Target, from the PRD:
* Connect a phone live in **under 60 seconds**.
* Show **10+ trackers** explained.
* **"Block all ads"**, and the ads are gone on refresh.
* **Close the laptop**, and the phone is still protected.

The whole run takes under 3 minutes.

## The day before

1. **VM up:**
   * Follow [infra/README.md](infra/README.md).
   * `https://dns.<domain>/` must **not** show the AdGuard login. If it does, apply the hardening step.
   * The certificate must be valid for more than 7 days: `sudo certbot certificates` on the VM.
2. **Laptop:**
   * Run `.\scripts\dev.ps1 -Real -VmHost <VM_IP>`.
   * Settings shows every row green: gateway, poller, rule sync, lists, companion.
3. **Phone:**
   * Add it in **Setup → Android phone** and set Private DNS once.
   * Then delete it, so you can re-add it live on stage. A re-added phone gets a new name, so on stage you type the new hostname.
4. **TVs** (only the ones you will show):
   * **Fire TV / Google TV:**
     * Install adb: `winget install Google.PlatformTools`.
     * Turn on ADB debugging on the TV.
     * Run **Connect my TV** once and tick "Always allow" on the TV's debugging prompt.
   * **LG / Samsung at home** (behind CGNAT):
     * **Connect my TV**, then **Allow** the laptop relay. Type the laptop IP into the TV's DNS.
     * Allow DNS through Windows Firewall once, as admin, using the command the assistant shows.
     * Give the laptop a fixed IP, or a DHCP reservation, on the router.
   * **LG / Samsung at the venue:** use a phone hotspot and the manual DNS guide instead.
5. **Explanations cached.** With `GEMINI_API_KEY` in `backend\.env`, run this so cards never wait on Gemini live:
   ```powershell
   cd backend; poetry run python -m devtools.prewarm
   ```
6. **Backup video:** record one full run and keep it open in a tab.

## 30 minutes before

- [ ] **Settings:** everything green. **Laptop DNS relay** shows your TVs, if you use it.
- [ ] Run `.\scripts\reset-demo.ps1` so all cards are back. Then toggle Airplane Mode on the phone.
- [ ] Pick an ad-heavy site you **haven't opened on that phone today**.
- [ ] Laptop on power, notifications off, and the browser zoomed to 125 %.
- [ ] **Relay users:** the "close the laptop" moment cuts DNS for relay TVs. Only show it with the phone, which talks to the VM directly.

## Run of show

| Time | Do | Say |
|---|---|---|
| 0:00 | Open the dashboard | "Every device at home quietly talks to dozens of companies. Pahra shows you who, in plain words." |
| 0:15 | **Setup → Android phone**: name it, scan the QR on the phone, paste it into Private DNS | "One setting, no app." The page turns green within about 30 s |
| 0:50 | **Suggestions** | Point at the count ("15 things from 10 companies") and at the **"Watches your screen"** card for the TV, with its confidence and sources |
| 1:20 | **Block all ads & trackers**, then Airplane Mode for 2 s, then refresh the ad-heavy site | "Gone. Per device, undoable. Netflix and updates are never touched." |
| 1:50 | **Alerts**: the LG Live Plus card | "We don't just block, we tell you how to switch it off at the source." |
| 2:10 | *(optional)* **Connect my TV** on the Fire TV, then **Allow** twice | The live scan, then **Connected**. Or show the LG relay that's already set up |
| 2:40 | **Close the laptop**, refresh on the phone | "The phone talks to Pahra's DNS server directly. Still blocked." |
| 2:55 | **Report** | "This week: N lookups, X % blocked." |

## When something goes wrong

| Symptom | Fix on stage |
|---|---|
| A block doesn't show | Toggle Airplane Mode again and try another site you haven't opened. Blocked answers expire in 10 s |
| The setup page doesn't turn green | Check the hostname for typos and that the phone has data. Otherwise open the backup video |
| A Gemini error shows in Settings | Nothing to do: cards fall back to list text and the assistant runs step by step |
| Companion is red | Restart the API window (it restarts the companion). The relay comes back on its own |
| The TV won't connect live | Say "here's the guided path" and show the guide card, or play the video |
| The VM or tunnel is down | Run `.\scripts\dev.ps1` (local fake) and do the story on simulated traffic |

## Reset between rehearsals

```powershell
.\scripts\reset-demo.ps1
```
Then toggle Airplane Mode on the phone. For a fully clean local run, close the windows and delete `backend\pahra-dev.db`.
