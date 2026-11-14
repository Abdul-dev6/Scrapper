import { NextResponse, type NextRequest } from "next/server";
import { z } from "zod";
import { getCurrentUser } from "@/lib/auth";
import { connectDB } from "@/lib/db";
import { jsonError, serverError, zodError } from "@/lib/http";
import { PLACE_ATTRIBUTION, searchPlaces } from "@/lib/places";

const querySchema = z.object({ q: z.string().trim().min(2, "Type at least 2 letters").max(120) });

/** GET /api/places?q=London -> candidate places (offline gazetteer; nothing is sent to an outside service). */
export async function GET(req: NextRequest) {
  try {
    const user = await getCurrentUser();
    if (!user) return jsonError(401, "Sign in required");
    const parsed = querySchema.safeParse({ q: req.nextUrl.searchParams.get("q") ?? "" });
    if (!parsed.success) return zodError(parsed.error);
    await connectDB();
    const places = await searchPlaces(parsed.data.q);
    return NextResponse.json({ places, attribution: PLACE_ATTRIBUTION }, { headers: { "Cache-Control": "no-store" } });
  } catch (err) {
    return serverError(err);
  }
}
