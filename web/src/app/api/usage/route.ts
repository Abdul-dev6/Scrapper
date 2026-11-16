import { NextResponse } from "next/server";
import { getCurrentUser } from "@/lib/auth";
import { connectDB } from "@/lib/db";
import { googleUsageStatus } from "@/lib/googleUsage";
import { jsonError, serverError } from "@/lib/http";

/** ADMIN_EMAILS (web env): comma-separated addresses that may see the Google cost figures. */
function isAdmin(email: string): boolean {
  const admins = (process.env.ADMIN_EMAILS ?? "").split(",").map((e) => e.trim().toLowerCase()).filter(Boolean);
  return admins.includes(email.toLowerCase());
}

/**
 * GET /api/usage: this month's shared Google Places quota (read-only).
 * Everyone sees how much of the shared quota is left (it decides whether their search uses Google).
 * The money and error figures are the owner's business only (security review L5): admins only.
 */
export async function GET() {
  try {
    const user = await getCurrentUser();
    if (!user) return jsonError(401, "Sign in required");
    await connectDB();
    const status = await googleUsageStatus();
    const google = isAdmin(user.email)
      ? status
      : { ...status, estimatedCostUsd: 0, failed: { textSearchPro: 0, placeDetailsEssentials: 0 }, redacted: true };
    return NextResponse.json({ google }, { headers: { "Cache-Control": "no-store" } });
  } catch (err) {
    return serverError(err);
  }
}
