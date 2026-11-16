// Concurrency regression for the job limits (max 3 open jobs, max 10 per hour). Real create/retry
// handlers + in-memory MongoDB. Before the per-user lock (lib/jobs.ts startJobLimited) simultaneous
// requests all counted the same open jobs, and all of them created one.
// Run with: npm run test:integration
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
let CREATE: Handler, RETRY: Handler;
const alice = new Types.ObjectId();
const bob = new Types.ObjectId();
const db = () => mongoose.connection.db!;
const jobs = () => db().collection("scrapingJobs");
const PLACE = { _id: "gn:2643743", kind: "city", name: "London", countryCode: "GB", countryName: "United Kingdom",
  admin1Code: "ENG", admin1Name: "England", lat: 51.5, lon: -0.12, population: 8961989, keys: ["london"], ctxKeys: [] };

async function signIn(who: Types.ObjectId) {
  const { signSession } = await import("../../src/lib/session");
  cookieValue = await signSession({ sub: String(who), email: `${who}@test.test` });
}
const create = () =>
  CREATE(new Request("http://test/api/jobs", { method: "POST", body: JSON.stringify({ location: "London", placeId: "gn:2643743", categories: ["gym"] }) }));
const retry = (id: Types.ObjectId) => RETRY(new Request(`http://test/api/jobs/${id}/retry`, { method: "POST" }), { params: Promise.resolve({ id: String(id) }) });
const statuses = async (rs: Promise<Response>[]) => (await Promise.all(rs)).map((r) => r.status).sort();
const open = (owner = alice) => jobs().countDocuments({ ownerId: owner, status: { $in: ["queued", "running"] } });
const oldJob = async (status: string, owner = alice, hoursAgo = 2) =>
  (await jobs().insertOne({ ownerId: owner, status, location: { query: "London", place: PLACE }, categories: ["gym"], sources: ["osm"],
    createdAt: new Date(Date.now() - hoursAgo * 3_600_000) } as never)).insertedId as Types.ObjectId;

before(async () => {
  mongod = await MongoMemoryServer.create();
  process.env.MONGODB_URI = mongod.getUri();
  process.env.MONGODB_DB = "joblimit_integration";
  process.env.AUTH_SECRET = "integration-test-secret-0123456789abcdef";
  const { NextRequest } = await import("next/server");
  const wrap = (h: (...args: never[]) => Promise<Response>): Handler => (req, c) => h(new NextRequest(req) as never, c as never);
  CREATE = wrap((await import("../../src/app/api/jobs/route")).POST);
  RETRY = wrap((await import("../../src/app/api/jobs/[id]/retry/route")).POST);
  await (await import("../../src/lib/db")).connectDB();
  const { JobStartLock } = await import("../../src/models");
  await JobStartLock.syncIndexes();
  await db().collection("users").insertMany([alice, bob].map((_id) => ({ _id, email: `${_id}@test.test`, passwordHash: "x" })) as never);
  await db().collection("gazetteer").insertOne(PLACE as never);
});

beforeEach(async () => {
  await jobs().deleteMany({});
  await db().collection("jobStartLocks").deleteMany({});
  await signIn(alice);
});

after(async () => {
  await mongoose.disconnect();
  await mongod.stop();
});

test("12 simultaneous new searches create exactly 3 jobs; the rest get 429", async () => {
  const got = await statuses(Array.from({ length: 12 }, create));
  assert.deepEqual(got, [...Array(3).fill(201), ...Array(9).fill(429)]);
  assert.equal(await open(), 3);
  assert.equal(await db().collection("jobStartLocks").countDocuments(), 0); // every lock was released
});

test("with 2 jobs open, simultaneous searches AND retries together add only 1 more", async () => {
  await oldJob("running");
  await oldJob("queued");
  const failed = await Promise.all([1, 2, 3].map(() => oldJob("failed")));
  const got = await statuses([...Array.from({ length: 4 }, create), ...failed.map(retry)]);
  assert.deepEqual(got, [201, ...Array(6).fill(429)]);
  assert.equal(await open(), 3);
});

test("the hourly limit holds under concurrency too (9 this hour + 6 at once = 10)", async () => {
  for (let i = 0; i < 9; i++) await oldJob("completed", alice, 0.1);
  const got = await statuses(Array.from({ length: 6 }, create));
  assert.deepEqual(got, [201, ...Array(5).fill(429)]);
  assert.equal(await jobs().countDocuments({ ownerId: alice, createdAt: { $gte: new Date(Date.now() - 3_600_000) } }), 10);
});

test("the lock is per user: another user's searches are not blocked or counted", async () => {
  const forAlice = Array.from({ length: 5 }, create);
  await signIn(bob); // cookies() is read when each handler runs: Alice's requests already read theirs
  const all = await Promise.all([...forAlice, ...Array.from({ length: 5 }, create)]);
  assert.equal(all.filter((r) => r.status === 201).length, 6);
  assert.equal(await open(alice) + await open(bob), 6);
});

test("a lock left by a crashed request expires; a live one makes the request wait, then answer 429", async () => {
  const locks = db().collection("jobStartLocks");
  await locks.insertOne({ _id: alice, token: "crashed", until: new Date(Date.now() - 1000), expiresAt: new Date(Date.now() + 60_000) } as never);
  assert.equal((await create()).status, 201); // expired lock taken over
  await locks.insertOne({ _id: alice, token: "busy", until: new Date(Date.now() + 60_000), expiresAt: new Date(Date.now() + 60_000) } as never);
  const t0 = Date.now();
  const res = await create();
  assert.equal(res.status, 429);
  assert.match((await res.json()).error.message, /being started right now/);
  assert.ok(Date.now() - t0 >= 2500, "waited for the other request first");
  assert.equal((await locks.findOne({ _id: alice } as never))?.token, "busy"); // someone else's lock is never removed
  assert.equal(await open(), 1);
});
