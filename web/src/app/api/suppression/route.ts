import { NextResponse } from "next/server";
import { getCurrentUser } from "@/lib/auth";
import { connectDB } from "@/lib/db";
import { jsonError, serverError, zodError, readJsonBody } from "@/lib/http";
import { domainFromInput, removeSuppressedLeads, suppressionHash } from "@/lib/suppression";
import { suppressionAddSchema } from "@/lib/validators";
import { Suppression } from "@/models";

/** The list shows type, reason and date only: the value itself is stored as a hash. */
export async function GET() {
  try {
    const user = await getCurrentUser();
    if (!user) return jsonError(401, "Sign in required");
    await connectDB();
    const entries = await Suppression.find({ ownerId: user.id })
      .sort({ addedAt: -1 })
      .limit(1000)
      .select("type reason addedAt")
      .lean();
    return NextResponse.json({ entries });
  } catch (err) {
    return serverError(err);
  }
}

export async function POST(req: Request) {
  try {
    const user = await getCurrentUser();
    if (!user) return jsonError(401, "Sign in required");
    const body = await readJsonBody(req); // size-capped before zod (see lib/http.ts)
    if (!body.ok) return body.response;
    const parsed = suppressionAddSchema.safeParse(body.data);
    if (!parsed.success) return zodError(parsed.error);
    const { type, reason } = parsed.data;
    let value = parsed.data.value;

    if (type === "email" && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)) return jsonError(400, "Enter a valid email address.");
    if (type === "phone") {
      if (!value.startsWith("+")) return jsonError(400, "Enter the phone with its country code, e.g. +44 20 7946 0000.");
      if (value.replace(/\D/g, "").length < 7) return jsonError(400, "That phone number is too short.");
    }
    if (type === "domain") {
      value = domainFromInput(value);
      if (!value.includes(".")) return jsonError(400, "Enter a website or domain, e.g. examplegym.co.uk.");
    }

    await connectDB();
    const valueHash = suppressionHash(type, value);
    const res = await Suppression.updateOne(
      { ownerId: user.id, type, valueHash },
      { $setOnInsert: { ownerId: user.id, type, valueHash, reason: reason || "Added by hand" } },
      { upsert: true }
    );
    if (!res.upsertedCount) return jsonError(409, "That's already on your opt-out list.");
    const removedLeads = await removeSuppressedLeads(user.id);
    return NextResponse.json({ ok: true, removedLeads }, { status: 201 });
  } catch (err) {
    return serverError(err);
  }
}
