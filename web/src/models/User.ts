import { Schema, model, models, type InferSchemaType, type Model } from "mongoose";

const userSchema = new Schema(
  {
    email: { type: String, required: true, lowercase: true, trim: true, maxlength: 254 },
    passwordHash: { type: String, required: true, select: false }, // never returned unless asked
    name: { type: String, trim: true, maxlength: 100 },
    // Goes into every session token. Changing the password increments it, so every session signed
    // before (other browsers, a stolen cookie) stops working. Missing = 0 (tokens from before this field).
    sessionVersion: { type: Number, default: 0 },
  },
  { timestamps: true, collection: "users" }
);

userSchema.index({ email: 1 }, { unique: true });

export type UserDoc = InferSchemaType<typeof userSchema>;
export const User: Model<UserDoc> = models.User || model<UserDoc>("User", userSchema);
