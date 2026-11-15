// Every model's indexes build on a real MongoDB (what `npm run db:indexes` does), including the
// Phase 2 collections. Run with: npm run test:integration
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { MongoMemoryServer } from "mongodb-memory-server";
import mongoose from "mongoose";

let mongod: MongoMemoryServer;

before(async () => {
  mongod = await MongoMemoryServer.create();
  process.env.MONGODB_URI = mongod.getUri();
  process.env.MONGODB_DB = "indexes_integration";
  process.env.AUTH_SECRET = "integration-test-secret-0123456789abcdef";
  await (await import("../../src/lib/db")).connectDB();
});

after(async () => {
  await mongoose.disconnect();
  await mongod.stop();
});

test("all indexes build, including osmExtracts and gazetteer", async () => {
  const models = await import("../../src/models");
  const names: Record<string, string[]> = {};
  for (const m of Object.values(models)) {
    if (typeof m !== "function" || !("syncIndexes" in m)) continue;
    const model = m as unknown as mongoose.Model<unknown>;
    await model.createCollection().catch(() => {});
    await model.syncIndexes();
    names[model.collection.name] = (await model.listIndexes()).map((i) => i.name as string);
  }
  assert.ok(names.osmExtracts.includes("refreshRequestedAt_1"));
  assert.ok(names.osmExtracts.includes("readers.lease_1"));
  assert.ok(names.osmExtracts.includes("refreshLease.until_1"));
  assert.ok(names.gazetteer.includes("keys_1_population_-1")); // same name the worker's loader uses
  assert.ok(names.leads.includes("ownerId_1_externalIds.osm_1")); // existing lead indexes unchanged
  assert.ok(names.leads.includes("ownerId_1_dedupeKey_1"));
  assert.ok(names.leads.includes("ownerId_1_externalIds.overture_1")); // one lead per Overture place per user
  assert.ok(names.leads.includes("ownerId_1_dataLane_1_discoveredAt_-1")); // the Source filter
  assert.ok(names.overtureTiles.includes("status_1_lastAccessAt_1"));
});
