import { NextResponse } from "next/server";
import { getCurrentUser } from "@/lib/auth";
import { connectDB } from "@/lib/db";
import { jsonError, serverError } from "@/lib/http";
import { objectIdSchema } from "@/lib/validators";
import { Suppression } from "@/models";

type Ctx = { params: Promise<{ id: string }> };

/** Remove an entry (e.g. added by mistake). Deleted leads are not brought back. */
export async function DELETE(_req: Request, { params }: Ctx) {
  try {
    const user = await getCurrentUser();
    if (!user) return jsonError(401, "Sign in required");
    const id = objectIdSchema.safeParse((await params).id);
    if (!id.success) return jsonError(404, "Entry not found");
    await connectDB();
    const res = await Suppression.deleteOne({ _id: id.data, ownerId: user.id });
    if (!res.deletedCount) return jsonError(404, "Entry not found");
    return NextResponse.json({ ok: true });
  } catch (err) {
    return serverError(err);
  }
}
