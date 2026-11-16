import { type NextRequest } from "next/server";
import { getCurrentUser } from "@/lib/auth";
import { connectDB } from "@/lib/db";
import { jsonError, serverError, zodError } from "@/lib/http";
import {
  EXPORT_COLUMNS, EXPORT_FIELDS, MAX_EXPORT_ROWS, SORTS, buildWorkbook, csvDocument, csvLine, exportRow, leadFilter,
  parseLeadQuery, type ExportLead,
} from "@/lib/leads";
import { counterKey, hit, limitFromEnv } from "@/lib/rateLimit";
import { isSuppressed, loadHashes } from "@/lib/suppression";
import { Lead } from "@/models";

export const runtime = "nodejs"; // exceljs and node:crypto need Node, not the Edge runtime
// Measured locally on 44,756 leads (the 50,000 cap): CSV 3.8 s, XLSX 18.4 s. The platform default may be shorter.
export const maxDuration = 60;

/** GET /api/leads/export?format=csv|xlsx&<same filters as the table> */
export async function GET(req: NextRequest) {
  try {
    const user = await getCurrentUser();
    if (!user) return jsonError(401, "Sign in required");
    await connectDB();
    // Each big export costs up to ~20 s of server time: limited per user (security review L7).
    const exportsThisHour = await hit(counterKey("export", String(user.id)), 60);
    const maxExports = limitFromEnv("EXPORT_HOURLY_MAX", 10);
    if (exportsThisHour > maxExports) return jsonError(429, `Limit is ${maxExports} exports per hour. Try again later.`);
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
    // Leads are read through a cursor with only the exported fields (see EXPORT_FIELDS): memory stays
    // proportional to the output, not to the full documents.
    const hashes = await loadHashes(user.id);
    let skipped = 0;
    const cursor = Lead.find(filter).sort(SORTS[parsed.data.sort]).select(EXPORT_FIELDS).lean().cursor();
    async function* rows(): AsyncGenerator<string[]> {
      for await (const l of cursor) {
        if (isSuppressed(l as unknown as ExportLead, hashes)) skipped += 1;
        else yield exportRow(l as unknown as ExportLead);
      }
    }

    const stamp = new Date().toISOString().slice(0, 10);
    const name = `leads-${stamp}.${format}`;
    let body: BodyInit;
    let type: string;
    let count = 0;
    const counted = async function* () {
      for await (const r of rows()) {
        count += 1;
        yield r;
      }
    };
    if (format === "csv") {
      const lines: string[] = [];
      for await (const r of counted()) lines.push(csvLine(r));
      body = csvDocument(EXPORT_COLUMNS, lines);
      type = "text/csv; charset=utf-8";
    } else {
      body = await buildWorkbook(counted()); // plain strings (never formulas) + an attribution sheet
      type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
    }
    return new Response(body, {
      headers: {
        "Content-Type": type,
        "Content-Disposition": `attachment; filename="${name}"`,
        "Cache-Control": "no-store",
        "X-Export-Rows": String(count),
        "X-Export-Skipped-Opt-Out": String(skipped),
      },
    });
  } catch (err) {
    return serverError(err);
  }
}
