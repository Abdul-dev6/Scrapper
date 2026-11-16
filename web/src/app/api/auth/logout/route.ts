import { NextResponse } from "next/server";
import { getCurrentUser } from "@/lib/auth";
import { serverError } from "@/lib/http";
import { sessionCookie } from "@/lib/session";
import { User } from "@/models";

/**
 * POST /api/auth/logout                      -> sign out this browser
 * POST /api/auth/logout  {"everywhere":true} -> also end every other session (security review L3):
 *   the user's sessionVersion goes up, so all tokens signed before (other devices, a copied cookie)
 *   stop working at their next request. A plain logout only deletes this browser's cookie.
 */
export async function POST(req: Request) {
  try {
    const raw = await req.text().catch(() => "");
    let everywhere = false;
    try {
      everywhere = raw.length < 200 && JSON.parse(raw || "{}")?.everywhere === true;
    } catch {
      everywhere = false;
    }
    if (everywhere) {
      const user = await getCurrentUser();
      if (user) await User.updateOne({ _id: user.id }, { $inc: { sessionVersion: 1 } });
    }
    const res = NextResponse.json({ ok: true });
    // Same flags as when it was set (secure, httpOnly, sameSite), so the browser really replaces it.
    res.cookies.set({ ...sessionCookie(""), maxAge: 0 });
    return res;
  } catch (err) {
    return serverError(err);
  }
}
