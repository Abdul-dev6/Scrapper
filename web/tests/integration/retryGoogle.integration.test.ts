// Regression: Retry must never carry Google over from an old job. Real handler + in-memory MongoDB.
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

let mongod: MongoMemoryServer;
let RETRY: (req: Request, ctx: { params: Promise<{ id: string }> }) => Promise<Response>;
let CREATE: (req: Request) => Promise<Response>;
let signSession: (p: { sub: string; email: string }) => Promise<string>;
const alice = new Types.ObjectId();
const mallory = new Types.ObjectId();
const jobs = () => mongoose.connection.db!.collection("scrapingJobs");

async function oldJob(sources: string[], status = "failed") {
  const _id = new Types.ObjectId();
  await jobs().insertOne({
    _id, ownerId: alice, location: { query: "London, UK" }, categories: ["gym"], sources, status,
    createdAt: new Date("2026-10-01"), updatedAt: new Date("2026-10-01"),
  });
  return _id;
}

async function retry(as: Types.ObjectId, id: Types.ObjectId) {
  cookieValue = await signSession({ sub: String(as), email: "x@test.test" });
  const res = await RETRY(new Request(`http://test/api/jobs/${id}/retry`, { method: "POST" }), {
    params: Promise.resolve({ id: String(id) }),
  });
  return { status: res.status, body: await res.json() };
}

before(async () => {
  mongod = await MongoMemoryServer.create();
  process.env.MONGODB_URI = mongod.getUri();
  process.env.MONGODB_DB = "retry_integration";
  process.env.AUTH_SECRET = "integration-test-secret-0123456789abcdef";
  ({ POST: RETRY } = await import("../../src/app/api/jobs/[id]/retry/route"));
  CREATE = (await import("../../src/app/api/jobs/route")).POST as unknown as typeof CREATE;
  ({ signSession } = await import("../../src/lib/session"));
  await (await import("../../src/lib/db")).connectDB();
});

beforeEach(async () => {
  await jobs().deleteMany({});
});

after(async () => {
  await mongoose.disconnect();
  await mongod.stop();
});

for (const [label, sources] of [
  ["osm + google", ["osm", "google"]],
  ["google only", ["google"]],
  ["google first", ["google", "osm"]],
] as const) {
  test(`retrying an old job with ${label} creates an OSM-only job`, async () => {
    const id = await oldJob([...sources]);
    const r = await retry(alice, id);
    assert.equal(r.status, 201);
    assert.equal(r.body.googleRemoved, true);
    const created = await jobs().findOne({ _id: new Types.ObjectId(r.body.job._id) });
    assert.deepEqual(created!.sources, ["osm"]);
    assert.equal(String(created!.retryOf), String(id));
    assert.deepEqual((await jobs().findOne({ _id: id }))!.sources, [...sources]); // history untouched
  });
}

test("retrying an OSM-only job keeps it OSM-only and doesn't mention Google", async () => {
  const r = await retry(alice, await oldJob(["osm"], "partial"));
  assert.equal(r.status, 201);
  assert.equal(r.body.googleRemoved, false);
  assert.deepEqual(r.body.job.sources, ["osm"]);
});

test("no retried job in the database ever contains Google", async () => {
  for (const s of [["osm", "google"], ["google"], ["osm"]]) await retry(alice, await oldJob(s, "cancelled"));
  assert.equal(await jobs().countDocuments({ retryOf: { $exists: true }, sources: "google" }), 0);
});

test("someone else's old Google job can't be retried", async () => {
  const r = await retry(mallory, await oldJob(["osm", "google"]));
  assert.equal(r.status, 404);
  assert.equal(await jobs().countDocuments({ retryOf: { $exists: true } }), 0);
});

test("a new search without sources defaults to OSM only (Google off by default)", async () => {
  cookieValue = await signSession({ sub: String(alice), email: "x@test.test" });
  const res = await CREATE(new Request("http://test/api/jobs", {
    method: "POST", body: JSON.stringify({ location: "Leeds, UK", categories: ["gym"] }),
  }));
  assert.equal(res.status, 201);
  const created = await jobs().findOne({ _id: new Types.ObjectId((await res.json()).job._id) });
  assert.deepEqual(created!.sources, ["osm"]);
});

test("the zod memory attack body is refused (413) before validation, using little memory", async () => {
  cookieValue = await signSession({ sub: String(alice), email: "x@test.test" });
  const attack = JSON.stringify({ location: "Leeds, UK", categories: Array(200_000).fill("bogus"), sources: ["osm"] });
  const before = process.memoryUsage().heapUsed;
  const res = await CREATE(new Request("http://test/api/jobs", { method: "POST", body: attack }));
  const grewMb = (process.memoryUsage().heapUsed - before) / 1e6;
  assert.equal(res.status, 413);
  assert.ok(grewMb < 40, `heap grew ${grewMb.toFixed(1)} MB`); // vs ~90-116 MB when zod validates it
  assert.equal(await jobs().countDocuments({}), 0);
});

test("a sources array longer than the allowed sources is rejected", async () => {
  cookieValue = await signSession({ sub: String(alice), email: "x@test.test" });
  const res = await CREATE(new Request("http://test/api/jobs", {
    method: "POST", body: JSON.stringify({ location: "Leeds, UK", categories: ["gym"], sources: ["osm", "osm", "osm"] }),
  }));
  assert.equal(res.status, 400);
});
