# Deploying Lead Finder

Web app → **Vercel** (folder `web/`). Worker → **Railway** (folder `worker/`, Docker). Database → **MongoDB Atlas**.
Nothing here is automatic: run each step yourself, in order, and only continue when its check passes.

Order: **0 backup → 1 Atlas → 2 indexes + gazetteer → 3 Railway worker → 4 Vercel web → 5 verify**.
Staging first (`leadscraper_staging`), production only after staging passed.

---

## 0. Before anything: backup (production)

```bash
cd web
npm run db:backup                     # writes ../backups/<timestamp>/ and VERIFIES it; must say "verified"
```
Keep the `backups/` folder out of cloud-synced folders (it contains business contact data).

## 1. MongoDB Atlas

| Item | Staging | Production |
|---|---|---|
| Database name | `leadscraper_staging` | `leadscraper` |
| Database user for **web** | `lf-web-staging`, role *readWrite* on `leadscraper_staging` only | `lf-web`, *readWrite* on `leadscraper` only |
| Database user for **worker** | `lf-worker-staging`, *readWrite* on `leadscraper_staging` only | `lf-worker`, *readWrite* on `leadscraper` only |
| Network Access | Vercel needs `0.0.0.0/0` (no fixed IPs on Hobby); Railway: same, or its static egress IP on Pro | same |
| Backups | optional | **on** (Atlas → Backup) |

Atlas → Database Access → *Add New Database User* → "Specific Privileges" → readWrite → database name.
Use separate users so a leaked web password can't be used on the other database. Strong generated passwords.

## 2. Indexes and gazetteer (needs the Atlas users from step 1)

Preview (read-only), then build. Staging first:
```bash
cd web
MONGODB_DB=leadscraper_staging npm run db:indexes
cd ../worker && MONGODB_DB=leadscraper_staging python -m app.geo.gazetteer load data/geo/raw
```
Production (after staging passed). Expected changes, checked read-only on 2026-11-17:
`scrapingJobs` +1 index (`createdAt`), `leads` +7 indexes and the old `ownerId_1_nameNormalized_1` replaced by
`ownerId_1_nameNormalized_1__id_1`, and 6 new collections (`jobStartLocks`, `gazetteer`, `gazetteerMeta`,
`osmExtracts`, `overtureTiles`, `overtureReleases`). Nothing else is removed.
```bash
cd web
MONGODB_DB=leadscraper npm run db:indexes
cd ../worker && MONGODB_DB=leadscraper python -m app.geo.gazetteer load data/geo/raw   # ~172k places
```
(`web/.env.local` and `worker/.env` hold the connection string; the `MONGODB_DB=` prefix wins over the file.)
GeoNames files: `cities1000.zip`, `countryInfo.txt`, `admin1CodesASCII.txt` from
https://download.geonames.org/export/dump/ into `worker/data/geo/raw/` (already there locally).

## 3. Railway (worker)

1. New project → *Deploy from GitHub repo* → **Root Directory `worker`**. Railway reads `worker/railway.json`
   (Dockerfile build, 1 replica, restart **always**: the worker exits if Atlas has no primary for a while;
   Railway restarts it and the job queue resumes interrupted jobs from their checkpoints).
2. **Volume**: Service → Settings → Volumes → mount path **`/data`** (≥ 5 GB). Without it every restart
   re-downloads the map data.
3. Variables (Service → Variables). **Required:**

| Variable | Staging | Production |
|---|---|---|
| `MONGODB_URI` | worker-staging user's connection string | worker user's connection string |
| `MONGODB_DB` | `leadscraper_staging` | `leadscraper` |
| `CRAWLER_CONTACT_URL` | `mailto:` your email or a page you own (never example.com) | same |
| `WORKER_ID` | `railway-staging` | `railway-prod` |

   Already set by the Dockerfile (don't override): `OSM_CACHE_DIR=/data/osm`, `OVERTURE_CACHE_DIR=/data/overture`,
   `OSM_TMP_DIR`, `GEO_DATA_DIR`, `DUCKDB_EXTENSION_DIRECTORY`.
   Optional (defaults are fine): `POLL_INTERVAL_SECONDS` 3, `STALE_AFTER_SECONDS` 600, `MAX_ATTEMPTS` 3,
   `CRAWL_*` (polite limits; can only be made stricter), `OSM_CACHE_MAX_BYTES` 4 GB.
   **`GOOGLE_PLACES_API_KEY`: do NOT set.** Unset = Google Places disabled (log line
   "Google Places disabled (no GOOGLE_PLACES_API_KEY)"). Obsolete, can be deleted: `MAX_DISCOVERY_QUERIES`,
   `OVERPASS_TIMEOUT_SECONDS`.
4. Deploy. Check the log for: `connected to database '<name>'` and `Google Places disabled`.

CLI alternative: `railway login && railway link && railway up` (from `worker/`).

## 4. Vercel (web)

1. Import the repo → **Root Directory `web`**, framework Next.js, Node.js **20.x or 22.x**.
2. Environment variables. Set them per environment (Production / Preview): **Preview = staging DB**.

| Variable | Preview (staging) | Production |
|---|---|---|
| `MONGODB_URI` | web-staging user's string | web user's string |
| `MONGODB_DB` | `leadscraper_staging` | `leadscraper` |
| `AUTH_SECRET` | its own random value | a NEW random value, never reused |
| `ADMIN_EMAILS` | optional | your email (sees Google cost figures) |
| Limits (optional) | `JOB_USER_DAILY_MAX` 30, `JOB_USER_DAILY_COUNTRY_MAX` 3, `JOB_GLOBAL_OPEN_MAX` 20, `JOB_GLOBAL_DAILY_MAX` 300, `SIGNUP_GLOBAL_HOURLY_MAX` 50, `EXPORT_HOURLY_MAX` 10 | same |

   `AUTH_SECRET`: `node -e "console.log(require('crypto').randomBytes(48).toString('base64url'))"`.
   Do NOT set `ADMIN_EMAIL`/`ADMIN_PASSWORD` on Vercel (only for the local `npm run seed:user`).
   Changing `AUTH_SECRET` signs everyone out (expected).
3. The web app must run on Vercel: rate limits read the client IP from `x-forwarded-for`, which Vercel sets.

CLI alternative (from `web/`): `vercel link`, `vercel env add <NAME> preview|production`,
`vercel deploy` (preview = staging), `vercel deploy --prod` (production).

## 5. Verify (staging, then production)

1. `https://<domain>/login` loads. Response headers contain `Content-Security-Policy` (with `nonce-`),
   `Strict-Transport-Security`, `X-Frame-Options: DENY`. Browser DevTools console: **no CSP errors**.
2. Sign up → sign in → Find leads: a small city, OSM only (Google not offered as working) → Start search.
3. Jobs page: queued → running (progress moves) → completed within minutes. Railway log shows the job.
4. All leads: rows with source links; open one, save notes, ★ save. Export CSV and Excel both download.
5. Settings: add + remove an opt-out entry; "Sign out on all devices" returns to the login page.
6. Start another search and press Cancel → "cancelled"; Retry → a new job starts (cancel it).
7. `curl -X POST -H "Origin: https://evil.example" https://<domain>/api/jobs` → **403**.
8. Atlas: `leads` count grows only for the test account; nothing in the other database changed.

## Rollback

| What | How | Data impact |
|---|---|---|
| Web | Vercel → Deployments → previous one → **Instant Rollback** (or `vercel rollback <deployment-url>`) | none |
| Worker | Railway → Deployments → previous one → **Redeploy** | none; running jobs resume from checkpoints |
| Indexes | Additive. To restore the one replaced index: `db.leads.createIndex({ownerId:1, nameNormalized:1})` in Atlas → Collections → leads → Indexes | none |
| Gazetteer | Separate collections; the old app version doesn't read them | none |
| Data | Restore the verified backup into a **separate** database first (`npm run db:restore -- --backup=../backups/<stamp> --target-uri="<uri>" --target-db=leadscraper_restore`; it refuses the production DB and non-empty collections), check it, then copy what is needed | as chosen |
| Sessions | Rotate `AUTH_SECRET` on Vercel and redeploy: everyone is signed out | none |
