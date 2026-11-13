import { NextResponse } from "next/server";
import { getCurrentUser } from "@/lib/auth";
import { connectDB } from "@/lib/db";
import { jsonError, serverError } from "@/lib/http";
import { Lead, ScrapingJob } from "@/models";

/** Options for the filter dropdowns: your countries (with counts) and your recent searches. */
export async function GET() {
  try {
    const user = await getCurrentUser();
    if (!user) return jsonError(401, "Sign in required");
    await connectDB();
    const [countries, jobs] = await Promise.all([
      Lead.aggregate<{ _id: string; count: number }>([
        { $match: { ownerId: user.id, "address.countryCode": { $type: "string" } } },
        { $group: { _id: "$address.countryCode", count: { $sum: 1 } } },
        { $sort: { count: -1 } },
        { $limit: 250 },
      ]),
      ScrapingJob.find({ ownerId: user.id, status: { $in: ["completed", "partial", "cancelled", "failed"] } })
        .sort({ createdAt: -1 })
        .limit(50)
        .select("location.query location.displayName createdAt stats.discovered")
        .lean(),
    ]);
    return NextResponse.json({
      countries: countries.map((c) => ({ code: c._id, count: c.count })),
      jobs: jobs.map((j) => ({
        _id: j._id,
        label: j.location?.query ?? "",
        createdAt: j.createdAt,
        found: j.stats?.discovered ?? 0,
      })),
    });
  } catch (err) {
    return serverError(err);
  }
}
