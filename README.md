# Lead Finder

Finds gyms, boxing/MMA/martial arts clubs, fitness centers and sports stores in a location,
then collects the contact details they publish on their own websites.

```
web/      Next.js app: dashboard UI + API (Vercel)
worker/   Python worker: discovery + website crawling (Railway)
```

Both talk only to MongoDB. The web app creates jobs, the worker claims and runs them.

## Setup (Step 1)

### 1. MongoDB Atlas
1. Create a free M0 cluster.
2. Database Access → create two users with **readWrite on `leadscraper` only**: one for web, one for the worker.
3. Network Access → add your IP (and later Railway's egress; Vercel needs `0.0.0.0/0` unless you use a paid integration).

### 2. Web app
```bash
cd web
npm install
cp .env.example .env.local      # fill in MONGODB_URI, AUTH_SECRET, ADMIN_EMAIL, ADMIN_PASSWORD
npm run db:indexes              # creates collections + all indexes
npm run seed:user               # creates your login
npm run dev                     # http://localhost:3000
```
Generate `AUTH_SECRET` with:
`node -e "console.log(require('crypto').randomBytes(48).toString('base64url'))"`

### 3. Worker
```bash
cd worker
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env            # fill in MONGODB_URI and a REAL CRAWLER_CONTACT_URL (see below)
pytest                          # 163 tests, no network needed
python -m app.main
```
`CRAWLER_CONTACT_URL` must be your real email (`mailto:you@...`) or a page you own.
OpenStreetMap blocks the placeholder `example.com` with HTTP 406.

### 4. Check it works
Sign in → Find leads → enter a city, region or country (e.g. "Manchester, UK") → Scraping jobs.
The job goes **queued → running → completed**, with "Found" and "New" counts.
A city takes about a minute; a whole country takes minutes, and its New count grows while it runs.
Only continents are rejected.
Run the same search again: "New" is 0, because existing leads are updated, not duplicated.

How discovery works (Step 2):
1. Nominatim turns the location text into an area (max 1 request/second, cached 30 days in `geocodeCache`).
2. Overpass searches that area for named places in the selected categories. If an area is too heavy
   for the free server, it is split into 4 and each quarter is searched (only crowded parts get split).
   Before each search the worker asks `/api/status` when a slot is free and waits exactly that long.
   Leads are saved after every piece; pieces that still fail are logged and the job ends as "partial".
3. Each place becomes one lead (`externalIds.osm`), and every contact value has `sourceUrl` = its OSM page.
   Chain branches sharing a website stay separate leads. Suppressed contacts/domains are skipped.

How the website check works (Step 3), after discovery, for every lead with a website:
1. robots.txt is read first (kept 24 h) and obeyed, including `Crawl-delay`. If it can't be read
   because the site has a server error, the site is skipped.
2. Max 5 pages per site: the home page, then the best "contact / impressum / about" links
   (footer links first). Never login, cart or legal fine-print pages. At least 2.5 s between
   two requests to the same site; up to 10 different sites are checked at the same time.
3. Contacts come from JSON-LD first, then `mailto:` / `tel:` / WhatsApp / social profile links,
   then emails written in the page text. Each value stores the page it was found on.
4. Emails: syntax + DNS MX record check (no SMTP, nothing is sent). Placeholder, image and
   role-only addresses (no-reply, privacy, careers...) are dropped. Phones are converted to
   international format (+44...) using the lead's country; invalid numbers are dropped.
5. Merge: website values replace older website values, OSM values and your notes/status are kept.
   A site that is down keeps its old data. Branches sharing one website URL are crawled once.
   A site checked in the last 7 days is not crawled again. If a contact on the site is on your
   opt-out list, the lead is removed.

Jobs page (Step 4):
- The progress bar updates every 3 s while a job runs: % done, current stage, time running.
- **Cancel**: a waiting job stops at once; a running job stops at its next progress report
  (seconds normally, about 30 s when the map server is down). Leads found so far are kept.
- **Retry** (failed / partial / cancelled jobs): starts a new job with the same search; the old
  one stays in the history. Re-running is cheap: leads are updated, not duplicated, and websites
  checked in the last 7 days are skipped.
- **Details**: counts (found, new, updated, websites, emails) and the list of problems.
- A warning appears if a running job hasn't reported for 3 minutes (is the worker running?).

Leads (Step 5):
- **All leads / Saved leads**: search, filters (country, city, business type, has email/phone/website,
  contact check, lead status, which search found it), sorting, 50 per page. Filters live in the URL,
  so a filtered view can be bookmarked. Click a name for every contact with its source page,
  ★ save, status, notes, and the opt-out button.
- **Export CSV / Excel**: exactly what the filters show, max 50,000 rows. The opt-out list is checked
  again before export. Cells starting with = + - @ get a leading ' so Excel never runs them as a
  formula (CSV injection); this is also why phones show as '+44... in CSV. Excel (.xlsx) is cleaner.
- **Settings → Opt-out list**: add an email, phone (with country code) or domain. It is stored only
  as a sha256 hash (same format the worker checks). Matching leads are deleted, future searches skip
  them, and exports never include them. Settings also has "change password".

Google Places (Step 6, optional):
- Put `GOOGLE_PLACES_API_KEY` in `worker/.env` (or Railway's variables); the worker also reads the repo-root `.env`.
  The key is used only by the worker, never by the web app or browser. Restrict it to "Places API (New)".
- Run `npm run db:indexes` once (new collections: `apiUsage`, `apiUsageLog`, `googlePlaceRefs`, `googleCoverage`).
- Google is OFF by default. Ticking "Also search Google Maps" on Find leads runs Google Text Search at the same
  time as OpenStreetMap (max 50 Google searches per location; crowded areas are split into smaller tiles; a
  repeated search skips tiles searched in the last 30 days and goes deeper).
- Google results are NEVER matched with or added to leads. Each place is stored only as its place ID
  (`googlePlaceRefs`, unique per user) and shown as an "open in Google Maps" link in the job details.
  Nothing from Google (names, addresses, coordinates, place IDs) is ever exported. Place Details is not used.
- Monthly limits, shared by everyone: Text Search 4,500 (Google free: 5,000) and Place Details 9,000 (free: 10,000).
  Each request is reserved atomically before it is sent. When either limit is reached, Google stops completely and
  searches run OpenStreetMap-only until the next month (Pacific time). Usage is shown on Find leads and Jobs,
  and every request is logged in `apiUsageLog` with its estimated cost. Free caps are per billing account:
  other projects on the same account share them.

Duplicates and safety (Step 6):
- The same gym under two OSM ids (node + building), or found again later, is merged by phone / name + distance /
  website + distance; chain branches (different names or places) stay separate. Unique indexes stop two workers
  inserting the same business at once.
- The crawler refuses websites that point to private/internal addresses (SSRF protection), on every redirect too.
- Excel export adds full address, data sources, last contact check and an "About this data" sheet with
  OpenStreetMap (ODbL) attribution. `npm test` in web/ checks the export.

Contact checks and retries (Phase 1, 2026-11-14):
- Each email has its own checks: syntax, mail server (MX: found / none / not checked) and "verified by you".
  Each phone: valid number format and "verified by you". A lead is "Checked automatically" when a check passed and
  "Verified by you" ONLY after you click "Mark verified" on a contact in the lead panel. Re-crawls keep your marks.
- Excel columns: Email syntax, Email MX check, Email manually verified, Phone format, Phone manually verified
  (one value per email/phone, same order as the Emails/Phones column).
- `npm run db:backup` saves and verifies a copy of `leads` in `backups/` (git-ignored).
  `npm run db:migrate:contacts` shows what the status migration would change; add `-- --apply --backup=<dir>` to run it
  (it refuses without a verified backup from the last 24 hours). Done on 2026-11-14 for all 4,795 leads.
- Jobs save checkpoints. If the worker restarts, or the map server is temporarily down, the job is retried after
  ~1, ~4, then ~16 minutes and continues where it stopped (finished map pieces aren't searched again). A page that
  times out or answers 429/5xx is retried once, politely.

Place search and search areas (Phase 2a, 2026-11-14). No public Nominatim:
- Find leads: type a place and press **Find place**. Matches come from the `gazetteer` collection (GeoNames,
  CC BY 4.0); if several match (London GB / Ontario / Ohio...) you pick one. Nothing you type leaves the app.
- Load or refresh the gazetteer (needs `npm run db:indexes` once). Download `cities1000.zip`, `countryInfo.txt`
  and `admin1CodesASCII.txt` from https://download.geonames.org/export/dump/ into one folder, then
  `cd worker && python -m app.geo.gazetteer load <folder>` (atomic swap; ~172k places, ~115 MB incl. index).
- The worker builds the search area from the picked place with Natural Earth (public domain) shapes, pinned by
  version + sha256 (`app/geo/data.py`): countries, regions, urban outlines, else a radius by population, always
  clipped to the country. Places outside the area are dropped. The job records the boundary source and version.
- Searches created before this change can't be retried (they have no picked place): start a new search.

## Build steps
1. ✅ Setup, models + indexes, auth, job queue
2. ✅ OpenStreetMap discovery (Nominatim + Overpass)
3. ✅ Website crawler, extraction, verification, dedupe
4. ✅ Live job progress, cancel/retry
5. Leads tables, filters, CSV/Excel export, opt-out list
6. 🟡 Google Places + OSM, dedupe, SSRF guard, usage limits (done); deployment
