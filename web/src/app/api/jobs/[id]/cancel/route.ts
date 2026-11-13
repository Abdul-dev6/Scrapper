import { NextResponse } from "next/server";
import { getCurrentUser } from "@/lib/auth";
import { connectDB } from "@/lib/db";
import { jsonError, serverError } from "@/lib/http";
import { objectIdSchema } from "@/lib/validators";
import { ScrapingJob } from "@/models";

type Ctx = { params: Promise<{ id: string }> };

/**
 * Queued job  -> cancelled right away (no worker has it yet).
 * Running job -> cancelRequested; the worker sees it at its next progress report (a few seconds),
 *                stops, and marks the job "cancelled". Leads found so far are kept.
 * Each step is one atomic update, so it is safe even if the worker claims the job at the same moment.
 */
export async function POST(_req: Request, { params }: Ctx) {
  try {
    const user = await getCurrentUser();
    if (!user) return jsonError(401, "Sign in required");
    const id = objectIdSchema.safeParse((await params).id);
    if (!id.success) return jsonError(404, "Job not found");

    await connectDB();
    const mine = { _id: id.data, ownerId: user.id };
    const now = new Date();

    const queued = await ScrapingJob.updateOne(
      { ...mine, status: "queued" },
      { $set: { status: "cancelled", cancelRequested: true, finishedAt: now, "progress.stage": "Cancelled before it started" } }
    );
    if (queued.modifiedCount) return NextResponse.json({ status: "cancelled" });

    const running = await ScrapingJob.updateOne(
      { ...mine, status: "running" },
      { $set: { cancelRequested: true, "progress.stage": "Cancelling…" } }
    );
    if (running.matchedCount) return NextResponse.json({ status: "cancelling" });

    const exists = await ScrapingJob.exists(mine);
    if (!exists) return jsonError(404, "Job not found");
    return jsonError(409, "This job has already finished.");
  } catch (err) {
    return serverError(err);
  }
}
