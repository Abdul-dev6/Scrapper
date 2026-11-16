import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { Types } from "mongoose";
import { connectDB } from "./db";
import { SESSION_COOKIE, verifySession } from "./session";
import { User } from "@/models";

export type CurrentUser = { id: Types.ObjectId; email: string; sessionVersion: number };

/**
 * The signed-in user, or null. Besides the token's signature and expiry (checked in middleware too),
 * the user must still exist and the token's version must be the current one: changing the password
 * increments it, which ends every older session (one indexed _id lookup per request).
 */
export async function getCurrentUser(): Promise<CurrentUser | null> {
  const store = await cookies();
  const session = await verifySession(store.get(SESSION_COOKIE)?.value);
  if (!session || !Types.ObjectId.isValid(session.sub)) return null;
  const id = new Types.ObjectId(session.sub);
  await connectDB();
  const user = await User.findById(id).select("sessionVersion").lean();
  const current = user?.sessionVersion ?? 0;
  if (!user || current !== (session.ver ?? 0)) return null;
  return { id, email: session.email, sessionVersion: current };
}

/** For server components: send unauthenticated visitors to /login. */
export async function requireUserPage(): Promise<CurrentUser> {
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  return user;
}
