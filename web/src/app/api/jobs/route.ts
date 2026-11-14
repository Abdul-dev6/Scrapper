import { NextResponse, type NextRequest } from "next/server";
import { getCurrentUser } from "@/lib/auth";
import { connectDB } from "@/lib/db";
import { jsonError, serverError, zodError, readJsonBody } from "@/lib/http";
import { createJobSchema } from "@/lib/validators";
import { JOB_LIST_FIELDS, jobLimitError } from "@/lib/jobs";
import { placeForJob } from "@/lib/places";
import { ScrapingJob } from "@/models";

export async function GET(req: NextRequest) {
  try {
    // Checked here too, not only in middleware: defense in depth.
    const user = await getCurrentUser();
    if (!user) return jsonError(401, "Sign in required");

    const limit = Math.min(Math.max(Number(req.nextUrl.searchParams.get("limit")) || 20, 1), 100);
    await connectDB();
    const jobs = await ScrapingJob.find({ ownerId: user.id })
      .sort({ createdAt: -1 })
      .limit(limit)
      .select(JOB_LIST_FIELDS) // lockedAt is included: the UI warns when the worker stops reporting
      .lean();
    return NextResponse.json({ jobs });
  } catch (err) {
    return serverError(err);
  }
}

export async function POST(req: NextRequest) {
  try {
    const user = await getCurrentUser();
    if (!user) return jsonError(401, "Sign in required");

    const body = await readJsonBody(req); // size-capped before zod (see lib/http.ts)
    if (!body.ok) return body.response;
    const parsed = createJobSchema.safeParse(body.data);
    if (!parsed.success) return zodError(parsed.error);
    const { location, placeId, categories, sources } = parsed.data;

    await connectDB();
    const limit = await jobLimitError(user.id);
    if (limit) return jsonError(429, limit);
    // The exact place the user picked; the worker builds the search area from this copy.
    const place = await placeForJob(placeId);
    if (!place) return jsonError(400, "That place wasn't found. Search again and pick it from the list.");

    const job = await ScrapingJob.create({
      ownerId: user.id,
      location: { query: location, place },
      categories: [...new Set(categories)],
      sources: [...new Set(sources)],
    });
    return NextResponse.json({ job: { _id: job._id, status: job.status } }, { status: 201 });
  } catch (err) {
    return serverError(err);
  }
}
