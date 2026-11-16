// Place search + job creation with a picked place. Real handlers + in-memory MongoDB.
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
let SEARCH: Handler, CREATE: Handler, RETRY: Handler;
let signSession: (p: { sub: string; email: string }) => Promise<string>;
const alice = new Types.ObjectId();
const db = () => mongoose.connection.db!;

const UK = ["united kingdom", "gb", "gbr", "uk"];
const GAZ = [
  { _id: "country:GB", kind: "country", name: "United Kingdom", countryCode: "GB", countryName: "United Kingdom", population: 66488991, keys: UK, ctxKeys: [] },
  { _id: "admin1:GB.ENG", kind: "admin1", name: "England", countryCode: "GB", countryName: "United Kingdom", admin1Code: "ENG", population: 62609530, cityBbox: [50, -5.7, 55.8, 1.7], keys: ["england"], ctxKeys: UK },
  { _id: "gn:2643743", kind: "city", name: "London", countryCode: "GB", countryName: "United Kingdom", admin1Code: "ENG", admin1Name: "England", lat: 51.50853, lon: -0.12574, population: 8961989, keys: ["london", "londres"], ctxKeys: [...UK, "england"] },
  { _id: "gn:6058560", kind: "city", name: "London", countryCode: "CA", countryName: "Canada", admin1Code: "08", admin1Name: "Ontario", lat: 42.98, lon: -81.23, population: 422324, keys: ["london"], ctxKeys: ["canada", "ca", "can", "ontario"] },
  { _id: "gn:2643123", kind: "city", name: "Manchester", countryCode: "GB", countryName: "United Kingdom", admin1Code: "ENG", admin1Name: "England", lat: 53.48, lon: -2.24, population: 568996, keys: ["manchester"], ctxKeys: [...UK, "england"] },
];

const asAlice = async () => (cookieValue = await signSession({ sub: String(alice), email: "a@test.test" }));
async function search(q: string) {
  const res = await SEARCH(new Request(`http://test/api/places?q=${encodeURIComponent(q)}`) as never);
  return { status: res.status, body: await res.json() };
}
async function create(body: unknown) {
  const res = await CREATE(new Request("http://test/api/jobs", { method: "POST", body: JSON.stringify(body) }) as never);
  return { status: res.status, body: await res.json() };
}

before(async () => {
  mongod = await MongoMemoryServer.create();
  process.env.MONGODB_URI = mongod.getUri();
  process.env.MONGODB_DB = "places_integration";
  process.env.AUTH_SECRET = "integration-test-secret-0123456789abcdef";
  // Next's route handlers expect NextRequest; nextUrl is the only extra they use here.
  const { NextRequest } = await import("next/server");
  const wrap = (h: (...args: never[]) => Promise<Response>): Handler => (req, ctx) => h(new NextRequest(req) as never, ctx as never);
  SEARCH = wrap((await import("../../src/app/api/places/route")).GET);
  CREATE = wrap((await import("../../src/app/api/jobs/route")).POST);
  RETRY = wrap((await import("../../src/app/api/jobs/[id]/retry/route")).POST);
  ({ signSession } = await import("../../src/lib/session"));
  await (await import("../../src/lib/db")).connectDB();
  // Sessions are only valid for users that exist (lib/auth.ts), so the test users exist.
  await mongoose.connection.db!.collection("users").insertMany([{ _id: alice, email: `${alice}@test.test`, passwordHash: "x" }] as never);
  await db().collection("gazetteer").insertMany(GAZ as never);
  await db().collection("gazetteerMeta").insertOne({ _id: "gazetteer", files: { "cities1000.zip": { sha256: "abcdef0123456789ff" } } } as never);
});

beforeEach(async () => {
  await db().collection("scrapingJobs").deleteMany({});
  await asAlice();
});

after(async () => {
  await mongoose.disconnect();
  await mongod.stop();
});

test("an ambiguous name returns every match, biggest first", async () => {
  const r = await search("London");
  assert.equal(r.status, 200);
  assert.deepEqual(r.body.places.map((p: { id: string }) => p.id), ["gn:2643743", "gn:6058560"]);
  assert.equal(r.body.places[0].label, "London, England, United Kingdom");
  assert.match(r.body.places[0].detail, /City or town · population 8,961,989/);
  assert.match(r.body.attribution.text, /GeoNames \(CC BY 4\.0\)/);
});

test("country or region qualifiers narrow it down; regions and countries are found too", async () => {
  assert.deepEqual((await search("London, UK")).body.places.map((p: { id: string }) => p.id), ["gn:2643743"]);
  assert.deepEqual((await search("london, ontario")).body.places.map((p: { id: string }) => p.id), ["gn:6058560"]);
  assert.deepEqual((await search("England")).body.places.map((p: { id: string }) => p.id), ["admin1:GB.ENG"]);
  assert.deepEqual((await search("UK")).body.places.map((p: { id: string }) => p.id), ["country:GB"]);
  assert.deepEqual((await search("Londres")).body.places.map((p: { id: string }) => p.id), ["gn:2643743"]); // other language
});

test("a partial name falls back to names starting with it; nothing found is an empty list", async () => {
  assert.deepEqual((await search("Manch")).body.places.map((p: { id: string }) => p.id), ["gn:2643123"]);
  assert.deepEqual((await search("Atlantis")).body.places, []);
  assert.equal((await search("x")).status, 400);
  assert.deepEqual((await search(".*")).body.places, []); // regex characters are escaped, not executed
});

test("place search requires sign-in", async () => {
  cookieValue = undefined;
  assert.equal((await search("London")).status, 401);
});

test("a job stores a copy of the picked place (with the gazetteer version)", async () => {
  const r = await create({ location: "London, England, United Kingdom", placeId: "gn:2643743", categories: ["gym"] });
  assert.equal(r.status, 201);
  const job = await db().collection("scrapingJobs").findOne({ _id: new Types.ObjectId(r.body.job._id) });
  assert.equal(job!.location.place._id, "gn:2643743");
  assert.equal(job!.location.place.lat, 51.50853);
  assert.equal(job!.location.place.gazetteerVersion, "geonames#abcdef012345");
  assert.equal(job!.location.place.keys, undefined); // search keys aren't copied
  assert.deepEqual(job!.sources, ["osm"]);
});

test("a job needs a valid, existing place", async () => {
  assert.equal((await create({ location: "London", categories: ["gym"] })).status, 400); // no placeId
  assert.equal((await create({ location: "London", placeId: "gn:abc", categories: ["gym"] })).status, 400);
  const unknown = await create({ location: "London", placeId: "gn:999999", categories: ["gym"] });
  assert.equal(unknown.status, 400);
  assert.match(unknown.body.error.message, /pick it from the list/);
  assert.equal(await db().collection("scrapingJobs").countDocuments({}), 0);
});

test("retry keeps the picked place; a search from before place selection can't be retried", async () => {
  const withPlace = new Types.ObjectId();
  const legacy = new Types.ObjectId();
  const base = { ownerId: alice, categories: ["gym"], sources: ["osm"], status: "failed", createdAt: new Date(), updatedAt: new Date() };
  await db().collection("scrapingJobs").insertMany([
    { _id: withPlace, ...base, location: { query: "London", place: GAZ[2] } },
    { _id: legacy, ...base, location: { query: "London, UK" } },
  ] as never);
  const ok = await RETRY(new Request(`http://test/api/jobs/${withPlace}/retry`, { method: "POST" }), { params: Promise.resolve({ id: String(withPlace) }) });
  assert.equal(ok.status, 201);
  const created = await db().collection("scrapingJobs").findOne({ retryOf: withPlace });
  assert.equal(created!.location.place._id, "gn:2643743");
  const old = await RETRY(new Request(`http://test/api/jobs/${legacy}/retry`, { method: "POST" }), { params: Promise.resolve({ id: String(legacy) }) });
  assert.equal(old.status, 409);
  assert.match((await old.json()).error.message, /pick the place/);
});
