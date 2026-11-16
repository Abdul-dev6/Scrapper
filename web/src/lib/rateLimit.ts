import { createHash } from "node:crypto";
import { LoginAttempt } from "@/models";

// Fixed-window counters in MongoDB (loginAttempts collection; its TTL index deletes old windows).
// Works on serverless: every instance sees the same counters.
//
// "Increase, then check" in ONE atomic update (security review M4): the old "read count, do the slow
// bcrypt work, then increase" let 100 parallel requests all read the same low count and pass.

export const counterKey = (...parts: string[]) => createHash("sha256").update(parts.join("|")).digest("hex");

/** Count one more hit in the window and return the new count (1 = the first hit in this window). */
export async function hit(key: string, windowMinutes: number): Promise<number> {
  const update = {
    $inc: { count: 1 },
    $setOnInsert: { expiresAt: new Date(Date.now() + windowMinutes * 60_000) },
  };
  for (let attempt = 0; ; attempt++) {
    try {
      const doc = await LoginAttempt.findOneAndUpdate({ key }, update, { upsert: true, new: true, projection: { count: 1 } }).lean();
      return doc?.count ?? 1;
    } catch (err) {
      // Two first hits at the same moment: one upsert wins, the other gets E11000. Retry = plain $inc.
      if ((err as { code?: number }).code === 11000 && attempt < 2) continue;
      throw err;
    }
  }
}

/** Give a hit back (e.g. a successful login shouldn't count against the network or the account). */
export async function release(key: string): Promise<void> {
  await LoginAttempt.updateOne({ key, count: { $gt: 0 } }, { $inc: { count: -1 } });
}

export async function clear(key: string): Promise<void> {
  await LoginAttempt.deleteOne({ key });
}

/** Read a positive integer setting, e.g. limitFromEnv("JOB_GLOBAL_DAILY_MAX", 300). */
export function limitFromEnv(name: string, fallback: number): number {
  const n = Number(process.env[name]);
  return Number.isInteger(n) && n > 0 ? n : fallback;
}
