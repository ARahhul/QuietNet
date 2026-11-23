# Contributing to QuietNet

Thanks for wanting to help. QuietNet is small enough that most changes are a single file and a test.

## Ways to help

* **Report a TV or router that didn't work.** Say the brand, model and what the assistant showed. Fingerprints and guide steps in `companion/` haven't all been checked on real hardware, so this is the most useful kind of report.
* **Fix a guide.** The step-by-step guides are YAML in [`companion/pahra_companion/guides/`](companion/pahra_companion/guides) and [`backend/pahra/data/`](backend/pahra/data). No Python needed.
* **Improve the domain data.** [`seed_domains.yaml`](backend/pahra/data/seed_domains.yaml) and [`must_allow.yaml`](backend/pahra/data/must_allow.yaml) decide what is explained and what is never blocked.
* **Fix a bug or build a feature.** Open an issue first for anything big.

## Set up

You need Python 3.11+, [Poetry](https://python-poetry.org/) 2 and Node 20+. The full walkthrough is in the [README](README.md#quick-start); the short version:

```powershell
cd backend;   poetry install; copy .env.example .env
cd companion; poetry install
cd frontend;  npm install
.\scripts\dev.ps1        # fake AdGuard + API + dashboard, no VM needed
```

## Before you open a pull request

Run what CI runs:

```powershell
cd backend;   poetry run pytest -q
cd companion; poetry run pytest -q
cd frontend;  npm run lint; npm run build
```

## Ground rules

These keep QuietNet safe to run on someone's home network, so changes that break them won't be merged:

* **Every AdGuard rule carries `$client`.** A rule must never affect a device other than the one it was made for. The tests assert this.
* **Decisions can be undone, and rule sync stays idempotent.**
* **Nothing identifying reaches an AI model.** IP and MAC addresses, hostnames and DNS logs stay out of prompts. Add or extend a test in [`test_assistant.py`](backend/tests/test_assistant.py) if you touch what is sent.
* **The companion only runs a fixed list of `adb` commands** and never scans outside the user's own private subnet.
* **Don't block must-allow domains** (captive-portal checks, time sync, a streaming app's own API) by default.
* **No secrets in the repo.** `.env` is git-ignored; put new settings in [`backend/.env.example`](backend/.env.example) with a comment.

## Dependencies

Use `poetry add <pkg>` or `poetry update <pkg>` and commit both `pyproject.toml` and `poetry.lock`. For the dashboard, commit `package-lock.json`.

## Commits and pull requests

* Write commit messages in plain English that say what changed for a user, e.g. *Add "Site or app not working?": find what QuietNet blocked and let it through*.
* Keep a pull request to one change. Say how you tested it, and include a screenshot for anything visible in the dashboard.
* Shell scripts, YAML and Docker files must keep LF line endings. [`.gitattributes`](.gitattributes) enforces it.

## Security

Please don't open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).
