import { createHash } from "node:crypto";
import bcrypt from "bcryptjs";
import { NextResponse, type NextRequest } from "next/server";
import { connectDB } from "@/lib/db";
import { clientIp, jsonError, readJsonBody, serverError, zodError } from "@/lib/http";
import { SESSION_COOKIE, SESSION_TTL_SECONDS, signSession } from "@/lib/session";
import { loginSchema } from "@/lib/validators";
import { LoginAttempt, User } from "@/models";

const MAX_FAILURES = 5;
const LOCK_MINUTES = 15;
// Compared against when the email doesn't exist, so response time doesn't reveal valid emails.
const DUMMY_HASH = "$2a$12$RTnPHn4H9CtMmuoYNtr3vONzvatMoX5uvTx46oqAuKrJ0ZCcBeOLC";

export async function POST(req: NextRequest) {
  try {
    const body = await readJsonBody(req); // size-capped before zod (see lib/http.ts)
    if (!body.ok) return body.response;
    const parsed = loginSchema.safeParse(body.data);
    if (!parsed.success) return zodError(parsed.error);
    const { email, password } = parsed.data;

    await connectDB();
    const key = createHash("sha256").update(`${clientIp(req)}|${email}`).digest("hex");
    const attempt = await LoginAttempt.findOne({ key }).lean();
    if (attempt && attempt.count >= MAX_FAILURES) {
      return jsonError(429, `Too many failed attempts. Try again in ${LOCK_MINUTES} minutes.`);
    }

    const user = await User.findOne({ email }).select("+passwordHash").lean();
    const ok = await bcrypt.compare(password, user?.passwordHash ?? DUMMY_HASH);

    if (!user || !ok) {
      await LoginAttempt.updateOne(
        { key },
        { $inc: { count: 1 }, $setOnInsert: { expiresAt: new Date(Date.now() + LOCK_MINUTES * 60_000) } },
        { upsert: true }
      );
      return jsonError(401, "Email or password is incorrect.");
    }

    await LoginAttempt.deleteOne({ key });
    const token = await signSession({ sub: String(user._id), email: user.email });
    const res = NextResponse.json({ ok: true });
    res.cookies.set(SESSION_COOKIE, token, {
      httpOnly: true, // JS can't read it → XSS can't steal the session
      secure: process.env.NODE_ENV === "production",
      sameSite: "lax", // blocks cross-site POSTs (CSRF) while allowing normal navigation
      path: "/",
      maxAge: SESSION_TTL_SECONDS,
    });
    return res;
  } catch (err) {
    return serverError(err);
  }
}
