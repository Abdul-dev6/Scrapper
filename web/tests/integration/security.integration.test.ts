// Regression tests for the 2026-11-16 security review (web side): M3 job caps, M4 login limits,
// L3 sign out everywhere, L4 sign-up limits, L5 usage redaction, L7 export limit, L1 CSP, L8 Origin check.
// Real route handlers + middleware, throwaway in-memory MongoDB. Run with: npm run test:integration
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
let H: Record<string, Handler>;
let middleware: (req: import("next/server").NextRequest) => Promise<Response>;
let NextRequestCtor: typeof import("next/server").NextRequest;
let signSession: (p: { sub: string; email: string; ver?: number }) => Promise<string>;

const alice = new Types.ObjectId();
const bob = new Types.ObjectId();
const ALICE_EMAIL = "alice@test.test";
const PASSWORD = "correct horse battery";
const db = () => mongoose.connection.db!;
const jobs = () => db().collection("scrapingJobs");
const CITY = { _id: "gn:2643743", kind: "city", name: "London", countryCode: "GB", countryName: "United Kingdom",
  admin1Code: "ENG", admin1Name: "England", lat: 51.5, lon: -0.12, population: 8961989, keys: ["london"], ctxKeys: [] };
const COUNTRY = { _id: "country:GB", kind: "country", name: "United Kingdom", countryCode: "GB", countryName: "United Kingdom",
  population: 67000000, keys: ["united kingdom"], ctxKeys: [] };

const json = (url: string, body: unknown, headers: Record<string, string> = {}) =>
  new Request(url, { method: "POST", headers: { "content-type": "application/json", ...headers }, body: JSON.stringify(body) });
const login = (password: string, ip = "198.51.100.7") =>
  H.login(json("http://test/api/auth/login", { email: ALICE_EMAIL, password }, { "x-forwarded-for": ip }));
const signup = (email: string, ip: string) =>
  H.signup(json("http://test/api/auth/signup", { email, password: "a long enough password" }, { "x-forwarded-for": ip }));
const createJob = (placeId = "gn:2643743") =>
  H.createJob(json("http://test/api/jobs", { location: "London", placeId, categories: ["gym"] }));
const statuses = async (rs: Promise<Response>[]) => (await Promise.all(rs)).map((r) => r.status).sort();
const as = async (who: Types.ObjectId, email = `${who}@test.test`, ver = 0) =>
  (cookieValue = await signSession({ sub: String(who), email, ver }));
const pastJob = (owner: Types.ObjectId, hoursAgo: number, status = "completed", place: object = CITY) =>
  jobs().insertOne({ ownerId: owner, status, location: { query: "x", place }, categories: ["gym"], sources: ["osm"],
    createdAt: new Date(Date.now() - hoursAgo * 3_600_000) } as never);

before(async () => {
  mongod = await MongoMemoryServer.create();
  process.env.MONGODB_URI = mongod.getUri();
  process.env.MONGODB_DB = "security_integration";
  process.env.AUTH_SECRET = "integration-test-secret-0123456789abcdef";
  const server = await import("next/server");
  NextRequestCtor = server.NextRequest;
  const wrap = (h: (...args: never[]) => Promise<Response>): Handler => (req, c) => h(new NextRequestCtor(req) as never, c as never);
  H = {
    login: wrap((await import("../../src/app/api/auth/login/route")).POST),
    signup: wrap((await import("../../src/app/api/auth/signup/route")).POST),
    logout: wrap((await import("../../src/app/api/auth/logout/route")).POST),
    me: wrap((await import("../../src/app/api/auth/me/route")).GET),
    createJob: wrap((await import("../../src/app/api/jobs/route")).POST),
    exportLeads: wrap((await import("../../src/app/api/leads/export/route")).GET),
    usage: wrap((await import("../../src/app/api/usage/route")).GET),
  };
  middleware = (await import("../../src/middleware")).middleware as never;
  signSession = (await import("../../src/lib/session")).signSession;
  await (await import("../../src/lib/db")).connectDB();
  const models = await import("../../src/models");
  await models.LoginAttempt.syncIndexes();
  await models.User.syncIndexes();
  await models.JobStartLock.syncIndexes();
  const bcrypt = (await import("bcryptjs")).default;
  await db().collection("users").insertMany([
    { _id: alice, email: ALICE_EMAIL, passwordHash: await bcrypt.hash(PASSWORD, 4), sessionVersion: 0 },
    { _id: bob, email: `${bob}@test.test`, passwordHash: "x" },
  ] as never);
  await db().collection("gazetteer").insertMany([CITY, COUNTRY] as never);
});

beforeEach(async () => {
  await db().collection("loginAttempts").deleteMany({});
  await jobs().deleteMany({});
  await db().collection("users").updateOne({ _id: alice }, { $set: { sessionVersion: 0 } });
  for (const k of ["SIGNUP_GLOBAL_HOURLY_MAX", "JOB_USER_DAILY_MAX", "JOB_USER_DAILY_COUNTRY_MAX", "JOB_GLOBAL_OPEN_MAX",
    "JOB_GLOBAL_DAILY_MAX", "EXPORT_HOURLY_MAX", "ADMIN_EMAILS"]) delete process.env[k];
  cookieValue = undefined;
});

after(async () => {
  await mongoose.disconnect();
  await mongod.stop();
});

// ---------- M4: login limits ----------

test("30 simultaneous wrong passwords from one network: at most 5 are even checked", async () => {
  const got = await statuses(Array.from({ length: 30 }, () => login("wrong password!!")));
  assert.equal(got.filter((s) => s === 401).length, 5); // before the fix all 30 read count 0 and were checked
  assert.equal(got.filter((s) => s === 429).length, 25);
  assert.equal((await login(PASSWORD)).status, 429); // still locked, even with the right password
});

test("one account attacked from many networks is locked after 20 failures", async () => {
  const got = await statuses(Array.from({ length: 26 }, (_, i) => login("wrong password!!", `203.0.113.${i + 1}`)));
  assert.equal(got.filter((s) => s === 401).length, 20);
  assert.equal(got.filter((s) => s === 429).length, 6);
});

test("a correct login gives its counts back and resets the network+account counter", async () => {
  for (let i = 0; i < 3; i++) assert.equal((await login("wrong password!!")).status, 401);
  assert.equal((await login(PASSWORD)).status, 200);
  for (let i = 0; i < 5; i++) assert.equal((await login("wrong password!!")).status, 401); // 5 fresh tries again
  assert.equal((await login("wrong password!!")).status, 429);
});

// ---------- L4: sign-up limits ----------

test("10 simultaneous sign-ups from one network create exactly 5 accounts", async () => {
  const got = await statuses(Array.from({ length: 10 }, (_, i) => signup(`new${i}@test.test`, "192.0.2.50")));
  assert.deepEqual(got, [...Array(5).fill(201), ...Array(5).fill(429)]);
});

test("the global sign-up cap stops IP rotation", async () => {
  process.env.SIGNUP_GLOBAL_HOURLY_MAX = "3";
  const got = await statuses(Array.from({ length: 6 }, (_, i) => signup(`rot${i}@test.test`, `192.0.2.${100 + i}`)));
  assert.deepEqual(got, [201, 201, 201, 429, 429, 429]);
});

// ---------- L3: sign out everywhere ----------

test("sign out everywhere ends every older session; a plain logout clears the cookie with its flags", async () => {
  await as(alice, ALICE_EMAIL, 0);
  const stolen = cookieValue;
  assert.equal((await H.me(new Request("http://test/api/auth/me"))).status, 200);

  const plain = await H.logout(new Request("http://test/api/auth/logout", { method: "POST" }));
  const setCookie = plain.headers.get("set-cookie") ?? "";
  assert.match(setCookie, /ls_session=;/);
  assert.match(setCookie, /HttpOnly/i);
  assert.match(setCookie, /SameSite=lax/i);
  cookieValue = stolen;
  assert.equal((await H.me(new Request("http://test/api/auth/me"))).status, 200); // plain logout: token still valid

  await H.logout(json("http://test/api/auth/logout", { everywhere: true }));
  cookieValue = stolen;
  assert.equal((await H.me(new Request("http://test/api/auth/me"))).status, 401); // the copied token is dead now
});

// ---------- M3: job caps ----------

test("per-user daily cap", async () => {
  process.env.JOB_USER_DAILY_MAX = "2";
  await pastJob(alice, 5);
  await pastJob(alice, 6);
  await pastJob(alice, 30); // older than 24 h: doesn't count
  await as(alice);
  const r = await createJob();
  assert.equal(r.status, 429);
  assert.match((await r.json()).error.message, /per 24 hours/);
});

test("whole-country searches have their own daily cap; cities still work", async () => {
  await Promise.all([1, 2, 3].map((h) => pastJob(alice, h + 1, "completed", COUNTRY)));
  await as(alice);
  const country = await createJob("country:GB");
  assert.equal(country.status, 429);
  assert.match((await country.json()).error.message, /whole-country/);
  assert.equal((await createJob("gn:2643743")).status, 201);
});

test("global cap on open jobs counts every user's jobs", async () => {
  process.env.JOB_GLOBAL_OPEN_MAX = "2";
  await pastJob(bob, 0.1, "running");
  await pastJob(bob, 0.1, "queued");
  await as(alice);
  const r = await createJob();
  assert.equal(r.status, 429);
  assert.match((await r.json()).error.message, /busy/);
});

test("global daily cap", async () => {
  process.env.JOB_GLOBAL_DAILY_MAX = "3";
  for (let i = 0; i < 3; i++) await pastJob(bob, 2 + i);
  await as(alice);
  const r = await createJob();
  assert.equal(r.status, 429);
  assert.match((await r.json()).error.message, /today's limit/);
});

// ---------- L7: export limit ----------

test("exports are limited per user per hour", async () => {
  process.env.EXPORT_HOURLY_MAX = "2";
  await as(alice);
  const exp = () => H.exportLeads(new Request("http://test/api/leads/export?format=csv"));
  assert.equal((await exp()).status, 200);
  assert.equal((await exp()).status, 200);
  assert.equal((await exp()).status, 429);
  await as(bob);
  assert.equal((await exp()).status, 200); // per user, not shared
});

// ---------- L5: usage redaction ----------

test("only admins see Google cost and failure figures", async () => {
  const { monthKey } = await import("../../src/lib/googleUsage");
  await db().collection("apiUsage").insertOne({ _id: `google:${monthKey(new Date())}`, textSearchPro: 10,
    placeDetailsEssentials: 0, estimatedCostUsd: 12.5, failed: { textSearchPro: 2 } } as never);
  process.env.ADMIN_EMAILS = `other@test.test, ${ALICE_EMAIL}`;
  await as(bob);
  const user = (await (await H.usage(new Request("http://test/api/usage"))).json()).google;
  assert.equal(user.estimatedCostUsd, 0);
  assert.equal(user.failed.textSearchPro, 0);
  assert.equal(user.used.textSearchPro, 10); // the shared quota itself stays visible
  await as(alice, ALICE_EMAIL);
  const admin = (await (await H.usage(new Request("http://test/api/usage"))).json()).google;
  assert.equal(admin.estimatedCostUsd, 12.5);
  assert.equal(admin.failed.textSearchPro, 2);
  await db().collection("apiUsage").deleteMany({});
});

// ---------- L8 Origin check + L1 CSP (middleware) ----------

test("middleware refuses cross-site POSTs and sets a nonce CSP on pages", async () => {
  const token = await signSession({ sub: String(alice), email: ALICE_EMAIL });
  const req = (path: string, method: string, origin?: string) =>
    new NextRequestCtor(`http://app.test${path}`, {
      method,
      headers: { host: "app.test", cookie: `ls_session=${token}`, ...(origin ? { origin } : {}) },
    });

  assert.equal((await middleware(req("/api/jobs", "POST", "https://evil.test"))).status, 403);
  assert.equal((await middleware(req("/api/auth/login", "POST", "https://evil.test"))).status, 403); // public too
  assert.equal((await middleware(req("/api/jobs", "POST", "null"))).status, 403);
  assert.notEqual((await middleware(req("/api/jobs", "POST", "http://app.test"))).status, 403);
  assert.notEqual((await middleware(req("/api/jobs", "POST"))).status, 403); // no Origin (curl): cookie rules apply
  assert.notEqual((await middleware(req("/api/jobs", "GET", "https://evil.test"))).status, 403); // reads aren't blocked

  const page = await middleware(req("/leads", "GET"));
  const csp = page.headers.get("content-security-policy") ?? "";
  assert.match(csp, /script-src 'self' 'nonce-[A-Za-z0-9+/=]+' 'strict-dynamic'/);
  assert.match(csp, /frame-ancestors 'none'/);
  assert.match(csp, /object-src 'none'/);
  const again = (await middleware(req("/leads", "GET"))).headers.get("content-security-policy");
  assert.notEqual(again, csp); // a new nonce every request
});
