import { NextResponse } from "next/server";
import { getCurrentUser } from "@/lib/auth";
import { connectDB } from "@/lib/db";
import { jsonError, serverError, zodError, readJsonBody } from "@/lib/http";
import { leadPatchSchema, objectIdSchema } from "@/lib/validators";
import { applyManualVerification } from "@/lib/contactStatus";
import { Lead } from "@/models";

type Ctx = { params: Promise<{ id: string }> };

export async function GET(_req: Request, { params }: Ctx) {
  try {
    const user = await getCurrentUser();
    if (!user) return jsonError(401, "Sign in required");
    const id = objectIdSchema.safeParse((await params).id);
    if (!id.success) return jsonError(404, "Lead not found");

    await connectDB();
    const lead = await Lead.findOne({ _id: id.data, ownerId: user.id }).select("-ownerId -nameNormalized").lean();
    if (!lead) return jsonError(404, "Lead not found");
    return NextResponse.json({ lead });
  } catch (err) {
    return serverError(err);
  }
}

/** Save/unsave, change status, edit notes, and manually verify one email/phone. */
export async function PATCH(req: Request, { params }: Ctx) {
  try {
    const user = await getCurrentUser();
    if (!user) return jsonError(401, "Sign in required");
    const id = objectIdSchema.safeParse((await params).id);
    if (!id.success) return jsonError(404, "Lead not found");
    const body = await readJsonBody(req, 64 * 1024); // size-capped before zod (see lib/http.ts)
    if (!body.ok) return body.response;
    const parsed = leadPatchSchema.safeParse(body.data);
    if (!parsed.success) return zodError(parsed.error);
    const { verifyContact, ...fields } = parsed.data;

    await connectDB();
    if (!verifyContact) {
      const lead = await Lead.findOneAndUpdate(
        { _id: id.data, ownerId: user.id }, // ownerId: you can only change your own leads
        { $set: fields },
        { new: true, runValidators: true, projection: { saved: 1, status: 1, notes: 1 } }
      ).lean();
      if (!lead) return jsonError(404, "Lead not found");
      return NextResponse.json({ lead });
    }

    // Manual verification: read, change one contact, write back only if nobody (e.g. the worker's
    // website check) changed the lead in between. Otherwise the client is asked to retry.
    const current = await Lead.findOne({ _id: id.data, ownerId: user.id })
      .select("emails phones whatsapp contactStatus updatedAt")
      .lean();
    if (!current) return jsonError(404, "Lead not found");
    const next = applyManualVerification(current as never, verifyContact, String(user.id), new Date());
    if (!next) return jsonError(404, "That email or phone isn't on this lead");

    const res = await Lead.updateOne(
      { _id: id.data, ownerId: user.id, updatedAt: current.updatedAt },
      { $set: { ...fields, emails: next.emails, phones: next.phones, contactStatus: next.contactStatus } },
      { runValidators: true }
    );
    if (res.matchedCount === 0) return jsonError(409, "This lead was just updated. Reload it and try again.");
    const lead = await Lead.findOne({ _id: id.data, ownerId: user.id })
      .select("saved status notes emails phones whatsapp contactStatus")
      .lean();
    return NextResponse.json({ lead });
  } catch (err) {
    return serverError(err);
  }
}
