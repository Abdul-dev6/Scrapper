# Lead Finder: project rules

## What this is

A location-based business lead scraper. The owner sells combat-sports equipment
(punching bags, grappling dummies, boxing/MMA gear) on Amazon and wants B2B leads:
gyms, boxing clubs, MMA academies, martial arts schools, wrestling clubs,
fitness centers, sports equipment stores.

Flow: user enters a location + categories → worker discovers businesses
(OpenStreetMap) → visits each official website → extracts PUBLISHED contact details
→ verifies → saves to MongoDB with source URLs → dashboard + CSV/Excel export.

## Stack and layout

- web/: Next.js 15 (App Router) + Tailwind v4 + Mongoose. UI and API route handlers. Deploys to Vercel.
- worker/: Python 3.12 worker. Deploys to Railway (Docker).
- MongoDB Atlas is the ONLY link between web and worker. No Redis/BullMQ in v1:
  the `scrapingJobs` collection is the queue (atomic find_one_and_update claim,
  lockedAt heartbeat, stale-job recovery, cancelRequested flag).

## Locked decisions (do not change without asking)

- Multi-user with open sign-up (/signup, max 5 accounts per IP per hour; decided 2026-11-09).
  Confirmed 2026-11-16 (security review M3 = C): sign-up stays open, with abuse/cost caps: per user
  3 open / 10 per hour / 30 per 24 h / 3 whole-country per 24 h; all users 20 open / 300 per 24 h;
  sign-ups 5 per IP + 50 in total per hour; exports 10 per user per hour (env-overridable, web/.env.example).
  Every document has `ownerId` and every query filters by it, so users never see each other's data.
- Discovery: offline gazetteer + Natural Earth areas (Phase 2a) and Geofabrik extracts (Phase 2b).
  No public Nominatim and no public Overpass in production (decided 2026-11-14/15).
- Google Places (decided 2026-11-13, revised 2026-11-14): optional, OFF by default, worker-only key
  (GOOGLE_PLACES_API_KEY). Text Search Pro field mask only (never Enterprise fields). Google results
  are NEVER matched or linked to any lead; only place IDs are stored (`googlePlaceRefs`), and nothing
  from Google (not even place IDs) is ever exported. Place Details is not used. Global monthly limits
  4,500 / 9,000 reserved atomically in `apiUsage`; either reached = OSM-only until the next PT month.
- Licensing mode M2 (decided 2026-11-14): OSM and non-OSM (Overture etc.) data in separate lanes.
  No cross-lane dedupe, linking or overlap calculation (not even in memory). Separate attribution in exports.
- Geocoding (decided 2026-11-14, later phase): GeoNames + Natural Earth + license-checked geoBoundaries
  gbOpen. No public Nominatim in production. No ODbL boundaries in the non-OSM lane. Ambiguous cities
  are shown to the user to pick.
- Contact statuses: "verified" ONLY when a person marked a contact (PATCH verifyContact). Automatic
  checks (syntax, MX, phone format) give "checked" at most. Per-contact `checks` subdocuments.
- Contacts come from OSM tags and the business's OWN website. Every stored value
  carries sourceUrl + sourceType ('osm' | 'website' | 'search').
- Category ids are shared: web/src/lib/categories.ts ↔ worker/app/categories.py.
- Job error field is `errorLog` (not `errors`, which is reserved in Mongoose).
- Indexes are built by `npm run db:indexes` (autoIndex is off).
- Crawling: httpx (async) + BeautifulSoup/selectolax. Playwright only as a
  fallback for JS-only pages. Scrapy is not used inside jobs.

## Hard rules (compliance)

- Never invent, guess or hardcode data. Missing field = empty.
- Respect robots.txt (cache 24h). Max ~1 request per 2–3 s per domain. Max ~5 pages per site
  (home, contact, about, footer links). Honest User-Agent with CRAWLER_CONTACT_URL.
- No login pages, no CAPTCHA solving, no proxy rotation, no private profiles,
  no SMTP probing of emails (syntax + MX check only).
- Social links only from the official website or OSM `contact:*` tags.
- Check the suppression list before saving and before exporting.
- No automatic outreach of any kind.

## Code rules

- Clean, secure, typed code. zod validation on every API input.
  Session check inside every route handler (not only middleware).
- Never expose secrets to the client. Env vars only. Never commit .env files.
- Errors: log details server-side, return safe messages to the client.
- Write tests for worker logic (pytest + mongomock). Run them before saying a step is done.
- Before writing code for a new step: show a short plan and wait for my OK.
- When debugging: find the root cause first, explain it, then fix.

## How to explain things to me

Simple English, step by step, friendly. Short Urdu explanation when introducing
a new concept. Explain WHY a solution works, not only what changed.

## Build steps

1. ✅ Setup, models + indexes, auth, job queue loop (done)
2. ✅ OSM discovery: Nominatim + Overpass → save businesses as leads (done)
3. ✅ Website crawler, extraction (JSON-LD first), verification, dedupe/merge (done)
4. ✅ Live job progress polish, cancel/retry endpoints (done)
5. ✅ Leads tables, search/filters, CSV/Excel export, saved leads, opt-out list, settings (done)
6. 🟡 Google Places (refs only), fuzzy dedupe, SSRF guard, usage limits (done); security review, deploy
7. ✅ Phase 1 (2026-11-14): contact check statuses + manual verification, migration (4,795 leads),
   job checkpoints/resume, exponential backoff, crawler retry, Google off + no matching/export.
   Next: Phase 2 (GeoNames/Natural Earth geocoder, Geofabrik extracts), Phase 3 (Overture, non-OSM lane),
   then the Great Britain pilot. Each phase needs approval.
8. ✅ Phase 2a (branch phase-2-geocoder-extracts): offline place search (GeoNames gazetteer) + Natural Earth
   search areas; Nominatim removed.
9. ✅ Phase 2b (same branch): Geofabrik extracts replace public Overpass completely (no fallback). Filtered
   gym files on the Railway Volume (/data/osm), metadata in `osmExtracts`; leases, pins, LRU eviction,
   stale-while-refresh. pyosmium only loads in the Docker image (Windows App Control blocks it locally).
10. ✅ Phase 2c (same branch): Overture Places via DuckDB over S3 GeoParquet, "overture" lane (`dataLane`),
   per-release 1° tile cache (/data/overture, `overtureTiles`), GERS IDs + aliases + provenance, Source
   filter, source-aware exports. Never matched with OSM. Leads without `dataLane` are OSM.
11. ✅ Phase 3 (same branch, local only): end-to-end tests with real data (`worker/scripts/e2e_local.py`),
   job lease fencing (`scrapingJobs.lease`) + SIGTERM hand-back, progress/checkpoints also on skipped rows,
   Overture pin renewal once a minute, lead sort indexes, streaming export, per-network login cap,
   authorization sweep over every API route (`web/tests/integration/authz.integration.test.ts`),
   per-user job-start lock (`jobStartLocks`), session invalidation on password change (`users.sessionVersion`),
   linear email scan, crawl test against a real local HTTP server.
