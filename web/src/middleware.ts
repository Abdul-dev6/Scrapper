import { NextResponse, type NextRequest } from "next/server";
import { SESSION_COOKIE, verifySession } from "@/lib/session";

const PUBLIC_PATHS = ["/login", "/signup", "/api/auth/login", "/api/auth/signup"];
const MUTATING = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/**
 * Content-Security-Policy (security review L1). The page shows text from thousands of websites and
 * OpenStreetMap; React escapes it and links go through safeHref, and this is the safety net behind that:
 * only our own scripts (with this request's nonce) may run, nothing may frame the app, and forms and
 * requests can only go to our own origin.
 */
function contentSecurityPolicy(nonce: string): string {
  const dev = process.env.NODE_ENV !== "production";
  return [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${dev ? " 'unsafe-eval'" : ""}`, // dev: React refresh
    "style-src 'self' 'unsafe-inline'", // style={{ width }} on progress bars
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    `connect-src 'self'${dev ? " ws: wss:" : ""}`, // dev: hot reload socket
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
    ...(dev ? [] : ["upgrade-insecure-requests"]),
  ].join("; ");
}

/**
 * CSRF backup layer (security review L8). The session cookie is SameSite=Lax, which already keeps it
 * off cross-site POSTs. Browsers also send an Origin header on every POST/PATCH/DELETE: if it names
 * another site, refuse. (No Origin = not a browser form/fetch, e.g. curl; the cookie rule still applies.)
 */
function crossSite(req: NextRequest): boolean {
  const origin = req.headers.get("origin");
  if (!origin) return false;
  try {
    return new URL(origin).host !== req.headers.get("host");
  } catch {
    return true; // "null" (sandboxed frame, file://) or garbage
  }
}

export async function middleware(req: NextRequest) {
  const { pathname } = req.nextUrl;

  if (pathname.startsWith("/api/") && MUTATING.has(req.method) && crossSite(req)) {
    return NextResponse.json({ error: { message: "Cross-site request refused" } }, { status: 403 });
  }

  if (!PUBLIC_PATHS.includes(pathname)) {
    const session = await verifySession(req.cookies.get(SESSION_COOKIE)?.value);
    if (!session) {
      if (pathname.startsWith("/api/")) {
        return NextResponse.json({ error: { message: "Sign in required" } }, { status: 401 });
      }
      const url = req.nextUrl.clone();
      url.pathname = "/login";
      url.search = pathname === "/" ? "" : `?next=${encodeURIComponent(pathname)}`;
      return NextResponse.redirect(url);
    }
  }

  const nonce = btoa(crypto.randomUUID());
  const csp = contentSecurityPolicy(nonce);
  // Next.js reads the nonce from the request's CSP header and adds it to the scripts it renders.
  const headers = new Headers(req.headers);
  headers.set("content-security-policy", csp);
  headers.set("x-nonce", nonce);
  const res = NextResponse.next({ request: { headers } });
  res.headers.set("Content-Security-Policy", csp);
  return res;
}

export const config = {
  // everything except Next.js internals and static files
  matcher: ["/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|ico|woff2?)$).*)"],
};
