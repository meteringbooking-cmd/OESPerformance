# OESPerformance

A collection of internal performance/ops dashboards (attendance, compliance, bonuses, Energise ordering, and more) for OES/NGU field engineering teams. It's a static, build-free HTML/JS app — every page is a self-contained `.html` file that talks directly to Firebase (Firestore, Auth, Storage) from the browser. One Python file (`api/run-sync.py`) runs as a serverless function to sync live pricing from a third-party inventory system into Firestore.

## Quick start

```
npx serve .
```
Then open `http://localhost:<port>`. By default this talks to the real production Firebase project, exactly as the deployed app does.

To develop against a **local Firebase emulator** instead (no production credentials needed, zero risk to live data), see **[docs/LOCAL-DEVELOPMENT.md](docs/LOCAL-DEVELOPMENT.md)**.

## Docs

- [docs/LOCAL-DEVELOPMENT.md](docs/LOCAL-DEVELOPMENT.md) — running the app locally, and the local Firebase Emulator Suite setup with seed data.
- [docs/MCP-SERVERS.md](docs/MCP-SERVERS.md) — the GitHub and Firebase MCP servers used for AI-assisted development on this repo: what they are, why, and how to set them up.

## Structure

- `*.html` — the dashboards themselves. No build step; each page inlines its own `<script>` and Firebase config.
- `api/run-sync.py` — Vercel-style Python serverless function; uses `firebase-admin` (via `FIREBASE_CONFIG_JSON`) to sync `Energise Inventory` pricing from a third-party site. Production-only; not part of local development.
- `manifest.json`, `sw.js` — PWA manifest and service worker for `engineer.html`.

## Known follow-ups

- `api/run-sync.py` currently has a third-party login credential committed in plaintext. It should be rotated and moved to an environment variable.
- The local emulator setup currently covers the main Firebase project (`oesperformance-a54c0`) and 11 pilot pages (9 of them sharing `shared-header.js`); see [docs/LOCAL-DEVELOPMENT.md](docs/LOCAL-DEVELOPMENT.md#known-follow-ups-not-done-in-this-pass) for what's not covered yet.
