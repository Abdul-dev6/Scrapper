// Creates (or resets the password of) the single admin account from ADMIN_EMAIL / ADMIN_PASSWORD.
//   npm run seed:user
import bcrypt from "bcryptjs";
import mongoose from "mongoose";
import { connectDB } from "../src/lib/db";
import { User } from "../src/models";

async function main() {
  const email = process.env.ADMIN_EMAIL?.trim().toLowerCase();
  const password = process.env.ADMIN_PASSWORD;
  if (!email || !password) throw new Error("Set ADMIN_EMAIL and ADMIN_PASSWORD in .env.local");
  if (password.length < 12) throw new Error("ADMIN_PASSWORD must be at least 12 characters");

  await connectDB();
  const passwordHash = await bcrypt.hash(password, 12);
  const res = await User.updateOne({ email }, { $set: { email, passwordHash } }, { upsert: true });
  console.log(res.upsertedCount ? `Created user ${email}` : `Updated password for ${email}`);
  console.log("You can now remove ADMIN_PASSWORD from .env.local.");
  await mongoose.disconnect();
}

main().catch(async (err) => {
  console.error(err.message ?? err);
  await mongoose.disconnect();
  process.exit(1);
});
