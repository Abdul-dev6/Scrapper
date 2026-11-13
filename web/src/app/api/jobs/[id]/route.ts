import { NextResponse } from "next/server";
import { getCurrentUser } from "@/lib/auth";
import { connectDB } from "@/lib/db";
import { jsonError, serverError } from "@/lib/http";
import { objectIdSchema } from "@/lib/validators";
import { GooglePlaceRef, ScrapingJob } from "@/models";

type Ctx = { params: Promise<{ id: string }> };
const GOOGLE_ONLY_LIMIT = 200;

/** One job with its error log (newest first), for the "Details" panel. */
export async function GET(_req: Request, { params }: Ctx) {
  try {
    const user = await getCurrentUser();
    if (!user) return jsonError(401, "Sign in required");
    const id = objectIdSchema.safeParse((await params).id);
    if (!id.success) return jsonError(404, "Job not found");

    await connectDB();
    // ownerId in the filter: another user's job id simply "doesn't exist" for you.
    const job = await ScrapingJob.findOne({ _id: id.data, ownerId: user.id }).select("-workerId -ownerId -checkpoint").lean();
    if (!job) return jsonError(404, "Job not found");
    const errorLog = [...(job.errorLog ?? [])].reverse().slice(0, 50);
    // Places Google returned for this job. Only the place ID is stored (Google allows that) and it is
    // never linked to a lead; the browser turns it into a Google Maps link. ("unmatched" = older refs.)
    const refs = await GooglePlaceRef.find({ ownerId: user.id, jobIds: id.data, status: { $in: ["seen", "unmatched"] } })
      .select("placeId -_id")
      .limit(GOOGLE_ONLY_LIMIT)
      .lean();
    return NextResponse.json({ job: { ...job, errorLog }, googleOnly: refs.map((r) => r.placeId) });
  } catch (err) {
    return serverError(err);
  }
}
