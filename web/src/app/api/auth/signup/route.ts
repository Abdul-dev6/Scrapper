import bcrypt from "bcryptjs";
import { NextResponse, type NextRequest } from "next/server";
import { connectDB } from "@/lib/db";
import { clientIp, jsonError, serverError, zodError, readJsonBody } from "@/lib/http";
import { counterKey, hit, limitFromEnv } from "@/lib/rateLimit";
import { signupSchema } from "@/lib/validators";
import { User } from "@/models";

// Sign-up is public (decided 2026-11-09, confirmed 2026-11-16), so it is limited per network AND in
// total: rotating IPs gets around the first, not the second.
const MAX_SIGNUPS_PER_IP = 5;
const WINDOW_MINUTES = 60;

export async function POST(req: NextRequest) {
  try {
    const body = await readJsonBody(req); // size-capped before zod (see lib/http.ts)
    if (!body.ok) return body.response;
    const parsed = signupSchema.safeParse(body.data);
    if (!parsed.success) return zodError(parsed.error);
    const { email, password } = parsed.data;

    await connectDB();
    // Counted before the account is created, atomically (parallel requests can't all pass).
    // Attempts for an existing email count too, so trying many emails to see which exist is limited.
    const [perIp, total] = await Promise.all([
      hit(counterKey("signup", clientIp(req)), WINDOW_MINUTES),
      hit(counterKey("signup-global"), WINDOW_MINUTES),
    ]);
    if (perIp > MAX_SIGNUPS_PER_IP) return jsonError(429, "Too many accounts created from this network. Try again later.");
    if (total > limitFromEnv("SIGNUP_GLOBAL_HOURLY_MAX", 50)) {
      return jsonError(429, "Sign-up is busy right now. Try again in an hour.");
    }

    const passwordHash = await bcrypt.hash(password, 12);
    try {
      await User.create({ email, passwordHash });
    } catch (err) {
      if ((err as { code?: number }).code === 11000) {
        return jsonError(409, "An account with this email already exists. Sign in instead.");
      }
      throw err;
    }
    return NextResponse.json({ ok: true }, { status: 201 });
  } catch (err) {
    return serverError(err);
  }
}
