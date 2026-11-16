// Integration test of the REAL PATCH /api/leads/[id] handler against a real (throwaway, in-memory)
// MongoDB. Only `next/headers` is replaced, to hand the handler a session cookie; the session itself
// is a real signed token, checked by the real code.  Run with: npm run test:integration
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
let PATCH: (req: Request, ctx: { params: Promise<{ id: string }> }) => Promise<Response>;
let signSession: (p: { sub: string; email: string }) => Promise<string>;

const alice = new Types.ObjectId();
const mallory = new Types.ObjectId();
const leadId = new Types.ObjectId();

const leadDoc = () => ({
  _id: leadId,
  ownerId: alice,
  name: "Iron Fist",
  nameNormalized: "iron fist",
  emails: [{ value: "info@ironfist.test", kind: "generic", mxValid: true, sourceUrl: "https://ironfist.test/contact",
             sourceType: "website", checks: { syntaxValid: true, mx: "valid", manuallyVerified: false } }],
  phones: [{ e164: "+442079460000", raw: "020 7946 0000", sourceUrl: "https://ironfist.test/", sourceType: "website",
             checks: { formatValid: true, manuallyVerified: false } }],
  contactStatus: "checked",
  contactsVersion: 2,
  saved: false,
  status: "new",
  notes: "",
  discoveredAt: new Date("2026-11-01"),
  createdAt: new Date("2026-11-01"),
  updatedAt: new Date("2026-11-01"),
});

const leads = () => mongoose.connection.db!.collection("leads");
const raw = () => leads().findOne({ _id: leadId });

async function patch(as: Types.ObjectId | null, body: unknown, id: string = String(leadId)) {
  cookieValue = as ? await signSession({ sub: String(as), email: `${as}@test.test` }) : undefined;
  const res = await PATCH(new Request(`http://test/api/leads/${id}`, { method: "PATCH", body: JSON.stringify(body) }), {
    params: Promise.resolve({ id }),
  });
  return { status: res.status, body: await res.json() };
}

before(async () => {
  mongod = await MongoMemoryServer.create();
  process.env.MONGODB_URI = mongod.getUri();
  process.env.MONGODB_DB = "patch_integration";
  process.env.AUTH_SECRET = "integration-test-secret-0123456789abcdef";
  ({ PATCH } = await import("../../src/app/api/leads/[id]/route"));
  ({ signSession } = await import("../../src/lib/session"));
  const { connectDB } = await import("../../src/lib/db");
  await connectDB();
  // Sessions are only valid for users that exist (lib/auth.ts), so the test users exist.
  await mongoose.connection.db!.collection("users").insertMany([{ _id: alice, email: `${alice}@test.test`, passwordHash: "x" }, { _id: mallory, email: `${mallory}@test.test`, passwordHash: "x" }] as never);
});

beforeEach(async () => {
  await leads().deleteMany({});
  await leads().insertOne(leadDoc());
});

after(async () => {
  await mongoose.disconnect();
  await mongod.stop();
});

test("owner verifies their own email: stored per contact, status becomes 'verified'", async () => {
  const r = await patch(alice, { verifyContact: { kind: "email", value: "INFO@ironfist.test", verified: true } });
  assert.equal(r.status, 200);
  const doc = await raw();
  assert.equal(doc!.emails[0].checks.manuallyVerified, true);
  assert.equal(String(doc!.emails[0].checks.manuallyVerifiedBy), String(alice));
  assert.ok(doc!.emails[0].checks.manuallyVerifiedAt instanceof Date);
  assert.equal(doc!.emails[0].checks.mx, "valid"); // automatic results untouched
  assert.equal(doc!.phones[0].checks.manuallyVerified, false); // only the chosen contact
  assert.equal(doc!.contactStatus, "verified");
  assert.equal(r.body.lead.contactStatus, "verified");
});

test("owner verifies a phone written differently, then removes the verification", async () => {
  assert.equal((await patch(alice, { verifyContact: { kind: "phone", value: "+44 20 7946 0000", verified: true } })).status, 200);
  assert.equal((await raw())!.phones[0].checks.manuallyVerified, true);
  assert.equal((await patch(alice, { verifyContact: { kind: "phone", value: "+442079460000", verified: false } })).status, 200);
  const doc = await raw();
  assert.equal(doc!.phones[0].checks.manuallyVerified, false);
  assert.equal(doc!.phones[0].checks.manuallyVerifiedBy, undefined);
  assert.equal(doc!.contactStatus, "checked"); // back to the automatic result
});

test("another user cannot verify or change someone else's contacts (looks like 'not found')", async () => {
  const before = await raw();
  for (const body of [
    { verifyContact: { kind: "email", value: "info@ironfist.test", verified: true } },
    { saved: true, notes: "hijack" },
  ]) {
    const r = await patch(mallory, body);
    assert.equal(r.status, 404);
  }
  assert.deepEqual(await raw(), before);
});

test("no session: 401 and nothing changes", async () => {
  const before = await raw();
  const r = await patch(null, { verifyContact: { kind: "email", value: "info@ironfist.test", verified: true } });
  assert.equal(r.status, 401);
  assert.deepEqual(await raw(), before);
});

test("invalid contact values and unknown fields are rejected without changes", async () => {
  const before = await raw();
  const cases: [unknown, number][] = [
    [{ verifyContact: { kind: "email", value: "nobody@ironfist.test", verified: true } }, 404], // not on this lead
    [{ verifyContact: { kind: "email", value: "x", verified: true } }, 400], // too short
    [{ verifyContact: { kind: "fax", value: "123456", verified: true } }, 400], // unknown kind
    [{ verifyContact: { kind: "email", value: "info@ironfist.test", verified: "yes" } }, 400], // not a boolean
    [{ verifyContact: { kind: "email", value: "info@ironfist.test", verified: true, by: "x" } }, 400], // extra key
    [{ ownerId: String(mallory) }, 400], // unknown field
    [{ emails: [] }, 400], // raw contact arrays can't be written
    [{ contactStatus: "verified" }, 400], // status can't be set directly
    [{}, 400], // nothing to update
  ];
  for (const [body, status] of cases) {
    const r = await patch(alice, body);
    assert.equal(r.status, status, JSON.stringify(body));
  }
  assert.equal((await patch(alice, { saved: true }, "not-an-id")).status, 404);
  assert.deepEqual(await raw(), before);
});

test("a concurrent change makes the request fail safely (409) instead of overwriting", async () => {
  const { Lead } = await import("../../src/models");
  const realFindOne = Lead.findOne.bind(Lead);
  let first = true;
  // Between the handler's read and write, the worker updates the lead (new updatedAt).
  const spy = mock.method(Lead, "findOne", (...args: Parameters<typeof Lead.findOne>) => {
    const q = realFindOne(...args);
    if (first) {
      first = false;
      return { ...q, select: () => ({ lean: async () => {
        const doc = await realFindOne(...args).lean();
        await leads().updateOne({ _id: leadId }, { $set: { updatedAt: new Date(), notes: "worker was here" } });
        return doc;
      } }) } as never;
    }
    return q;
  });
  const r = await patch(alice, { verifyContact: { kind: "email", value: "info@ironfist.test", verified: true } });
  spy.mock.restore();
  assert.equal(r.status, 409);
  const doc = await raw();
  assert.equal(doc!.emails[0].checks.manuallyVerified, false);
  assert.equal(doc!.notes, "worker was here"); // the other write survived
});
