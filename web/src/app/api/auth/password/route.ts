import bcrypt from "bcryptjs";
import { NextResponse } from "next/server";
import { getCurrentUser } from "@/lib/auth";
import { connectDB } from "@/lib/db";
import { jsonError, serverError, zodError, readJsonBody } from "@/lib/http";
import { sessionCookie, signSession } from "@/lib/session";
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
    const doc = await User.findById(user.id).select("+passwordHash sessionVersion").lean();
    if (!doc || !(await bcrypt.compare(parsed.data.current, doc.passwordHash))) {
      return jsonError(400, "Your current password is incorrect.");
    }
    if (parsed.data.current === parsed.data.next) return jsonError(400, "The new password must be different.");
    // New hash + next session version in one update: every session signed before (other browsers,
    // a stolen cookie) stops working at its next request. This browser gets a fresh token.
    const updated = await User.findOneAndUpdate(
      { _id: user.id, sessionVersion: doc.sessionVersion ?? { $in: [0, null] } },
      { $set: { passwordHash: await bcrypt.hash(parsed.data.next, 12) }, $inc: { sessionVersion: 1 } },
      { new: true, projection: { email: 1, sessionVersion: 1 } }
    ).lean();
    if (!updated) return jsonError(409, "Your account was just changed elsewhere. Sign in again and retry.");
    const res = NextResponse.json({ ok: true });
    res.cookies.set(sessionCookie(await signSession({ sub: String(user.id), email: updated.email, ver: updated.sessionVersion })));
    return res;
  } catch (err) {
    return serverError(err);
  }
}
