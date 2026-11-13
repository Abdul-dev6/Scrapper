// Restores a backup made by `npm run db:backup` into a SEPARATE database and verifies it.
//   npm run db:restore -- --backup=<backup dir> --target-uri=<mongodb uri> --target-db=<name>
// Safety: never restores over production. It refuses when
//   - the target database is the configured production database (same hosts AND same db name), or
//   - any target collection already contains documents (restore only into empty collections), or
//   - the backup manifest is not verified, or a file's sha256 doesn't match the manifest.
// After inserting, every restored document is re-read and compared (count, _id, sha256) with the backup.
import { readFileSync } from "node:fs";
import path from "node:path";
import { MongoClient } from "mongodb";
import mongoose from "mongoose";
import { canonical, sha, type Manifest } from "./backup-db";

const { EJSON } = mongoose.mongo.BSON;

function hosts(uri: string): string {
  const m = uri.match(/^mongodb(?:\+srv)?:\/\/(?:[^@/]*@)?([^/?]+)/i);
  return (m?.[1] ?? "").toLowerCase().split(",").sort().join(",");
}

export function isProductionTarget(targetUri: string, targetDb: string, prodUri?: string, prodDb?: string): boolean {
  if (!prodUri) return false;
  return hosts(targetUri) === hosts(prodUri) && targetDb === (prodDb || "leadscraper");
}

export type RestoreResult = Record<string, { restored: number; expected: number; mismatched: number; missing: number; verified: boolean }>;

export async function restoreBackup(
  backupDir: string,
  targetUri: string,
  targetDb: string,
  prod: { uri?: string; db?: string } = { uri: process.env.MONGODB_URI, db: process.env.MONGODB_DB }
): Promise<RestoreResult> {
  if (isProductionTarget(targetUri, targetDb, prod.uri, prod.db)) {
    throw new Error("Refusing to restore into the production database. Use a separate test database.");
  }
  const manifest = JSON.parse(readFileSync(path.join(backupDir, "manifest.json"), "utf8")) as Manifest;
  if (!manifest.verified) throw new Error("Backup manifest is not verified");

  const client = await MongoClient.connect(targetUri);
  try {
    const db = client.db(targetDb);
    for (const name of Object.keys(manifest.collections)) {
      if ((await db.collection(name).estimatedDocumentCount()) > 0) {
        throw new Error(`Target collection ${targetDb}.${name} is not empty; refusing to overwrite`);
      }
    }
    const out: RestoreResult = {};
    for (const [name, c] of Object.entries(manifest.collections)) {
      const text = readFileSync(path.join(backupDir, c.file), "utf8");
      if (sha(text) !== c.fileSha256) throw new Error(`${c.file} was changed since the backup (sha256 mismatch)`);
      const lines = text.split("\n").filter(Boolean);
      const docs = lines.map((l) => EJSON.parse(l, { relaxed: false }) as Record<string, unknown>);
      // Expected hash per _id, from the file itself (its integrity was just checked against the manifest).
      const expected = new Map(docs.map((d, i) => [String(d._id), sha(lines[i])]));
      for (const [id, h] of Object.entries(c.docSha256 ?? {})) {
        if (expected.get(id) !== h) throw new Error(`${c.file}: document ${id} differs from the manifest`);
      }
      for (let i = 0; i < docs.length; i += 1000) {
        await db.collection(name).insertMany(docs.slice(i, i + 1000), { ordered: true });
      }
      // Verify what is now in the database, independently of what we inserted.
      let mismatched = 0;
      const seen = new Set<string>();
      for await (const d of db.collection(name).find({}, { sort: { _id: 1 } })) {
        const id = String(d._id);
        seen.add(id);
        if (expected.get(id) !== sha(canonical(d))) mismatched++;
      }
      const missing = [...expected.keys()].filter((id) => !seen.has(id)).length;
      out[name] = {
        restored: seen.size, expected: c.documents, mismatched, missing,
        verified: seen.size === c.documents && mismatched === 0 && missing === 0,
      };
    }
    return out;
  } finally {
    await client.close();
  }
}

async function main() {
  const arg = (k: string) => process.argv.find((a) => a.startsWith(`--${k}=`))?.slice(k.length + 3);
  const backup = arg("backup");
  const uri = arg("target-uri");
  const dbName = arg("target-db");
  if (!backup || !uri || !dbName) throw new Error("Usage: --backup=<dir> --target-uri=<uri> --target-db=<name>");
  const result = await restoreBackup(path.resolve(backup), uri, dbName);
  for (const [name, r] of Object.entries(result)) {
    console.log(`${name}: restored ${r.restored}/${r.expected}, mismatched ${r.mismatched}, missing ${r.missing} -> ` +
      (r.verified ? "VERIFIED" : "FAILED"));
    if (!r.verified) process.exitCode = 1;
  }
}

if (require.main === module) {
  main().catch((err) => {
    console.error(err instanceof Error ? err.message : err);
    process.exit(1);
  });
}
