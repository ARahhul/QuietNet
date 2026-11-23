# Security policy

QuietNet sits in front of every lookup a home makes, so security reports are taken seriously.

## Reporting a vulnerability

Please **don't open a public issue**. Use GitHub's private reporting instead:

1. Go to the [Security tab](https://github.com/ARahhul/QuietNet/security) of this repository.
2. Choose **Report a vulnerability** and describe what you found, how to reproduce it, and what it could let someone do.

You'll get a reply within a few days. Once it's fixed, you're welcome to be credited in the release notes.

## What is in scope

* The API in `backend/` (authentication, per-account isolation, rate limits).
* The dashboard in `frontend/`.
* The companion in `companion/` (its `adb` allow-list, LAN scanning limits and DNS relay).
* The VM setup and hardening scripts in `infra/`.

Of particular interest:

* One account reading or changing another account's devices, rules or logs.
* A rule that affects a device it wasn't made for (every rule must carry `$client`).
* IP addresses, MAC addresses, hostnames or DNS logs reaching an AI model or a trace.
* The companion running a command outside its fixed allow-list, or scanning outside the user's private subnet.
* The AdGuard admin login or DoH endpoint being reachable without the hardening step.

## Not in scope

* Findings that need an attacker who already controls the VM or the user's laptop.
* Vulnerabilities in third-party software QuietNet runs (AdGuard Home, Caddy, Certbot). Please report those upstream.

## Known limits

The [README](README.md#known-limits) lists limits that are already documented, such as no encryption at rest for `pahra.db`.
