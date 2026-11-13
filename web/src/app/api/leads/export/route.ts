import { type NextRequest } from "next/server";
import { getCurrentUser } from "@/lib/auth";
import { connectDB } from "@/lib/db";
import { jsonError, serverError, zodError } from "@/lib/http";
import {
  EXPORT_COLUMNS, MAX_EXPORT_ROWS, SORTS, buildWorkbook, exportRow, leadFilter, parseLeadQuery, toCsv, type ExportLead,
} from "@/lib/leads";
import { isSuppressed, loadHashes } from "@/lib/suppression";
import { Lead } from "@/models";

export const runtime = "nodejs"; // exceljs and node:crypto need Node, not the Edge runtime

/** GET /api/leads/export?format=csv|xlsx&<same filters as the table> */
export async function GET(req: NextRequest) {
  try {
    const user = await getCurrentUser();
    if (!user) return jsonError(401, "Sign in required");
    const params = new URLSearchParams(req.nextUrl.searchParams);
    const format = params.get("format") === "xlsx" ? "xlsx" : "csv";
    params.delete("format");
    params.delete("page");
    const parsed = parseLeadQuery(params);
    if (!parsed.success) return zodError(parsed.error);

    await connectDB();
    const filter = leadFilter(user.id, parsed.data);
    const total = await Lead.countDocuments(filter);
    if (total > MAX_EXPORT_ROWS) {
      return jsonError(413, `That's ${total.toLocaleString()} leads; the limit is ${MAX_EXPORT_ROWS.toLocaleString()} per file. Add a filter (e.g. a country).`);
    }

    // The opt-out list is checked again here (not only when saving): it may have changed since.
    const hashes = await loadHashes(user.id);
    const leads = await Lead.find(filter).sort(SORTS[parsed.data.sort]).select("-ownerId -jobIds -sources").lean();
    const rows = (leads as unknown as ExportLead[]).filter((l) => !isSuppressed(l, hashes)).map(exportRow);

    const stamp = new Date().toISOString().slice(0, 10);
    const name = `leads-${stamp}.${format}`;
    let body: BodyInit;
    let type: string;
    if (format === "csv") {
      body = toCsv([[...EXPORT_COLUMNS], ...rows]);
      type = "text/csv; charset=utf-8";
    } else {
      body = await buildWorkbook(rows); // plain strings (never formulas) + an attribution sheet
      type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
    }
    return new Response(body, {
      headers: {
        "Content-Type": type,
        "Content-Disposition": `attachment; filename="${name}"`,
        "Cache-Control": "no-store",
        "X-Export-Rows": String(rows.length),
        "X-Export-Skipped-Opt-Out": String(leads.length - rows.length),
      },
    });
  } catch (err) {
    return serverError(err);
  }
}
