import { Schema, model, models, type InferSchemaType, type Model } from "mongoose";

// Brute-force protection that works on serverless (in-memory counters reset per instance).
const loginAttemptSchema = new Schema(
  {
    key: { type: String, required: true }, // sha256(ip + email)
    count: { type: Number, default: 0 },
    expiresAt: { type: Date, required: true },
  },
  { collection: "loginAttempts" }
);

loginAttemptSchema.index({ key: 1 }, { unique: true });
loginAttemptSchema.index({ expiresAt: 1 }, { expireAfterSeconds: 0 }); // MongoDB deletes expired docs

export type LoginAttemptDoc = InferSchemaType<typeof loginAttemptSchema>;
export const LoginAttempt: Model<LoginAttemptDoc> =
  models.LoginAttempt || model<LoginAttemptDoc>("LoginAttempt", loginAttemptSchema);
