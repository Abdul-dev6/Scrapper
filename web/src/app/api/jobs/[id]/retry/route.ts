import { NextResponse } from "next/server";
import { getCurrentUser } from "@/lib/auth";
import { connectDB } from "@/lib/db";
import { jsonError, serverError } from "@/lib/http";
import { startJobLimited } from "@/lib/jobs";
import { objectIdSchema } from "@/lib/validators";
import { ScrapingJob } from "@/models";

type Ctx = { params: Promise<{ id: string }> };
const RETRYABLE = ["failed", "partial", "cancelled"];

/** The sources a retried job may use: the old ones minus Google, never empty. */
function retrySources(old: readonly string[] | null | undefined): string[] {
  const kept = (old ?? []).filter((s) => s !== "google");
  return kept.length ? [...new Set(kept)] : ["osm"];
}

/**
 * Starts a NEW job with the same search, so the old one stays in the history.
 * Re-running is cheap and safe: existing leads are updated (not duplicated), the location
 * lookup comes from the cache, and websites checked in the last 7 days are skipped.
 */
export async function POST(_req: Request, { params }: Ctx) {
  try {
    const user = await getCurrentUser();
    if (!user) return jsonError(401, "Sign in required");
    const id = objectIdSchema.safeParse((await params).id);
    if (!id.success) return jsonError(404, "Job not found");

    await connectDB();
    const old = await ScrapingJob.findOne({ _id: id.data, ownerId: user.id }).lean();
    if (!old?.location?.query) return jsonError(404, "Job not found");
    if (!RETRYABLE.includes(old.status ?? "")) {
      return jsonError(409, "Only failed, partial or cancelled jobs can be retried.");
    }
    // Searches from before place selection have no picked place: the worker can't build an area for
    // them (no public geocoder any more), so the user must start a new search and pick the place.
    if (!old.location.place) {
      return jsonError(409, "This search was created before place selection. Start a new search and pick the place from the list.");
    }
    // Google is never carried over by Retry (decision 2026-11-14): an old job's Google choice may
    // predate the current rules or quota. Google needs a NEW search where the user ticks it again.
    // The worker also refuses Google for any job with `retryOf` (defense in depth).
    const sources = retrySources(old.sources);
    const googleRemoved = (old.sources ?? []).includes("google");
    const { query, place } = old.location;
    const started = await startJobLimited(user.id, () =>
      ScrapingJob.create({
        ownerId: user.id,
        location: { query, place },
        categories: old.categories,
        sources,
        retryOf: old._id,
      }),
      place
    );
    if ("error" in started) return jsonError(429, started.error);
    const job = started.value;
    return NextResponse.json({ job: { _id: job._id, status: job.status, sources }, googleRemoved }, { status: 201 });
  } catch (err) {
    return serverError(err);
  }
}
