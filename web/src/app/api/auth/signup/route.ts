import { createHash } from "node:crypto";
import bcrypt from "bcryptjs";
import { NextResponse, type NextRequest } from "next/server";
import { connectDB } from "@/lib/db";
import { clientIp, jsonError, serverError, zodError, readJsonBody } from "@/lib/http";
import { signupSchema } from "@/lib/validators";
import { LoginAttempt, User } from "@/models";

// Sign-up is public, so limit how many accounts one IP can create.
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
    // Reuses the loginAttempts collection (TTL index deletes old counters automatically).
    const key = createHash("sha256").update(`signup|${clientIp(req)}`).digest("hex");
    const attempt = await LoginAttempt.findOne({ key }).lean();
    if (attempt && attempt.count >= MAX_SIGNUPS_PER_IP) {
      return jsonError(429, "Too many accounts created from this network. Try again later.");
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

    await LoginAttempt.updateOne(
      { key },
      { $inc: { count: 1 }, $setOnInsert: { expiresAt: new Date(Date.now() + WINDOW_MINUTES * 60_000) } },
      { upsert: true }
    );
    return NextResponse.json({ ok: true }, { status: 201 });
  } catch (err) {
    return serverError(err);
  }
}
