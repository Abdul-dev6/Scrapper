import { Schema, model, models, type InferSchemaType, type Model } from "mongoose";

// Opt-out list. We store a hash of the normalized value, so the list itself
// doesn't keep a readable copy of the contact someone asked us to forget.
const suppressionSchema = new Schema(
  {
    ownerId: { type: Schema.Types.ObjectId, ref: "User", required: true },
    type: { type: String, enum: ["email", "phone", "domain"], required: true },
    valueHash: { type: String, required: true }, // sha256(normalized value), hex
    reason: { type: String, maxlength: 500 },
  },
  { timestamps: { createdAt: "addedAt", updatedAt: false }, collection: "suppressionList" }
);

suppressionSchema.index({ ownerId: 1, type: 1, valueHash: 1 }, { unique: true });

export type SuppressionDoc = InferSchemaType<typeof suppressionSchema>;
export const Suppression: Model<SuppressionDoc> =
  models.Suppression || model<SuppressionDoc>("Suppression", suppressionSchema);
