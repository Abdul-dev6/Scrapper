import { NextResponse, type NextRequest } from "next/server";
import { getCurrentUser } from "@/lib/auth";
import { connectDB } from "@/lib/db";
import { jsonError, serverError, zodError } from "@/lib/http";
import { LIST_FIELDS, PAGE_SIZE, SORTS, leadFilter, parseLeadQuery } from "@/lib/leads";
import { Lead } from "@/models";

/** GET /api/leads?q=&country=&city=&category=&hasEmail=1&...&sort=newest&page=1 */
export async function GET(req: NextRequest) {
  try {
    const user = await getCurrentUser();
    if (!user) return jsonError(401, "Sign in required");
    const parsed = parseLeadQuery(req.nextUrl.searchParams);
    if (!parsed.success) return zodError(parsed.error);
    const q = parsed.data;

    await connectDB();
    const filter = leadFilter(user.id, q);
    const [leads, total] = await Promise.all([
      Lead.find(filter)
        .sort(SORTS[q.sort])
        .skip((q.page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
        .select(LIST_FIELDS)
        .lean(),
      Lead.countDocuments(filter),
    ]);
    return NextResponse.json({ leads, total, page: q.page, pageSize: PAGE_SIZE });
  } catch (err) {
    return serverError(err);
  }
}
