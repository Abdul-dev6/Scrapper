import { NextResponse } from "next/server";
import { getCurrentUser } from "@/lib/auth";
import { connectDB } from "@/lib/db";
import { jsonError, serverError } from "@/lib/http";
import { removeSuppressedLeads, suppressionHash, type SuppressionType } from "@/lib/suppression";
import { objectIdSchema } from "@/lib/validators";
import { Lead, Suppression } from "@/models";

type Ctx = { params: Promise<{ id: string }> };

/**
 * "This business asked not to be contacted": put its emails, phones and website domain on
 * the opt-out list, then remove every lead that matches (this one and e.g. other branches
 * sharing the email). Future searches skip it automatically.
 */
export async function POST(_req: Request, { params }: Ctx) {
  try {
    const user = await getCurrentUser();
    if (!user) return jsonError(401, "Sign in required");
    const id = objectIdSchema.safeParse((await params).id);
    if (!id.success) return jsonError(404, "Lead not found");

    await connectDB();
    const lead = await Lead.findOne({ _id: id.data, ownerId: user.id }).lean();
    if (!lead) return jsonError(404, "Lead not found");

    const entries: [SuppressionType, string][] = [];
    for (const e of lead.emails ?? []) if (e.value) entries.push(["email", e.value]);
    for (const p of lead.phones ?? []) if (p.e164 || p.raw) entries.push(["phone", (p.e164 || p.raw)!]);
    for (const w of lead.whatsapp ?? []) if (w.e164) entries.push(["phone", w.e164]);
    if (lead.website?.domain) entries.push(["domain", lead.website.domain]);
    if (entries.length === 0) {
      // Nothing to match on next time: just delete it.
      await Lead.deleteOne({ _id: lead._id, ownerId: user.id });
      return NextResponse.json({ added: 0, removedLeads: 1 });
    }

    const reason = `Opt-out from lead "${lead.name}"`.slice(0, 500);
    const results = await Promise.all(
      entries.map(([type, value]) => {
        const valueHash = suppressionHash(type, value);
        return Suppression.updateOne(
          { ownerId: user.id, type, valueHash },
          { $setOnInsert: { ownerId: user.id, type, valueHash, reason } },
          { upsert: true } // already on the list -> no duplicate
        );
      })
    );
    const removedLeads = await removeSuppressedLeads(user.id);
    return NextResponse.json({ added: results.filter((r) => r.upsertedCount).length, removedLeads });
  } catch (err) {
    return serverError(err);
  }
}
