import bcrypt from "bcryptjs";
import { NextResponse, type NextRequest } from "next/server";
import { connectDB } from "@/lib/db";
import { clientIp, jsonError, readJsonBody, serverError, zodError } from "@/lib/http";
import { clear, counterKey, hit, release } from "@/lib/rateLimit";
import { sessionCookie, signSession } from "@/lib/session";
import { loginSchema } from "@/lib/validators";
import { User } from "@/models";

const LOCK_MINUTES = 15;
const MAX_FAILURES = 5; // per network + email: typos from one place
// Per network: one IP trying a common password on many accounts. Generous for shared offices/NAT.
const MAX_IP_FAILURES = 50;
// Per account, from ANY network (security review M4): credential stuffing from many IPs. Higher than the
// per-network limit so an attacker can't lock a real user out quite as easily.
const MAX_EMAIL_FAILURES = 20;
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
    const ip = clientIp(req);
    const keys = {
      ipEmail: counterKey(ip, email),
      ip: counterKey("login-ip", ip),
      email: counterKey("login-email", email),
    };
    // Every attempt is counted BEFORE the password is checked, atomically. A success gives it back.
    const [n, nIp, nEmail] = await Promise.all([
      hit(keys.ipEmail, LOCK_MINUTES), hit(keys.ip, LOCK_MINUTES), hit(keys.email, LOCK_MINUTES),
    ]);
    if (n > MAX_FAILURES || nIp > MAX_IP_FAILURES || nEmail > MAX_EMAIL_FAILURES) {
      return jsonError(429, `Too many failed attempts. Try again in ${LOCK_MINUTES} minutes.`);
    }

    const user = await User.findOne({ email }).select("+passwordHash").lean();
    const ok = await bcrypt.compare(password, user?.passwordHash ?? DUMMY_HASH);
    if (!user || !ok) return jsonError(401, "Email or password is incorrect.");

    await Promise.all([clear(keys.ipEmail), release(keys.ip), release(keys.email)]);
    const token = await signSession({ sub: String(user._id), email: user.email, ver: user.sessionVersion ?? 0 });
    const res = NextResponse.json({ ok: true });
    res.cookies.set(sessionCookie(token));
    return res;
  } catch (err) {
    return serverError(err);
  }
}
