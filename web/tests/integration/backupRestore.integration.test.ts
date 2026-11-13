// Backup -> restore round trip on a throwaway in-memory MongoDB (never production).
// Run with: npm run test:integration
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { after, before, test } from "node:test";
import { MongoClient, ObjectId } from "mongodb";
import { MongoMemoryServer } from "mongodb-memory-server";
import { backupCollections, canonical } from "../../scripts/backup-db";
import { isProductionTarget, restoreBackup } from "../../scripts/restore-db";

let mongod: MongoMemoryServer;
let client: MongoClient;
const tmp = mkdtempSync(path.join(tmpdir(), "lf-backup-test-"));

const sample = () =>
  Array.from({ length: 1234 }, (_, i) => ({
    _id: new ObjectId(),
    ownerId: new ObjectId(),
    name: `Gym ${i} — Café “Ünïcode” 🥊`,
    geo: { type: "Point", coordinates: [-0.1 + i / 1e4, 51.5] },
    emails: [{ value: `info${i}@gym.test`, mxValid: i % 2 === 0, checks: { syntaxValid: true, mx: "valid", checkedAt: new Date(1_700_000_000_000 + i) } }],
    confidence: 0.35,
    count: i,
    big: 2 ** 40 + i, // int64 range
    discoveredAt: new Date("2026-11-01T00:00:00Z"),
  }));

before(async () => {
  mongod = await MongoMemoryServer.create();
  client = await MongoClient.connect(mongod.getUri());
  await client.db("source").collection("leads").insertMany(sample());
});

after(async () => {
  await client.close();
  await mongod.stop();
  rmSync(tmp, { recursive: true, force: true });
});

test("backup is verified, and restoring it into a separate database gives identical documents", async () => {
  const dir = path.join(tmp, "b1");
  const manifest = await backupCollections(client.db("source"), ["leads"], dir);
  assert.equal(manifest.verified, true);
  assert.equal(manifest.collections.leads.documents, 1234);

  const result = await restoreBackup(dir, mongod.getUri(), "restored", { uri: undefined });
  assert.deepEqual(result.leads, { restored: 1234, expected: 1234, mismatched: 0, missing: 0, verified: true });

  const a = await client.db("source").collection("leads").find({}, { sort: { _id: 1 } }).toArray();
  const b = await client.db("restored").collection("leads").find({}, { sort: { _id: 1 } }).toArray();
  assert.deepEqual(b.map(canonical), a.map(canonical)); // types (ObjectId, Date, int64, unicode) preserved
});

test("restore refuses to write into a non-empty collection", async () => {
  const dir = path.join(tmp, "b2");
  await backupCollections(client.db("source"), ["leads"], dir);
  await assert.rejects(restoreBackup(dir, mongod.getUri(), "source", { uri: undefined }), /not empty/);
});

test("restore refuses the production database", async () => {
  const prod = "mongodb://user:pw@host-00:27017,host-01:27017/?tls=true";
  assert.equal(isProductionTarget("mongodb://other:pw@host-01:27017,host-00:27017/", "leadscraper", prod, "leadscraper"), true);
  assert.equal(isProductionTarget(prod, "leadscraper_restore_test", prod, "leadscraper"), false);
  await assert.rejects(restoreBackup(path.join(tmp, "b2"), prod, "leadscraper", { uri: prod, db: "leadscraper" }), /production/);
});

test("a tampered backup file is refused", async () => {
  const dir = path.join(tmp, "b3");
  await backupCollections(client.db("source"), ["leads"], dir);
  const file = path.join(dir, "leads.ejson.jsonl");
  writeFileSync(file, readFileSync(file, "utf8").replace("Gym 7 ", "Gym 8 "));
  await assert.rejects(restoreBackup(dir, mongod.getUri(), "tampered", { uri: undefined }), /sha256 mismatch/);
});
