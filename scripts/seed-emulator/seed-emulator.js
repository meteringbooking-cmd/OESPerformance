// Seeds the local Firebase Emulator Suite with sample data for OESPerformance
// local development. Run this ONCE against a running emulator, then export
// the result with `firebase emulators:export ../../emulator-data` from the
// repo root so the data gets committed and restored automatically on every
// future `firebase emulators:start --import=./emulator-data`.
//
// Usage (from this folder, with the emulators already running in another
// terminal — see docs/LOCAL-DEVELOPMENT.md):
//   npm install
//   npm run seed
//
// NOTE ON DATA SHAPES: field names below were inferred by reading the pages
// that consume each collection (see docs/LOCAL-DEVELOPMENT.md for the file
// references). They're a best-effort starting point for the pilot pages
// covered by this local-dev pass, not a verified copy of production schemas.
// Extend this script as more pages get wired up to the emulator.

process.env.FIRESTORE_EMULATOR_HOST = process.env.FIRESTORE_EMULATOR_HOST || 'localhost:8080';
process.env.FIREBASE_AUTH_EMULATOR_HOST = process.env.FIREBASE_AUTH_EMULATOR_HOST || 'localhost:9099';
process.env.FIREBASE_STORAGE_EMULATOR_HOST = process.env.FIREBASE_STORAGE_EMULATOR_HOST || 'localhost:9199';

const admin = require('firebase-admin');

// Must match the projectId hardcoded in the app's firebaseConfig (see any
// pilot HTML file) so seeded data lands where the app's emulator-connected
// pages actually look for it.
const PROJECT_ID = 'oesperformance-a54c0';

admin.initializeApp({ projectId: PROJECT_ID });

const db = admin.firestore();
const auth = admin.auth();

const DEV_USER_EMAIL = 'dev@example.com';
const DEV_USER_PASSWORD = 'DevPassword123!';

async function seedAuthUser() {
  let user;
  try {
    user = await auth.getUserByEmail(DEV_USER_EMAIL);
  } catch (e) {
    user = await auth.createUser({
      email: DEV_USER_EMAIL,
      password: DEV_USER_PASSWORD,
      displayName: 'Dev User',
    });
  }

  await db.collection('users').doc(user.uid).set({
    role: 'admin',
    name: 'Dev User',
    lastLogin: new Date().toISOString(),
  });

  console.log(`Auth user ready: ${DEV_USER_EMAIL} / ${DEV_USER_PASSWORD} (uid ${user.uid})`);
}

async function seedCollection(name, docs) {
  const batch = db.batch();
  docs.forEach((data, i) => {
    const ref = db.collection(name).doc(`seed-${i + 1}`);
    batch.set(ref, data);
  });
  await batch.commit();
  console.log(`Seeded ${docs.length} doc(s) into "${name}"`);
}

async function main() {
  await seedAuthUser();

  // engineers — used by addname.html (list/add) and engineer.html's custom
  // password-gate login (engineer.html reads the plaintext `password` field).
  await seedCollection('engineers', [
    { engineerName: 'Alex Turner', teamLeader: 'Sam Wright', password: 'demo123', contract: 'OES' },
    { engineerName: 'Jordan Blake', teamLeader: 'Sam Wright', password: 'demo123', contract: 'OES' },
    { engineerName: 'Casey Morgan', teamLeader: 'Priya Nair', password: 'demo123', contract: 'Calisen' },
  ]);

  // Energise Inventory — read/written by api/run-sync.py and the Energise
  // ordering pages.
  await seedCollection('Energise Inventory', [
    { sku: 'ENG-001', price: 12.5 },
    { sku: 'ENG-002', price: 34.99 },
    { sku: 'ENG-003', price: 8.75 },
  ]);

  // energise order images — read by energise-order-form.html to look up
  // product photos by sku.
  await seedCollection('energise order images', [
    { sku: 'ENG-001', downloadURL: 'https://placehold.co/200x200?text=ENG-001' },
    { sku: 'ENG-002', downloadURL: 'https://placehold.co/200x200?text=ENG-002' },
  ]);

  // Complaints — read/written by complaints.html.
  await seedCollection('Complaints', [
    { customerName: 'J. Smith', description: 'Sample seeded complaint for local dev.', status: 'open', createdAt: new Date().toISOString() },
    { customerName: 'A. Patel', description: 'Second sample complaint.', status: 'resolved', createdAt: new Date().toISOString() },
  ]);

  // Absence Trends — read/written by absencetrends.html.
  await seedCollection('Absence Trends', [
    { engineerName: 'Alex Turner', category: 'sickness', hours: 7.5, date: '2026-08-04' },
    { engineerName: 'Jordan Blake', category: 'holiday', hours: 7.5, date: '2026-08-11' },
  ]);

  // jobs — read by display.html.
  await seedCollection('jobs', [
    { engineerName: 'Alex Turner', date: '2026-09-01', jobType: 'Install', value: 120 },
    { engineerName: 'Jordan Blake', date: '2026-09-02', jobType: 'Repair', value: 80 },
  ]);

  // Compliance Dashboard — read by compliance-dash.html for the weekly pass
  // rate chart.
  await seedCollection('Compliance Dashboard', [
    { week: '2026-W35', passRate: 92.5 },
    { week: '2026-W36', passRate: 94.1 },
  ]);

  // portal_documents — read by admin-upload.html's existing-documents list.
  await seedCollection('portal_documents', [
    { category: 'Policies', fileName: 'sample-policy.pdf', downloadURL: 'https://placehold.co/1x1', uploadedAt: new Date().toISOString() },
  ]);

  console.log('\nSeed complete.');
  process.exit(0);
}

main().catch((err) => {
  console.error('Seeding failed:', err);
  process.exit(1);
});
