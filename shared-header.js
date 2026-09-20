// shared-header.js
//
// Central Firebase bootstrap for OES Performance dashboard pages: initializes
// the app, opts in to the local emulator (see docs/LOCAL-DEVELOPMENT.md),
// gates the page behind Firebase Auth (redirects to login.html if signed
// out), and renders the common top nav bar with the signed-in user's name
// and a sign-out button.
//
// Usage — add this as the first script in <body>, before any page-specific
// module script:
//   <script type="module" src="shared-header.js"></script>
//
// A page's own module script should get the app/db via `getApps()[0]` /
// `getFirestore(getApps()[0])` rather than calling initializeApp() again.
//
// Not for login.html, which has its own sign-in flow and must not redirect
// signed-out visitors away from itself.

import { initializeApp, getApps } from "https://www.gstatic.com/firebasejs/10.7.1/firebase-app.js";
import { getAuth, onAuthStateChanged, signOut, connectAuthEmulator } from "https://www.gstatic.com/firebasejs/10.7.1/firebase-auth.js";
import { getFirestore, doc, getDoc, connectFirestoreEmulator } from "https://www.gstatic.com/firebasejs/10.7.1/firebase-firestore.js";

const FIREBASE_CONFIG = {
    apiKey: "AIzaSyCMmUOdW2uEIGVd6XQfWXKF8eaFmwEog1g",
    authDomain: "oesperformance-a54c0.firebaseapp.com",
    projectId: "oesperformance-a54c0",
    storageBucket: "oesperformance-a54c0.firebasestorage.app",
    messagingSenderId: "484894102454",
    appId: "1:484894102454:web:17535ee497eac2d90d6717"
};

const app  = getApps().find(a => a.name === '[DEFAULT]') || initializeApp(FIREBASE_CONFIG);
const auth = getAuth(app);
const db   = getFirestore(app);

// Local dev only — see docs/LOCAL-DEVELOPMENT.md. Never fires on a real
// deployment: requires localhost AND an explicit opt-in flag.
const OES_USE_EMULATOR = (location.hostname === 'localhost' || location.hostname === '127.0.0.1')
    && localStorage.getItem('oes_use_emulator') === 'true';
if (OES_USE_EMULATOR) {
    connectAuthEmulator(auth, 'http://127.0.0.1:9099');
    connectFirestoreEmulator(db, '127.0.0.1', 8080);
}

document.documentElement.style.visibility = 'hidden';

function renderHeader(name) {
    // Pages with their own topbar markup (e.g. index.html) just get the
    // name filled in — no second bar gets injected.
    const existing = document.getElementById('user-name');
    if (existing) {
        existing.textContent = name;
        return;
    }

    const style = document.createElement('style');
    style.textContent = `
        .oes-shared-header {
            position: fixed; top: 0; left: 0; right: 0; z-index: 999999;
            display: flex; align-items: center; justify-content: space-between;
            padding: 10px 20px; background: #0E1520; border-bottom: 1px solid #1C2A3A;
            font-family: 'Barlow', Arial, sans-serif; box-sizing: border-box;
        }
        .oes-shared-header a.oes-brand {
            color: #F5A623; font-weight: 700; text-decoration: none;
            font-size: 0.95rem; letter-spacing: 0.03em;
        }
        .oes-shared-header a.oes-brand:hover { text-decoration: underline; }
        .oes-shared-header .oes-user {
            display: flex; align-items: center; gap: 12px;
            color: #7A8FA6; font-size: 0.85rem;
        }
        .oes-shared-header button.oes-signout {
            background: none; border: 1px solid #2A3F55; color: #E8EDF4;
            padding: 4px 10px; border-radius: 4px; font-size: 0.78rem; cursor: pointer;
        }
        .oes-shared-header button.oes-signout:hover { border-color: #F5A623; color: #F5A623; }
    `;
    document.head.appendChild(style);

    const bar = document.createElement('div');
    bar.className = 'oes-shared-header';
    bar.innerHTML = `
        <a class="oes-brand" href="index.html">← OES Performance</a>
        <div class="oes-user">
            <span class="oes-user-name"></span>
            <button class="oes-signout" type="button">Sign out</button>
        </div>
    `;
    bar.querySelector('.oes-user-name').textContent = name;
    bar.querySelector('.oes-signout').addEventListener('click', () => {
        signOut(auth).then(() => { window.location.href = 'login.html'; });
    });
    document.body.insertBefore(bar, document.body.firstChild);

    // The bar is `position: fixed` so it works regardless of the host
    // page's own body layout (flex-centered cards, grids, etc.) — push
    // body content down by its height so nothing sits underneath it.
    const existingPaddingTop = parseFloat(getComputedStyle(document.body).paddingTop) || 0;
    document.body.style.paddingTop = (existingPaddingTop + bar.offsetHeight) + 'px';
}

onAuthStateChanged(auth, async (user) => {
    if (!user) { window.location.href = 'login.html'; return; }
    const snap = await getDoc(doc(db, 'users', user.uid));
    if (!snap.exists()) { await signOut(auth); window.location.href = 'login.html'; return; }
    window.currentUser     = user;
    window.currentUserRole = snap.data().role || 'viewer';
    window.currentUserName = snap.data().name || user.email;
    renderHeader(window.currentUserName);
    document.documentElement.style.visibility = 'visible';
});
