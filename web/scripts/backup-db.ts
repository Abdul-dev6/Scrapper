// Backs up collections to ../backups/<timestamp>/ (git-ignored) and VERIFIES the backup.
//   npm run db:backup                      -> backs up "leads" (what the contact migration changes)
//   npm run db:backup -- leads scrapingJobs
// Each collection is one canonical Extended-JSON-lines file (ObjectId/Date types survive; restore with
// `npm run db:restore`). Verification re-reads every file and compares document count, every _id and a
// sha256 of each document with the live database. manifest.json records the result; the migration
// script refuses to run without a verified manifest.
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import mongoose from "mongoose";
import type { Db } from "mongodb";

const { EJSON } = mongoose.mongo.BSON;
export const ALLOWED_COLLECTIONS = new Set(["leads", "scrapingJobs", "suppressionList", "googlePlaceRefs"]); // never users

export const canonical = (doc: unknown) => EJSON.stringify(doc, { relaxed: false });
export const sha = (s: string) => createHash("sha256").update(s).digest("hex");

export type CollectionManifest = {
  file: string; documents: number; liveCount: number; mismatched: number; missing: number; fileSha256: string;
  docSha256: Record<string, string>; verified: boolean;
};
export type Manifest = { createdAt: string; database: string; verified: boolean; collections: Record<string, CollectionManifest> };

export async function backupCollections(db: Db, names: string[], dir: string): Promise<Manifest> {
  for (const n of names) if (!ALLOWED_COLLECTIONS.has(n)) throw new Error(`Refusing to back up "${n}"`);
  mkdirSync(dir, { recursive: true });
  const manifest: Manifest = { createdAt: new Date().toISOString(), database: db.databaseName, verified: true, collections: {} };

  for (const name of names) {
    const coll = db.collection(name);
    const file = path.join(dir, `${name}.ejson.jsonl`);
    const lines: string[] = [];
    const hashes: Record<string, string> = {};
    for await (const doc of coll.find({}, { sort: { _id: 1 } })) {
      const line = canonical(doc);
      lines.push(line);
      hashes[String(doc._id)] = sha(line);
    }
    writeFileSync(file, lines.join("\n") + (lines.length ? "\n" : ""), "utf8");

    // Verify: the file must contain exactly what the database contains right now.
    const back = readFileSync(file, "utf8").split("\n").filter(Boolean);
    const parsed = back.map((l) => EJSON.parse(l, { relaxed: false }) as { _id: unknown });
    const fileIds = new Set(parsed.map((d) => String(d._id)));
    const liveCount = await coll.countDocuments();
    const mismatched = parsed.filter((d) => hashes[String(d._id)] !== sha(canonical(d))).length;
    const missing = Object.keys(hashes).filter((id) => !fileIds.has(id)).length;
    const verified = back.length === lines.length && back.length === liveCount && mismatched === 0 && missing === 0;
    manifest.collections[name] = {
      file: path.basename(file), documents: back.length, liveCount, mismatched, missing,
      fileSha256: sha(readFileSync(file, "utf8")), docSha256: hashes, verified,
    };
    if (!verified) manifest.verified = false;
  }
  writeFileSync(path.join(dir, "manifest.json"), JSON.stringify(manifest, null, 2));
  return manifest;
}

async function main() {
  const { connectDB } = await import("../src/lib/db");
  const names = process.argv.slice(2).length ? process.argv.slice(2) : ["leads"];
  await connectDB();
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const dir = path.resolve(__dirname, "..", "..", "backups", stamp);
  const m = await backupCollections(mongoose.connection.db!, names, dir);
  for (const [name, c] of Object.entries(m.collections)) {
    console.log(`${name}: ${c.documents} documents written, live ${c.liveCount}, mismatched ${c.mismatched}, ` +
      `missing ${c.missing} -> ${c.verified ? "VERIFIED" : "FAILED"}`);
  }
  console.log(`Backup ${m.verified ? "verified" : "NOT verified"}: ${dir}`);
  if (!m.verified) process.exitCode = 1;
  await mongoose.disconnect();
}

if (require.main === module) {
  main().catch(async (err) => {
    console.error(err);
    await mongoose.disconnect();
    process.exit(1);
  });
}
