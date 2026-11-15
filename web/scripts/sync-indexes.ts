// Builds/updates all MongoDB indexes. Run once after setup and after any schema index change:
//   npm run db:indexes
import mongoose from "mongoose";
import { connectDB } from "../src/lib/db";
import {
  User, LoginAttempt, ScrapingJob, Lead, Suppression, GeocodeCache, ApiUsage, ApiUsageLog, GooglePlaceRef, GoogleCoverage,
  Gazetteer, GazetteerMeta, OsmExtract,
} from "../src/models";

async function main() {
  await connectDB();
  for (const m of [
    User, LoginAttempt, ScrapingJob, Lead, Suppression, GeocodeCache, ApiUsage, ApiUsageLog, GooglePlaceRef, GoogleCoverage,
    Gazetteer, GazetteerMeta, OsmExtract,
  ] as const) {
    await m.createCollection().catch(() => {}); // already exists → fine
    const dropped = await m.syncIndexes();
    const names = (await m.listIndexes()).map((i) => i.name);
    console.log(`${m.collection.name}: ${names.length} indexes${dropped.length ? `, removed old: ${dropped.join(", ")}` : ""}`);
  }
  await mongoose.disconnect();
}

main().catch(async (err) => {
  console.error(err);
  await mongoose.disconnect();
  process.exit(1);
});
