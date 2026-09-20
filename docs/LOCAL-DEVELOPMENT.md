# Running OESPerformance locally

This is a static HTML/JS app — no build step, no bundler, no `npm install` required to run the site itself. Every page is a self-contained `.html` file that talks directly to Firebase from the browser.

Historically the only way to develop against this app was to point it at the **live production Firebase project** (`oesperformance-a54c0`), because there was no local alternative. This doc covers two things:

1. Serving the site locally.
2. Running it against a **local Firebase Emulator Suite** instead of production, so you can develop without a live Firebase login and without any risk of touching real data.

If you don't have Firebase credentials, skip straight to [Local development against the emulator](#local-development-against-the-emulator) — that path needs neither a Firebase account nor a login.

## Serving the site locally

There's nothing to install for this part. From the repo root, run any static file server, for example:
```
npx serve .
```
or
```
python -m http.server 5500
```
A few pages (`engineer.html`'s service worker/manifest, in particular) assume they're served from the domain root — `npx serve .` / `python -m http.server` from the repo root satisfy that. Opening a file directly via `file://` mostly works too for pages that don't depend on the service worker.

**By default, every page still talks to the real production Firebase project**, exactly as it does today — nothing about serving the files locally changes that.

## Local development against the emulator

### What's covered so far

The Firebase project used everywhere is `oesperformance-a54c0`. A second, separate project (`pingu-solar`) is used by `calendar.html`, `staff.html`, `officescreen.html` and a handful of other staff/rota pages — **that project is not covered by this emulator setup yet**. It's flagged here so the context isn't lost; extending this setup to `pingu-solar` is a reasonable next step but was kept out of scope for this first pass.

Of the ~103 HTML pages that use Firebase against `oesperformance-a54c0`, **9 pilot pages** share a single `shared-header.js` module (see [Shared header module](#shared-header-module) below), which is what actually wires them up to the local emulator now. Two more pages have their own standalone emulator wiring and are not on `shared-header.js`:

| Page | Emulator wiring | Why |
|---|---|---|
| `index.html`, `addname.html`, `absencetrends.html`, `display.html`, `complaints.html`, `additionalpayments.html`, `capacity.html`, `rubix.html`, `ashleigh.html` | Via `shared-header.js` | All use the modular SDK + a Firebase Auth `users`-collection allow-list — the pattern `shared-header.js` implements once for everyone |
| `login.html` | Own wiring | Firebase Auth, but redirects signed-in users *away* from itself instead of gating access — can't use the shared auth-guard, which would redirect signed-out visitors *to* `login.html` |
| `energise-order-form.html` | Own wiring | Identifies the field engineer via a `?user=` query param from `engineer.html`'s own PIN/signature flow, not Firebase Auth — engineers hitting this page are never signed into Firebase Auth, so the shared auth-guard would incorrectly redirect them to `login.html` |

`engineer.html`, `admin-upload.html` and `compliance-dash.html` were dropped from the pilot set entirely (their emulator wiring was reverted): all three still use the older Firebase compat SDK and have no Firebase Auth gating at all today, which made them inconsistent with the modular-SDK + auth-guard pattern the other pilot pages (and `shared-header.js`) use. Retrofitting real access control onto them is a separate, deliberate decision, not a side effect of adding a shared header.

The remaining ~90 pages are unchanged and still always talk to production. The pattern below is the same regardless of SDK style, so extending coverage to another page is mechanical — see [Extending to more pages](#extending-to-more-pages).

### Shared header module

`shared-header.js` (repo root) centralizes what used to be ~20 lines duplicated at the top of every pilot page's own `<script type="module">`: Firebase app init, the local-emulator opt-in, the Firebase Auth guard (redirect to `login.html` if signed out, sign out + redirect if the `users` doc is missing), and a small top nav bar showing the signed-in user's name with a sign-out button.

A page adopts it with one line, placed **before** its own module script(s):
```html
<script type="module" src="shared-header.js"></script>
```
Its own script(s) then get the already-initialized Firebase app via `getApps()[0]` (e.g. `const db = getFirestore(getApps()[0]);`) instead of calling `initializeApp()` again — module scripts run in document order, so the shared script has always finished by the time a later one runs.

If the page already has its own topbar with a `#user-name` element (like `index.html`), `shared-header.js` fills that in instead of injecting its own bar — it never adds a second one.

Only use it on pages where every visitor is expected to already be signed in via Firebase Auth (the `login.html`/`index.html` `users`-collection flow) — see the exclusions above for the two cases where that doesn't hold.

### Safety model — why this can't break production

Every one of the 10 pilot pages got the *same* small, additive change: right after the page's existing `getFirestore()` / `firebase.firestore()` (and `getAuth()` / `getStorage()` where relevant) call, there's now:

```js
const OES_USE_EMULATOR = (location.hostname === 'localhost' || location.hostname === '127.0.0.1')
    && localStorage.getItem('oes_use_emulator') === 'true';
if (OES_USE_EMULATOR) {
    connectFirestoreEmulator(db, '127.0.0.1', 8080);
    // + connectAuthEmulator / connectStorageEmulator, per page
}
```

Nothing else in any of these files was changed, reordered, or removed. Two conditions must **both** be true before any emulator code path activates:

1. You're on `localhost`/`127.0.0.1` — this alone is already false for any real deployment.
2. You've explicitly set a flag in the browser's `localStorage` — this alone is already false unless you deliberately set it.

On a real deployment, condition 1 is always false, so this is inert. Even in a local dev server, nothing changes until you opt in (see next section) — the app behaves exactly as it does today by default.

The emulator target is the literal IP `127.0.0.1`, not the hostname `localhost`. This was found the hard way while testing: on at least one machine, something else was already bound to port 8080 under the `localhost` name specifically, and the browser silently connected to that instead of the Firestore emulator, causing every request to look like a dead network. `127.0.0.1` sidesteps any such ambiguity and is what all 10 pilot pages actually use.

### Prerequisites

- Node.js (already required for the MCP servers — see [MCP-SERVERS.md](MCP-SERVERS.md)).
- **Java 21 or later.** The Firestore and Storage emulators are JVM-based and Firebase CLI refuses to start them on anything older ("firebase-tools no longer supports Java version before 21"). Check with `java -version`; install from [Adoptium](https://adoptium.net/) or via `winget install EclipseAdoptium.Temurin.21.JDK` if needed. The Auth emulator alone is pure Node and doesn't need Java.
- No Firebase login and no real credentials are required for any of this — see below.

### 1. Start the emulators

From the repo root:
```
npx firebase-tools emulators:start --import=./emulator-data
```
(Omit `--import=./emulator-data` on the very first run, before that folder exists — see step 2.)

This uses the real `oesperformance-a54c0` project ID configured in `.firebaserc` — deliberately the same one the app's pages use in their hardcoded `firebaseConfig`, so seeded/exported data lands in and reloads from the same place the pilot pages actually look. You'll see a one-line advisory that you're "not currently authenticated" — that's expected and harmless: it only affects features that talk to the real cloud project (which nothing here does), and no `firebase login` or real credentials are needed to run the emulators themselves.

You should see all three emulators (Firestore :8080, Auth :9099, Storage :9199) plus the Emulator UI at `http://127.0.0.1:4000`.

> This whole flow — starting the emulators, seeding data, exporting it, restarting with `--import`, and signing in through `login.html` in an actual browser with the emulator connected — was run and confirmed working end-to-end while building this setup (using a temporary portable JDK 21, since Firestore/Storage need it and it wasn't otherwise installed).

### 2. Seed sample data

The first time, seed some starter data so the pilot pages have something to show:
```
cd scripts/seed-emulator
npm install
npm run seed
```
This creates:
- A test login for `login.html`/`index.html`: **`dev@example.com` / `DevPassword123!`**, with an admin-role `users` doc.
- A few sample `engineers` docs (password `demo123`) for `engineer.html`'s custom login and `addname.html`.
- Sample docs in `Energise Inventory`, `energise order images`, `Complaints`, `Absence Trends`, `jobs`, `Compliance Dashboard`, and `portal_documents` — enough for each pilot page to render something.

See the comments in `scripts/seed-emulator/seed-emulator.js` for exactly what's seeded and why — the field shapes are a best-effort inference from reading the pages that consume them, not a verified copy of the production schema, so extend/correct them as you go.

Then export it so it's committed and restored automatically from then on:
```
firebase emulators:export ./emulator-data
```
(Run from the repo root, with the emulators still running. This is a one-time step per meaningful change to the seed data — day to day, `--import=./emulator-data` on step 1 restores everything automatically.)

### 3. Opt a browser session into emulator mode

Serve the site locally (see [Serving the site locally](#serving-the-site-locally)), open it at `http://localhost:<port>`, open the browser devtools console, and run once:
```js
localStorage.setItem('oes_use_emulator', 'true')
```
Reload. Any of the 10 pilot pages will now read/write the local emulator instead of production. This persists across page navigation (it's `localStorage`, not a URL param) so you can move between pilot pages without re-setting it. Sign in with `dev@example.com` / `DevPassword123!` on `login.html`, or with engineer name + `demo123` on `engineer.html`.

To go back to production, run `localStorage.removeItem('oes_use_emulator')` (or just use a different browser/profile — the flag never applies outside `localhost` anyway).

### Extending to more pages

For a page that already expects every visitor to be signed in via the Firebase Auth `users`-collection flow (the common case), add `<script type="module" src="shared-header.js"></script>` before its own module script(s), then update those script(s) to get the app via `getApps()[0]` instead of calling `initializeApp()` — see [Shared header module](#shared-header-module) above. Delete the page's own `firebaseConfig`/`initializeApp`/`OES_USE_EMULATOR` block; `shared-header.js` already covers Auth + Firestore. If the page also uses Storage, keep its own `connectStorageEmulator` call locally (`shared-header.js` doesn't init Storage) — see `complaints.html` for the pattern.

For a page that can't use the shared auth-guard (its own login page, or a page identified some other way than Firebase Auth — see the `login.html`/`energise-order-form.html` exclusions above), copy the older standalone pattern instead: add `connectFirestoreEmulator`/`connectAuthEmulator`/`connectStorageEmulator` to that page's existing Firebase imports, then add the `OES_USE_EMULATOR` check + `connect*Emulator(...)` calls immediately after that page's existing `getFirestore()`/`getAuth()`/`getStorage()` (or compat equivalents), before any other Firestore/Auth/Storage call. Order matters — `connect*Emulator` must run before the first real operation on that service instance or it throws.

## Known follow-ups (not done in this pass)

- `pingu-solar` project (calendar/staff pages) isn't covered by the emulator yet.
- Only 11 of ~103 Firebase-using pages are wired up (9 via `shared-header.js`, plus `login.html` and `energise-order-form.html` with their own wiring); the rest still always hit production.
- `engineer.html`, `admin-upload.html` and `compliance-dash.html` have no Firebase Auth gating at all today and use the older compat SDK — bringing them onto the modular SDK + auth-guard pattern (and `shared-header.js`) is a real product decision (it would start requiring sign-in), not just a mechanical port.
- The seed data's field shapes are inferred, not verified against production — treat them as a reasonable starting point.
