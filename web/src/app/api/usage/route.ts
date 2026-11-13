import { NextResponse } from "next/server";
import { getCurrentUser } from "@/lib/auth";
import { connectDB } from "@/lib/db";
import { googleUsageStatus } from "@/lib/googleUsage";
import { jsonError, serverError } from "@/lib/http";

/** GET /api/usage: this month's Google Places usage against the internal limits (read-only). */
export async function GET() {
  try {
    const user = await getCurrentUser();
    if (!user) return jsonError(401, "Sign in required");
    await connectDB();
    return NextResponse.json({ google: await googleUsageStatus() }, { headers: { "Cache-Control": "no-store" } });
  } catch (err) {
    return serverError(err);
  }
}
