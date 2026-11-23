## What changed

<!-- What does this change for someone using QuietNet? -->

## How I tested it

- [ ] `poetry run pytest -q` in `backend/` and `companion/`
- [ ] `npm run lint` and `npm run build` in `frontend/`
- [ ] Tried it in the dashboard (screenshot below if it's visible)

## Safety checklist

- [ ] Every AdGuard rule I added carries `$client`
- [ ] No IP, MAC, hostname or DNS log reaches an AI model
- [ ] No secrets committed; new settings are in `backend/.env.example`
