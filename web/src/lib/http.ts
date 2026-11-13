import { NextResponse, type NextRequest } from "next/server";
import type { ZodError } from "zod";

export function jsonError(status: number, message: string, details?: unknown) {
  return NextResponse.json({ error: { message, details } }, { status });
}

export function zodError(err: ZodError) {
  return jsonError(400, "Invalid input", err.flatten().fieldErrors);
}

export const JSON_BODY_LIMIT = 16 * 1024; // every JSON body we accept is far smaller than this

/**
 * Read a JSON request body with a hard size cap, BEFORE any parsing/validation.
 * Why: zod 3 (GHSA-mrhx-c7xc-m3w5) validates every element of an array and allocates an issue per bad
 * element even when .max() fails, so a large body can use hundreds of MB. Capping the bytes caps that.
 * The cap is enforced while streaming (a missing or false Content-Length doesn't help).
 * Invalid JSON gives data = null, exactly like the previous `req.json().catch(() => null)`.
 */
export async function readJsonBody(
  req: Request,
  limit = JSON_BODY_LIMIT
): Promise<{ ok: true; data: unknown } | { ok: false; response: NextResponse }> {
  const tooLarge = () => ({ ok: false as const, response: jsonError(413, "Request body is too large.") });
  const declared = Number(req.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > limit) return tooLarge();
  if (!req.body) return { ok: true, data: null };
  const reader = req.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > limit) {
      await reader.cancel().catch(() => {});
      return tooLarge();
    }
    chunks.push(value);
  }
  const text = new TextDecoder().decode(Buffer.concat(chunks));
  try {
    return { ok: true, data: JSON.parse(text) };
  } catch {
    return { ok: true, data: null };
  }
}

/** Catch-all so stack traces and DB errors never reach the client. */
export function serverError(err: unknown) {
  console.error(err);
  return jsonError(500, "Something went wrong on the server. Check the server logs.");
}

/** Client IP for rate limiting. On Vercel, x-forwarded-for is set by the platform. */
export function clientIp(req: NextRequest) {
  return req.headers.get("x-forwarded-for")?.split(",")[0]?.trim() || "unknown";
}
