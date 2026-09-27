// Captures the handful of screenshots README.md embeds as its "first
// impression" of the running portal. Requires the stack already up and
// seeded (`docker compose up -d --wait`, the default) -- this only drives a
// browser against it, it never touches the database directly.
//
//   node scripts/screenshots/capture.js [--base http://localhost:3000]
//
// One Chromium context per shot rather than one shared browser session: each
// screenshot needs a different signed-in identity (or none), and a fresh
// context is the simplest way to guarantee one shot's cookies never leak
// into the next.

const { chromium } = require('playwright');
const path = require('node:path');
const fs = require('node:fs');

const BASE = process.argv.includes('--base')
  ? process.argv[process.argv.indexOf('--base') + 1]
  : 'http://localhost:3000';

const OUT_DIR = path.join(__dirname, '..', '..', 'docs', 'screenshots');
const VIEWPORT = { width: 1280, height: 800 };
const PASSWORD = 'dogfood2026';

async function login(page, email) {
  await page.goto(`${BASE}/login`);
  await page.fill('#email', email);
  await page.fill('#password', PASSWORD);
  await Promise.all([page.waitForLoadState('networkidle'), page.click('button[type="submit"]')]);
}

async function shot(browser, { name, path: urlPath, as: email, before }) {
  const context = await browser.newContext({ viewport: VIEWPORT });
  const page = await context.newPage();
  if (email) await login(page, email);
  if (before) await before(page);
  await page.goto(`${BASE}${urlPath}`, { waitUntil: 'networkidle' });
  const dest = path.join(OUT_DIR, `${name}.png`);
  await page.screenshot({ path: dest, fullPage: false });
  console.log(`wrote ${dest}`);
  await context.close();
}

const API_BASE = process.argv.includes('--api-base')
  ? process.argv[process.argv.indexOf('--api-base') + 1]
  : 'http://localhost:8000';

// Talks to the API directly (not through the browser): the frontend has no
// client-side JS and no `/api` proxy of its own (`lib/api.ts`'s server
// components call the API server-side, over a different base URL than a
// browser would use) -- a plain server-side fetch with a bearer token is the
// simplest way to find a real ballot id to screenshot.
async function organizerToken() {
  const session = await fetch(`${API_BASE}/api/auth/login`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ email: 'organizer@example.com', password: PASSWORD }),
  }).then((r) => r.json());
  return session.token;
}

// The certificate wizard's step 3 (`?submission=<id>&code=<code>`) is the
// visually interesting page -- the live signed-record preview with its QR
// code -- so the screenshot goes straight there instead of stopping at
// step 1's search box.
async function findCertificateStepThreeParams(token, projectName) {
  const submissions = await fetch(
    `${API_BASE}/api/events/raptors-winter/submissions?per_page=200`,
    { headers: { authorization: `Bearer ${token}` } },
  ).then((r) => r.json());
  const submission = submissions.items.find((s) => s.name === projectName);
  if (!submission) throw new Error(`no submission named ${projectName}`);
  const certs = await fetch(`${API_BASE}/api/submissions/${submission.id}/certificates`).then(
    (r) => r.json(),
  );
  if (!certs[0]?.code) throw new Error(`no certificate issued for ${projectName}`);
  return { submissionId: submission.id, code: certs[0].code };
}

async function findFirstAssignmentId(email) {
  const session = await fetch(`${API_BASE}/api/auth/login`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ email, password: PASSWORD }),
  }).then((r) => r.json());
  const rows = await fetch(
    `${API_BASE}/api/judging/queue?event=raptors-winter`,
    { headers: { authorization: `Bearer ${session.token}` } },
  ).then((r) => r.json());
  if (!rows[0]?.id) {
    throw new Error(`no ballot found for ${email} on raptors-winter`);
  }
  return rows[0].id;
}

async function main() {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const browser = await chromium.launch();

  const ballotId = await findFirstAssignmentId('judge.rivera@example.com');
  const { submissionId, code } = await findCertificateStepThreeParams(
    await organizerToken(),
    'Crosshatch',
  );

  await shot(browser, { name: 'gallery', path: '/events/raptors-winter/gallery' });
  await shot(browser, { name: 'public-results', path: '/events/raptors-summer/results' });
  await shot(browser, {
    name: 'judge-ballot',
    path: `/judging/${ballotId}`,
    as: 'judge.rivera@example.com',
  });
  await shot(browser, {
    name: 'organizer-dashboard',
    path: '/organizer/raptors-winter',
    as: 'organizer@example.com',
  });
  await shot(browser, {
    name: 'certificate-wizard',
    path: `/certificate?submission=${submissionId}&code=${code}`,
  });

  await browser.close();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
