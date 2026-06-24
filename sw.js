// NGU Engineer Portal — Service Worker
// Caches the app shell so it loads instantly and works offline

const CACHE_NAME = 'ngu-portal-v1';

// These are the core files that make up the app shell.
// The portal loads its data from Firebase in real time,
// so we only cache the static shell here.
const SHELL_FILES = [
  '/',
  '/engineer.html',
  'https://fonts.googleapis.com/css2?...'
];

// ── INSTALL: cache the app shell ──
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      return cache.addAll(SHELL_FILES);
    })
  );
  // Activate immediately without waiting for old tabs to close
  self.skipWaiting();
});

// ── ACTIVATE: clean up old caches ──
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys
          .filter(key => key !== CACHE_NAME)
          .map(key => caches.delete(key))
      )
    )
  );
  // Take control of all open tabs immediately
  self.clients.claim();
});

// ── FETCH: network first, fall back to cache ──
// Firebase calls always go to the network.
// Everything else tries network first, then falls back to cache.
self.addEventListener('fetch', event => {
  const url = event.request.url;

  // Always let Firebase and Google API calls go straight to the network —
  // never try to cache these as they carry live data.
  if (
    url.includes('firestore.googleapis.com') ||
    url.includes('firebase') ||
    url.includes('googleapis.com') ||
    url.includes('gstatic.com/firebasejs') ||
    url.includes('emailjs.com') ||
    url.includes('cognitoforms.com') ||
    url.includes('dropbox.com') ||
    url.includes('wa.me')
  ) {
    event.respondWith(fetch(event.request));
    return;
  }

  // For everything else: try network first, fall back to cache
  event.respondWith(
    fetch(event.request)
      .then(response => {
        // If we got a valid response, store a copy in cache
        if (response && response.status === 200 && response.type === 'basic') {
          const responseClone = response.clone();
          caches.open(CACHE_NAME).then(cache => {
            cache.put(event.request, responseClone);
          });
        }
        return response;
      })
      .catch(() => {
        // Network failed — try the cache
        return caches.match(event.request).then(cached => {
          if (cached) return cached;
          // If it's a navigation request and nothing is cached,
          // return the cached homepage as a fallback
          if (event.request.mode === 'navigate') {
            return caches.match('/');
          }
        });
      })
  );
});
