// Phase 1 contact-status migration.
//   npm run db:migrate:contacts                                   -> DRY RUN: prints what would change
//   npm run db:migrate:contacts -- --apply --backup=<backup dir>  -> writes (needs a verified backup < 24 h old)
// Safe to run again: leads already at contactsVersion 2 are skipped. A lead that changes while the
// script runs (e.g. the worker updates it) is not overwritten; it is counted and picked up next run.
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import mongoose from "mongoose";
import type { AnyBulkWriteOperation, Document } from "mongodb";
import { CONTACTS_VERSION, migrateLeadContacts } from "../src/lib/contactMigration";
import { connectDB } from "../src/lib/db";

const BATCH = 500;
const MAX_BACKUP_AGE_MS = 24 * 3600 * 1000;

function checkBackup(dir: string | undefined, dbName: string) {
  if (!dir) throw new Error("--apply needs --backup=<dir> (run `npm run db:backup` first)");
  const file = path.join(path.resolve(dir), "manifest.json");
  if (!existsSync(file)) throw new Error(`No manifest.json in ${dir}`);
  const m = JSON.parse(readFileSync(file, "utf8"));
  const leads = m.collections?.leads;
  if (!m.verified || !leads?.verified) throw new Error("That backup is not verified");
  if (m.database !== dbName) throw new Error(`Backup is of database "${m.database}", not "${dbName}"`);
  if (Date.now() - Date.parse(m.createdAt) > MAX_BACKUP_AGE_MS) throw new Error("Backup is older than 24 hours; take a new one");
  console.log(`Using verified backup ${dir} (${leads.documents} leads, ${m.createdAt})`);
}

async function main() {
  const apply = process.argv.includes("--apply");
  const backupArg = process.argv.find((a) => a.startsWith("--backup="))?.slice("--backup=".length);
  await connectDB();
  const db = mongoose.connection.db!;
  if (apply) checkBackup(backupArg, db.databaseName);
  const leads = db.collection("leads");

  const filter = { contactsVersion: { $ne: CONTACTS_VERSION } };
  const total = await leads.countDocuments(filter);
  console.log(`${apply ? "APPLYING" : "DRY RUN"}: ${total} leads to migrate`);
  const transitions = new Map<string, number>();
  let written = 0;
  let conflicts = 0;
  let batch: AnyBulkWriteOperation<Document>[] = [];

  const flush = async () => {
    if (!apply || !batch.length) return (batch = []);
    const res = await leads.bulkWrite(batch, { ordered: false });
    written += res.modifiedCount;
    conflicts += batch.length - res.matchedCount; // changed by someone else meanwhile: next run
    batch = [];
  };

  for await (const doc of leads.find(filter, { projection: { emails: 1, phones: 1, whatsapp: 1, contactStatus: 1, verifiedAt: 1, updatedAt: 1 } })) {
    const { set, from, to } = migrateLeadContacts(doc as never);
    transitions.set(`${from} -> ${to}`, (transitions.get(`${from} -> ${to}`) ?? 0) + 1);
    // Only if the lead is unchanged since we read it (updatedAt), and not migrated meanwhile.
    batch.push({ updateOne: { filter: { _id: doc._id, updatedAt: doc.updatedAt, ...filter }, update: { $set: set } } });
    if (batch.length >= BATCH) await flush();
  }
  await flush();

  console.log("contactStatus changes:");
  for (const [k, v] of [...transitions].sort()) console.log(`  ${k}: ${v}`);
  if (apply) console.log(`Written: ${written}. Changed meanwhile (run again to finish): ${conflicts}.`);
  else console.log("Nothing was written. Re-run with --apply --backup=<dir> to migrate.");
  await mongoose.disconnect();
}

main().catch(async (err) => {
  console.error(err instanceof Error ? err.message : err);
  await mongoose.disconnect();
  process.exit(1);
});
