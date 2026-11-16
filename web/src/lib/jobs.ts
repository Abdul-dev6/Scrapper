import { randomUUID } from "node:crypto";
import type { Types } from "mongoose";
import { JobStartLock, ScrapingJob } from "@/models";
import { limitFromEnv } from "./rateLimit";

export const MAX_OPEN_JOBS = 3; // queued + running at the same time
export const MAX_JOBS_PER_HOUR = 10;
const LOCK_SECONDS = 15; // far longer than "count + insert" takes; frees the lock if a request dies inside
const LOCK_WAIT_MS = 3000; // a second click waits this long for the first request, then gets a friendly 429

/**
 * Abuse and cost caps (security review M3, decision 2026-11-16: open sign-up stays, with caps).
 * Per user: jobs open at once, per hour, per 24 h, and whole-country searches per 24 h (the expensive
 * ones: GBs of map data and thousands of websites). In total, over ALL users: jobs open at once and per
 * 24 h, so many accounts (sign-up is open) still can't overload the one worker or the Railway bill.
 * All overridable with environment variables (see web/.env.example).
 */
export const jobCaps = () => ({
  userOpen: MAX_OPEN_JOBS,
  userHourly: MAX_JOBS_PER_HOUR,
  userDaily: limitFromEnv("JOB_USER_DAILY_MAX", 30),
  userDailyCountry: limitFromEnv("JOB_USER_DAILY_COUNTRY_MAX", 3),
  globalOpen: limitFromEnv("JOB_GLOBAL_OPEN_MAX", 20),
  globalDaily: limitFromEnv("JOB_GLOBAL_DAILY_MAX", 300),
});

/** Returns a friendly message if the user may not start another job right now, else null. */
export async function jobLimitError(ownerId: Types.ObjectId, place?: { kind?: unknown } | null): Promise<string | null> {
  const caps = jobCaps();
  const now = Date.now();
  const hour = new Date(now - 3_600_000);
  const day = new Date(now - 86_400_000);
  const country = place?.kind === "country";
  const open = { status: { $in: ["queued", "running"] } };
  const [userOpen, userHour, userDay, userCountry, allOpen, allDay] = await Promise.all([
    ScrapingJob.countDocuments({ ownerId, ...open }),
    ScrapingJob.countDocuments({ ownerId, createdAt: { $gte: hour } }),
    ScrapingJob.countDocuments({ ownerId, createdAt: { $gte: day } }),
    country ? ScrapingJob.countDocuments({ ownerId, createdAt: { $gte: day }, "location.place.kind": "country" }) : 0,
    ScrapingJob.countDocuments(open),
    ScrapingJob.countDocuments({ createdAt: { $gte: day } }),
  ]);
  if (userOpen >= caps.userOpen) return `You already have ${userOpen} jobs waiting or running. Wait for one to finish.`;
  if (userHour >= caps.userHourly) return `Limit is ${caps.userHourly} searches per hour. Try again later.`;
  if (userDay >= caps.userDaily) return `Limit is ${caps.userDaily} searches per 24 hours. Try again tomorrow.`;
  if (country && userCountry >= caps.userDailyCountry) {
    return `Limit is ${caps.userDailyCountry} whole-country searches per 24 hours. Search a region or city, or try again tomorrow.`;
  }
  if (allOpen >= caps.globalOpen) return "The search service is busy right now. Try again in a few minutes.";
  if (allDay >= caps.globalDaily) return "The search service has reached today's limit. Try again tomorrow.";
  return null;
}

async function acquire(ownerId: Types.ObjectId, token: string): Promise<boolean> {
  const now = new Date();
  const until = new Date(now.getTime() + LOCK_SECONDS * 1000);
  try {
    // Takes a free or expired lock; if another request holds it, the filter doesn't match and the
    // upsert's insert hits the unique _id: E11000 = busy. Atomic on the server, across all instances.
    await JobStartLock.updateOne(
      { _id: ownerId, until: { $lt: now } },
      { $set: { token, until, expiresAt: new Date(until.getTime() + 3_600_000) } },
      { upsert: true }
    );
    return true;
  } catch (err) {
    if ((err as { code?: number }).code === 11000) return false;
    throw err;
  }
}

/**
 * Check the limits and create the job as ONE step per user: concurrent "Start search"/"Retry" requests
 * (double click, two tabs, a script) run one after another, so they can't all pass the same count.
 * Returns { error } (429 message) or { value } (whatever `create` returned).
 */
export async function startJobLimited<T>(
  ownerId: Types.ObjectId,
  create: () => Promise<T>,
  place?: { kind?: unknown } | null
): Promise<{ error: string } | { value: T }> {
  const token = randomUUID();
  const deadline = Date.now() + LOCK_WAIT_MS;
  while (!(await acquire(ownerId, token))) {
    if (Date.now() > deadline) return { error: "Another search is being started right now. Try again in a moment." };
    await new Promise((r) => setTimeout(r, 50 + Math.random() * 100));
  }
  try {
    const limit = await jobLimitError(ownerId, place);
    if (limit) return { error: limit };
    return { value: await create() };
  } finally {
    await JobStartLock.deleteOne({ _id: ownerId, token }).catch(() => {}); // expires by itself if this fails
  }
}

/** Fields the browser may see. errorLog is only sent by the single-job endpoint. */
export const JOB_LIST_FIELDS = "-errorLog -workerId -ownerId -checkpoint -lease"; // checkpoint/lease are worker-internal; checkpoint can be large
