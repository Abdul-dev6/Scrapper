// Phase 3: authentication + authorization sweep over EVERY API route, with real handlers, a real signed
// session and a throwaway in-memory MongoDB. Mallory (another signed-in user) must never see, change or
// delete Alice's jobs, leads or opt-out entries: for her they simply "don't exist" (404 / empty lists).
// Also: forged sessions, the middleware, and the export (only the user's rows, same values as before
// the export's field projection). Run with: npm run test:integration
import assert from "node:assert/strict";
import { after, before, beforeEach, mock, test } from "node:test";
import { MongoMemoryServer } from "mongodb-memory-server";
import mongoose, { Types } from "mongoose";

let cookieValue: string | undefined;
mock.module("next/headers", {
  namedExports: {
    cookies: async () => ({ get: (name: string) => (cookieValue ? { name, value: cookieValue } : undefined) }),
  },
});

type Handler = (req: Request, ctx?: unknown) => Promise<Response>;
let mongod: MongoMemoryServer;
let signSession: (p: { sub: string; email: string }) => Promise<string>;
let H: Record<string, Handler>;
let NextRequestCtor: typeof import("next/server").NextRequest;

const alice = new Types.ObjectId();
const mallory = new Types.ObjectId();
const aliceJob = new Types.ObjectId();
const aliceLead = new Types.ObjectId();
const aliceOverture = new Types.ObjectId();
const aliceEntry = new Types.ObjectId();
const db = () => mongoose.connection.db!;
const ctx = (id: string) => ({ params: Promise.resolve({ id }) });
const as = async (who: Types.ObjectId | null) =>
  (cookieValue = who ? await signSession({ sub: String(who), email: `${who}@test.test` }) : undefined);

const t = new Date("2026-11-10T10:00:00Z");
const LEADS = [
  {
    _id: aliceLead, ownerId: alice, name: "Iron Fist", nameNormalized: "iron fist", categories: ["boxing_club"],
    address: { city: "London", countryCode: "GB", street: "1 High St", postcode: "E1 1AA" },
    website: { url: "https://ironfist.test", domain: "ironfist.test", reachable: true },
    emails: [{ value: "info@ironfist.test", kind: "generic", mxValid: true, sourceUrl: "https://ironfist.test/contact",
               sourceType: "website", checks: { syntaxValid: true, mx: "valid", manuallyVerified: true } }],
    phones: [{ e164: "+442079460000", raw: "020 7946 0000", sourceUrl: "https://ironfist.test/", sourceType: "website",
               checks: { formatValid: true, manuallyVerified: false } }],
    socials: { instagram: { url: "https://instagram.com/ironfist", sourceUrl: "https://ironfist.test/", sourceType: "website" } },
    externalIds: { osm: "node/1", osmAliases: ["way/9"], googlePlaceId: "ChIJsecret" },
    fieldSources: { name: { sourceType: "osm", sourceUrl: "https://www.openstreetmap.org/node/1" } },
    mergeLog: [{ at: t, osm: "way/9", reason: "same phone" }],
    contactStatus: "verified", contactsVersion: 2, confidence: 0.85, saved: true, status: "contacted", notes: "call back",
    jobIds: [aliceJob], discoveredAt: t, verifiedAt: t, createdAt: t, updatedAt: t,
  },
  {
    _id: aliceOverture, ownerId: alice, name: "Iron Fist Gym", nameNormalized: "iron fist gym", categories: ["gym"],
    dataLane: "overture", address: { city: "London", countryCode: "GB" },
    phones: [{ e164: "+442079460001", raw: "+44 20 7946 0001", sourceUrl: "overture:2026-10-21.0:gers-1", sourceType: "overture" }],
    externalIds: { overture: "gers-1", overtureSources: ["meta:1"] },
    overture: { release: "2026-10-21.0", gersId: "gers-1", taxonomy: { primary: "gym" },
                sources: [{ dataset: "meta", license: "CDLA-Permissive-2.0", recordId: "1" }, { dataset: "Foursquare", license: "Apache-2.0" }] },
    contactStatus: "checked", confidence: 0.5, saved: false, status: "new", notes: "",
    jobIds: [aliceJob], discoveredAt: new Date(t.getTime() + 1000), createdAt: t, updatedAt: t,
  },
];

before(async () => {
  mongod = await MongoMemoryServer.create();
  process.env.MONGODB_URI = mongod.getUri();
  process.env.MONGODB_DB = "authz_integration";
  process.env.AUTH_SECRET = "integration-test-secret-0123456789abcdef";
  const { NextRequest } = await import("next/server");
  NextRequestCtor = NextRequest;
  const wrap = (h: (...args: never[]) => Promise<Response>): Handler => (req, c) => h(new NextRequest(req) as never, c as never);
  const r = (p: string) => import(`../../src/app/api/${p}/route`);
  H = {
    jobsList: wrap((await r("jobs")).GET), jobsCreate: wrap((await r("jobs")).POST),
    jobGet: wrap((await r("jobs/[id]")).GET), jobCancel: wrap((await r("jobs/[id]/cancel")).POST),
    jobRetry: wrap((await r("jobs/[id]/retry")).POST),
    leadsList: wrap((await r("leads")).GET), leadGet: wrap((await r("leads/[id]")).GET),
    leadPatch: wrap((await r("leads/[id]")).PATCH), leadOptOut: wrap((await r("leads/[id]/opt-out")).POST),
    leadsExport: wrap((await r("leads/export")).GET), leadsFacets: wrap((await r("leads/facets")).GET),
    places: wrap((await r("places")).GET), usage: wrap((await r("usage")).GET),
    suppList: wrap((await r("suppression")).GET), suppAdd: wrap((await r("suppression")).POST),
    suppDelete: wrap((await r("suppression/[id]")).DELETE),
    me: wrap((await r("auth/me")).GET), password: wrap((await r("auth/password")).POST),
  };
  ({ signSession } = await import("../../src/lib/session"));
  await (await import("../../src/lib/db")).connectDB();
  // Sessions are only valid for users that exist (lib/auth.ts), so the test users exist.
  await mongoose.connection.db!.collection("users").insertMany([{ _id: alice, email: `${alice}@test.test`, passwordHash: "x" }, { _id: mallory, email: `${mallory}@test.test`, passwordHash: "x" }] as never);
});

beforeEach(async () => {
  for (const c of ["scrapingJobs", "leads", "suppressionList"]) await db().collection(c).deleteMany({});
  await db().collection("scrapingJobs").insertOne({
    _id: aliceJob, ownerId: alice, status: "failed", location: { query: "London", place: { _id: "gn:2643743" } },
    categories: ["gym"], sources: ["osm", "overture"], workerId: "w1", lease: "secret-lease", checkpoint: { osm: { position: 5 } },
    errorLog: [{ at: t, stage: "x", message: "boom" }], createdAt: t, updatedAt: t,
  } as never);
  await db().collection("leads").insertMany(LEADS as never);
  await db().collection("suppressionList").insertOne({ _id: aliceEntry, ownerId: alice, type: "email", valueHash: "a".repeat(64),
    reason: "asked", addedAt: t } as never);
});

after(async () => {
  await mongoose.disconnect();
  await mongod.stop();
});

const get = (url: string) => new Request(`http://test${url}`);
const post = (url: string, body?: unknown, method = "POST") =>
  new Request(`http://test${url}`, { method, body: body === undefined ? undefined : JSON.stringify(body) });

// Every route that needs a session, with a request that would succeed for the right user.
const ROUTES: [string, string, () => Request, unknown?][] = [
  ["jobsList", "GET /api/jobs", () => get("/api/jobs")],
  ["jobsCreate", "POST /api/jobs", () => post("/api/jobs", { location: "London", placeId: "gn:2643743", categories: ["gym"] })],
  ["jobGet", "GET /api/jobs/[id]", () => get(`/api/jobs/${aliceJob}`), ctx(String(aliceJob))],
  ["jobCancel", "POST /api/jobs/[id]/cancel", () => post(`/api/jobs/${aliceJob}/cancel`), ctx(String(aliceJob))],
  ["jobRetry", "POST /api/jobs/[id]/retry", () => post(`/api/jobs/${aliceJob}/retry`), ctx(String(aliceJob))],
  ["leadsList", "GET /api/leads", () => get("/api/leads")],
  ["leadGet", "GET /api/leads/[id]", () => get(`/api/leads/${aliceLead}`), ctx(String(aliceLead))],
  ["leadPatch", "PATCH /api/leads/[id]", () => post(`/api/leads/${aliceLead}`, { notes: "x" }, "PATCH"), ctx(String(aliceLead))],
  ["leadOptOut", "POST /api/leads/[id]/opt-out", () => post(`/api/leads/${aliceLead}/opt-out`), ctx(String(aliceLead))],
  ["leadsExport", "GET /api/leads/export", () => get("/api/leads/export?format=csv")],
  ["leadsFacets", "GET /api/leads/facets", () => get("/api/leads/facets")],
  ["places", "GET /api/places", () => get("/api/places?q=London")],
  ["usage", "GET /api/usage", () => get("/api/usage")],
  ["suppList", "GET /api/suppression", () => get("/api/suppression")],
  ["suppAdd", "POST /api/suppression", () => post("/api/suppression", { type: "domain", value: "x.test" })],
  ["suppDelete", "DELETE /api/suppression/[id]", () => post(`/api/suppression/${aliceEntry}`, undefined, "DELETE"), ctx(String(aliceEntry))],
  ["me", "GET /api/auth/me", () => get("/api/auth/me")],
  ["password", "POST /api/auth/password", () => post("/api/auth/password", { current: "x", next: "y".repeat(12) })],
];

async function snapshot() {
  const docs = async (c: string) => db().collection(c).find({}, { sort: { _id: 1 } }).toArray();
  return JSON.stringify([await docs("scrapingJobs"), await docs("leads"), await docs("suppressionList")]);
}

test("every route refuses a request without a session (401) and changes nothing", async () => {
  await as(null);
  const before = await snapshot();
  for (const [key, label, req, c] of ROUTES) {
    const res = await H[key](req(), c);
    assert.equal(res.status, 401, label);
  }
  assert.equal(await snapshot(), before);
});

test("forged, tampered or wrongly signed sessions are refused", async () => {
  const good = await signSession({ sub: String(alice), email: "a@test.test" });
  const [h, p] = good.split(".");
  const none = `${Buffer.from(JSON.stringify({ alg: "none", typ: "JWT" })).toString("base64url")}.${p}.`;
  const otherSub = Buffer.from(JSON.stringify({ ...JSON.parse(Buffer.from(p, "base64url").toString()), sub: String(mallory) })).toString("base64url");
  const { SignJWT } = await import("jose");
  const wrongKey = await new SignJWT({ email: "a@test.test" }).setProtectedHeader({ alg: "HS256" }).setSubject(String(alice))
    .setExpirationTime("1h").sign(new TextEncoder().encode("another-secret-another-secret-0000000"));
  const expired = await new SignJWT({ email: "a@test.test" }).setProtectedHeader({ alg: "HS256" }).setSubject(String(alice))
    .setIssuedAt(1).setExpirationTime(2).sign(new TextEncoder().encode(process.env.AUTH_SECRET!));
  for (const token of [none, `${h}.${otherSub}.${good.split(".")[2]}`, wrongKey, expired, "garbage", ""]) {
    cookieValue = token;
    const res = await H.leadsList(get("/api/leads"));
    assert.equal(res.status, 401, token.slice(0, 20));
  }
});

test("another user can't read, change, cancel, retry, opt out, export or delete Alice's data", async () => {
  await as(mallory);
  const before = await snapshot();
  const expectStatus = async (key: string, req: Request, c: unknown, status: number) => {
    const res = await H[key](req, c);
    assert.equal(res.status, status, key);
    return res;
  };
  await expectStatus("jobGet", get(`/api/jobs/${aliceJob}`), ctx(String(aliceJob)), 404);
  await expectStatus("jobCancel", post(`/api/jobs/${aliceJob}/cancel`), ctx(String(aliceJob)), 404);
  await expectStatus("jobRetry", post(`/api/jobs/${aliceJob}/retry`), ctx(String(aliceJob)), 404);
  await expectStatus("leadGet", get(`/api/leads/${aliceLead}`), ctx(String(aliceLead)), 404);
  await expectStatus("leadPatch", post(`/api/leads/${aliceLead}`, { saved: false, notes: "pwned" }, "PATCH"), ctx(String(aliceLead)), 404);
  await expectStatus("leadPatch", post(`/api/leads/${aliceLead}`, { verifyContact: { kind: "email", value: "info@ironfist.test", verified: false } }, "PATCH"), ctx(String(aliceLead)), 404);
  await expectStatus("leadOptOut", post(`/api/leads/${aliceLead}/opt-out`), ctx(String(aliceLead)), 404);
  await expectStatus("suppDelete", post(`/api/suppression/${aliceEntry}`, undefined, "DELETE"), ctx(String(aliceEntry)), 404);

  const jobs = await (await expectStatus("jobsList", get("/api/jobs"), undefined, 200)).json();
  assert.deepEqual(jobs.jobs, []);
  const leads = await (await expectStatus("leadsList", get("/api/leads?lane=all"), undefined, 200)).json();
  assert.equal(leads.total, 0);
  // Filtering by Alice's job id doesn't help either: every query is scoped to the signed-in user.
  assert.equal((await (await H.leadsList(get(`/api/leads?job=${aliceJob}`))).json()).total, 0);
  const facets = await (await expectStatus("leadsFacets", get("/api/leads/facets"), undefined, 200)).json();
  assert.deepEqual(facets, { countries: [], jobs: [] });
  const supp = await (await expectStatus("suppList", get("/api/suppression"), undefined, 200)).json();
  assert.deepEqual(supp.entries, []);
  const csv = await (await expectStatus("leadsExport", get("/api/leads/export?format=csv"), undefined, 200)).text();
  assert.equal(csv.trim().split("\r\n").length, 1); // the header only
  assert.ok(!csv.includes("Iron Fist"));

  // Mallory adding the same opt-out value only affects Mallory: Alice's lead is still there.
  await expectStatus("suppAdd", post("/api/suppression", { type: "email", value: "info@ironfist.test" }), undefined, 201);
  assert.equal(await db().collection("leads").countDocuments({ _id: aliceLead }), 1);
  await db().collection("suppressionList").deleteMany({ ownerId: mallory });
  assert.equal(await snapshot(), before); // nothing of Alice's changed
});

test("job details never expose worker internals (worker id, lease, checkpoint, owner)", async () => {
  await as(alice);
  const one = await (await H.jobGet(get(`/api/jobs/${aliceJob}`), ctx(String(aliceJob)))).json();
  const list = await (await H.jobsList(get("/api/jobs"))).json();
  for (const job of [one.job, list.jobs[0]]) {
    for (const key of ["workerId", "lease", "checkpoint", "ownerId"]) assert.equal(key in job, false, key);
  }
  assert.equal(one.job.errorLog.length, 1); // the details panel still gets the error log
});

test("export: only the user's rows, both lanes, and exactly the values the full documents give", async () => {
  const { exportRow, csvLine, EXPORT_COLUMNS } = await import("../../src/lib/leads");
  await as(alice);
  await db().collection("leads").insertOne({ ...LEADS[0], _id: new Types.ObjectId(), ownerId: mallory, name: "Mallory Gym",
    nameNormalized: "mallory gym", externalIds: { osm: "node/77" } } as never);
  const res = await H.leadsExport(get("/api/leads/export?format=csv&sort=oldest"));
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("x-export-rows"), "2");
  const lines = (await res.text()).replace(/^﻿/, "").trim().split("\r\n");
  assert.equal(lines[0], csvLine([...EXPORT_COLUMNS]));
  const full = await db().collection("leads").find({ ownerId: alice }, { sort: { discoveredAt: 1 } }).toArray();
  assert.deepEqual(lines.slice(1), full.map((l) => csvLine(exportRow(l as never))));
  assert.ok(!lines.join("\n").includes("Mallory") && !lines.join("\n").includes("ChIJsecret"));
  const lane = EXPORT_COLUMNS.indexOf("Data lane");
  assert.deepEqual(full.map((l) => exportRow(l as never)[lane]), ["OpenStreetMap", "Overture Maps"]);

  const x = await H.leadsExport(get("/api/leads/export?format=xlsx&lane=overture"));
  assert.equal(x.status, 200);
  assert.equal(x.headers.get("x-export-rows"), "1");
  const ExcelJS = (await import("exceljs")).default;
  const wb = new ExcelJS.Workbook();
  await wb.xlsx.load(Buffer.from(await x.arrayBuffer()) as never);
  const sheet = wb.getWorksheet("Leads")!;
  assert.equal(sheet.rowCount, 2);
  assert.equal(sheet.getRow(2).getCell(1).value, "Iron Fist Gym");
  assert.match(String(sheet.getRow(2).getCell(EXPORT_COLUMNS.indexOf("License") + 1).value), /CDLA-Permissive-2\.0 \/ Apache-2\.0/);
  assert.ok(wb.getWorksheet("About this data"));
});

test("export skips leads that are on the opt-out list and says how many", async () => {
  const { suppressionHash } = await import("../../src/lib/suppression");
  await as(alice);
  await db().collection("suppressionList").insertOne({ ownerId: alice, type: "domain", valueHash: suppressionHash("domain", "ironfist.test"),
    reason: "x", addedAt: t } as never);
  const res = await H.leadsExport(get("/api/leads/export?format=csv"));
  assert.equal(res.headers.get("x-export-rows"), "1");
  assert.equal(res.headers.get("x-export-skipped-opt-out"), "1");
});

test("middleware: APIs answer 401, pages redirect to sign-in, sign-in pages stay public", async () => {
  const { middleware } = await import("../../src/middleware");
  const call = (path: string, cookie?: string) =>
    middleware(new NextRequestCtor(`http://test${path}`, cookie ? { headers: { cookie: `ls_session=${cookie}` } } : undefined));
  assert.equal((await call("/api/leads")).status, 401);
  const page = await call("/leads");
  assert.equal(page.status, 307);
  assert.equal(new URL(page.headers.get("location")!).pathname, "/login");
  assert.equal(new URL(page.headers.get("location")!).searchParams.get("next"), "/leads");
  for (const open of ["/login", "/signup", "/api/auth/login", "/api/auth/signup"]) {
    assert.equal((await call(open)).headers.get("x-middleware-next"), "1", open);
  }
  const token = await signSession({ sub: String(alice), email: "a@test.test" });
  assert.equal((await call("/api/leads", token)).headers.get("x-middleware-next"), "1");
  assert.equal((await call("/api/leads", token + "x")).status, 401);
});

test("login: an account locks after 5 failures from one network; one network is capped across accounts", async () => {
  const bcrypt = (await import("bcryptjs")).default;
  const { createHash } = await import("node:crypto");
  const login = (await import("../../src/app/api/auth/login/route")).POST;
  await db().collection("users").deleteMany({ email: "alice@test.test" });
  await db().collection("loginAttempts").deleteMany({});
  await db().collection("users").insertOne({ email: "alice@test.test", passwordHash: await bcrypt.hash("correct horse battery", 4) } as never);
  const attempt = (ip: string, password: string, email = "alice@test.test") =>
    login(new NextRequestCtor("http://test/api/auth/login", {
      method: "POST", headers: { "x-forwarded-for": ip }, body: JSON.stringify({ email, password }),
    }));

  for (let i = 0; i < 5; i++) assert.equal((await attempt("198.51.100.1", "wrong password")).status, 401);
  assert.equal((await attempt("198.51.100.1", "correct horse battery")).status, 429); // locked, even with the right password
  const ok = await attempt("198.51.100.2", "correct horse battery"); // the lock is per network + account
  assert.equal(ok.status, 200);
  assert.match(ok.headers.get("set-cookie") ?? "", /ls_session=.+HttpOnly/i);

  // A network that already failed 50 times (e.g. against 10+ different accounts) is refused for every account.
  const ipKey = createHash("sha256").update("login-ip|203.0.113.9").digest("hex");
  await db().collection("loginAttempts").insertOne({ key: ipKey, count: 50, expiresAt: new Date(Date.now() + 60_000) } as never);
  assert.equal((await attempt("203.0.113.9", "correct horse battery")).status, 429);
  // ...and each failure counts for the network too
  await attempt("203.0.113.10", "wrong password");
  const counted = await db().collection("loginAttempts").findOne({ key: createHash("sha256").update("login-ip|203.0.113.10").digest("hex") });
  assert.equal(counted?.count, 1);
});
