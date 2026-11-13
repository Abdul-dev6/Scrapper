import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { Types } from "mongoose";
import { SESSION_COOKIE, verifySession } from "./session";

export type CurrentUser = { id: Types.ObjectId; email: string };

export async function getCurrentUser(): Promise<CurrentUser | null> {
  const store = await cookies();
  const session = await verifySession(store.get(SESSION_COOKIE)?.value);
  if (!session || !Types.ObjectId.isValid(session.sub)) return null;
  return { id: new Types.ObjectId(session.sub), email: session.email };
}

/** For server components: send unauthenticated visitors to /login. */
export async function requireUserPage(): Promise<CurrentUser> {
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  return user;
}
