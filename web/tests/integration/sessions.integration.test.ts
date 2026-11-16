// Session invalidation: changing the password ends every OTHER session (other browsers, a stolen
// cookie); the browser that changed it stays signed in with a fresh token. Real login, password and
// API handlers + in-memory MongoDB. Run with: npm run test:integration
import assert from "node:assert/strict";
import { after, before, mock, test } from "node:test";
import { MongoMemoryServer } from "mongodb-memory-server";
import mongoose, { Types } from "mongoose";

let cookieValue: string | undefined;
mock.module("next/headers", {
  namedExports: {
    cookies: async () => ({ get: (name: string) => (cookieValue ? { name, value: cookieValue } : undefined) }),
  },
});

type Handler = (req: Request, ctx?: unknown) => Promise<Response>;
let mongod: MongoMemoryServer;
let LOGIN: Handler, PASSWORD: Handler, LEADS: Handler, ME: Handler;
const users = () => mongoose.connection.db!.collection("users");
const OLD = "first password 123";
const NEW = "second password 456";

const tokenFrom = (res: Response) => /ls_session=([^;]+)/.exec(res.headers.get("set-cookie") ?? "")?.[1];
const login = async (password: string, ip = "198.51.100.7") =>
  LOGIN(new Request("http://test/api/auth/login", { method: "POST", headers: { "x-forwarded-for": ip },
    body: JSON.stringify({ email: "pat@test.test", password }) }));
async function withToken(token: string | undefined, h: Handler, req: Request) {
  cookieValue = token;
  return h(req);
}
const leads = (token?: string) => withToken(token, LEADS, new Request("http://test/api/leads"));
const changePassword = (token: string | undefined, current: string, next: string) =>
  withToken(token, PASSWORD, new Request("http://test/api/auth/password", { method: "POST", body: JSON.stringify({ current, next }) }));

before(async () => {
  mongod = await MongoMemoryServer.create();
  process.env.MONGODB_URI = mongod.getUri();
  process.env.MONGODB_DB = "sessions_integration";
  process.env.AUTH_SECRET = "integration-test-secret-0123456789abcdef";
  const { NextRequest } = await import("next/server");
  const wrap = (h: (...args: never[]) => Promise<Response>): Handler => (req, c) => h(new NextRequest(req) as never, c as never);
  LOGIN = wrap((await import("../../src/app/api/auth/login/route")).POST);
  PASSWORD = wrap((await import("../../src/app/api/auth/password/route")).POST);
  LEADS = wrap((await import("../../src/app/api/leads/route")).GET);
  ME = wrap((await import("../../src/app/api/auth/me/route")).GET);
  await (await import("../../src/lib/db")).connectDB();
  const bcrypt = (await import("bcryptjs")).default;
  // No sessionVersion field: an account from before this change.
  await users().insertOne({ email: "pat@test.test", passwordHash: await bcrypt.hash(OLD, 4) } as never);
});

after(async () => {
  await mongoose.disconnect();
  await mongod.stop();
});

test("a password change ends the other sessions and keeps the current one signed in", async () => {
  const laptop = tokenFrom(await login(OLD));
  const phone = tokenFrom(await login(OLD, "198.51.100.8"));
  assert.ok(laptop && phone);
  assert.equal((await leads(laptop)).status, 200);
  assert.equal((await leads(phone)).status, 200);

  const res = await changePassword(laptop, OLD, NEW);
  assert.equal(res.status, 200);
  const fresh = tokenFrom(res);
  assert.ok(fresh && fresh !== laptop);
  assert.match(res.headers.get("set-cookie") ?? "", /HttpOnly/i);

  assert.equal((await leads(phone)).status, 401); // the other browser (or a stolen cookie) is out
  assert.equal((await leads(laptop)).status, 401); // the old token of this browser too...
  assert.equal((await leads(fresh)).status, 200); // ...which carries on with the fresh one
  assert.equal((await withToken(fresh, ME, new Request("http://test/api/auth/me"))).status, 200);
  assert.equal((await users().findOne({ email: "pat@test.test" }))?.sessionVersion, 1);

  assert.equal((await login(OLD, "198.51.100.9")).status, 401);
  const again = tokenFrom(await login(NEW, "198.51.100.9"));
  assert.equal((await leads(again)).status, 200); // a new sign-in gets the current version

  // An old session can't be used to change the password back either.
  assert.equal((await changePassword(phone, NEW, "third password 789")).status, 401);
});

test("tokens issued before session versions existed stay valid until the password changes", async () => {
  const { SignJWT } = await import("jose");
  const user = await users().findOne({ email: "pat@test.test" });
  await users().updateOne({ _id: user!._id }, { $unset: { sessionVersion: "" } });
  const legacy = await new SignJWT({ email: "pat@test.test" }).setProtectedHeader({ alg: "HS256" }).setSubject(String(user!._id))
    .setIssuedAt().setExpirationTime("1h").sign(new TextEncoder().encode(process.env.AUTH_SECRET!));
  assert.equal((await leads(legacy)).status, 200); // deploying this change signs nobody out
  await users().updateOne({ _id: user!._id }, { $set: { sessionVersion: 1 } });
  assert.equal((await leads(legacy)).status, 401);
});

test("a session for an account that no longer exists is refused", async () => {
  const { signSession } = await import("../../src/lib/session");
  const ghost = await signSession({ sub: String(new Types.ObjectId()), email: "ghost@test.test" });
  assert.equal((await leads(ghost)).status, 401);
});
