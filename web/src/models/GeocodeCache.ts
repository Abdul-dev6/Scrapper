import { Schema, model, models, type InferSchemaType, type Model } from "mongoose";

// Written and read only by the worker (Nominatim results). Defined here so that
// `npm run db:indexes` builds its TTL index like every other collection.
// Shared across users on purpose: it holds public place data, no user data.
const geocodeCacheSchema = new Schema(
  {
    _id: { type: String, required: true }, // normalized query, e.g. "london, uk"
    result: { type: Schema.Types.Mixed, required: true },
    createdAt: { type: Date, required: true },
  },
  { collection: "geocodeCache", versionKey: false }
);

geocodeCacheSchema.index({ createdAt: 1 }, { expireAfterSeconds: 60 * 60 * 24 * 30 }); // 30 days

export type GeocodeCacheDoc = InferSchemaType<typeof geocodeCacheSchema>;
export const GeocodeCache: Model<GeocodeCacheDoc> =
  models.GeocodeCache || model<GeocodeCacheDoc>("GeocodeCache", geocodeCacheSchema);
