// Edge-safe: used by middleware and server code. No Node-only imports here.
import { SignJWT, jwtVerify } from "jose";

export const SESSION_COOKIE = "ls_session";
export const SESSION_TTL_SECONDS = 60 * 60 * 24 * 7; // 7 days

export type SessionPayload = { sub: string; email: string; ver?: number };

/** Cookie settings for a session token (login and password change). */
export function sessionCookie(token: string) {
  return {
    name: SESSION_COOKIE,
    value: token,
    httpOnly: true, // JS can't read it → XSS can't steal the session
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax" as const, // blocks cross-site POSTs (CSRF) while allowing normal navigation
    path: "/",
    maxAge: SESSION_TTL_SECONDS,
  };
}

function key() {
  const secret = process.env.AUTH_SECRET;
  if (!secret || secret.length < 32) throw new Error("AUTH_SECRET is missing or too short");
  return new TextEncoder().encode(secret);
}

export async function signSession(payload: SessionPayload) {
  return new SignJWT({ email: payload.email, ver: payload.ver ?? 0 })
    .setProtectedHeader({ alg: "HS256" })
    .setSubject(payload.sub)
    .setIssuedAt()
    .setExpirationTime(`${SESSION_TTL_SECONDS}s`)
    .sign(key());
}

export async function verifySession(token: string | undefined): Promise<SessionPayload | null> {
  if (!token) return null;
  try {
    const { payload } = await jwtVerify(token, key(), { algorithms: ["HS256"] });
    if (typeof payload.sub !== "string" || typeof payload.email !== "string") return null;
    // Only the signature + expiry are checked here (Edge middleware has no database);
    // getCurrentUser (lib/auth.ts) also compares `ver` with the user's sessionVersion.
    const ver = typeof payload.ver === "number" ? payload.ver : 0; // tokens from before `ver` = version 0
    return { sub: payload.sub, email: payload.email, ver };
  } catch {
    return null; // expired, tampered, or wrong secret
  }
}
