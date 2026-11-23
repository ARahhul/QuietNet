# QuietNet dashboard

The React dashboard and public homepage ([quietnet.in](https://quietnet.in)) for QuietNet. React 19, TypeScript, Vite, Tailwind CSS 4, TanStack Query and Firebase Authentication (Google sign-in).

```powershell
npm install
npm run dev            # http://localhost:5173, proxies /api to the backend on :8000
npm run dev:emulator   # http://localhost:5174, against the Firebase Auth emulator (.env.emulator)
npm run lint           # oxlint
npm run build          # type-check, then production build into dist/
```

Run the backend first (see the [root README](../README.md#quick-start)), or use `.\scripts\dev.ps1` from the repo root to start everything.

| Folder | What |
|---|---|
| `src/pages/` | Landing, Login, Setup, Suggestions, Devices, Device detail, Live, Blocked, Report, Alerts, Settings |
| `src/components/` | Shared UI, the setup wizard, the TV assistant (`assistant/`), "Saw an ad?" and "Site or app not working?" |
| `src/lib/` | Firebase setup, image helpers, chart helpers, credits |
| `src/api.ts` | Typed client for the backend's `/api` |

Production settings are in `.env.production` (Firebase web config, which is public by design). The hosted build deploys to Firebase Hosting, configured in the root `firebase.json`.
