import bcrypt from "bcryptjs";
import { NextResponse } from "next/server";
import { getCurrentUser } from "@/lib/auth";
import { connectDB } from "@/lib/db";
import { jsonError, serverError, zodError, readJsonBody } from "@/lib/http";
import { passwordChangeSchema } from "@/lib/validators";
import { User } from "@/models";

export async function POST(req: Request) {
  try {
    const user = await getCurrentUser();
    if (!user) return jsonError(401, "Sign in required");
    const body = await readJsonBody(req); // size-capped before zod (see lib/http.ts)
    if (!body.ok) return body.response;
    const parsed = passwordChangeSchema.safeParse(body.data);
    if (!parsed.success) return zodError(parsed.error);

    await connectDB();
    const doc = await User.findById(user.id).select("+passwordHash").lean();
    if (!doc || !(await bcrypt.compare(parsed.data.current, doc.passwordHash))) {
      return jsonError(400, "Your current password is incorrect.");
    }
    if (parsed.data.current === parsed.data.next) return jsonError(400, "The new password must be different.");
    await User.updateOne({ _id: user.id }, { $set: { passwordHash: await bcrypt.hash(parsed.data.next, 12) } });
    return NextResponse.json({ ok: true });
  } catch (err) {
    return serverError(err);
  }
}
