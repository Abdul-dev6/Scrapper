import { Schema, model, models, type InferSchemaType, type Model } from "mongoose";

// Overture Maps tile cache METADATA (Phase 2c), written only by the worker (worker/app/overture/places.py).
// One document per (release, 1°x1° tile); the gym places themselves are an immutable file on the
// worker's Railway Volume (OVERTURE_CACHE_DIR). Public data, shared by all users; no user data.
const overtureTileSchema = new Schema(
  {
    _id: { type: String, required: true }, // "2026-10-21.0|51_-1"
    release: String,
    tile: String, // "<floor(lat)>_<floor(lon)>"
    status: { type: String, enum: ["missing", "ready", "evicted"] },
    file: String,
    sha256: String,
    bytes: Number,
    features: Number,
    license: String,
    lease: { owner: String, until: Date }, // building lease (cross-process)
    readers: [{ _id: false, lease: String, owner: String, jobId: Schema.Types.ObjectId, file: String, until: Date }],
    lastError: { at: Date, message: String, type: { type: String } },
    builtAt: Date,
    lastAccessAt: Date,
    createdAt: Date,
  },
  { collection: "overtureTiles", versionKey: false }
);
overtureTileSchema.index({ release: 1, tile: 1 });
overtureTileSchema.index({ status: 1, lastAccessAt: 1 }); // eviction
overtureTileSchema.index({ "readers.lease": 1 });

const overtureReleaseSchema = new Schema(
  { _id: { type: String, required: true }, release: String, checkedAt: Date },
  { collection: "overtureReleases", versionKey: false }
);

export type OvertureTileDoc = InferSchemaType<typeof overtureTileSchema>;
export const OvertureTile: Model<OvertureTileDoc> =
  models.OvertureTile || model<OvertureTileDoc>("OvertureTile", overtureTileSchema);
export type OvertureReleaseDoc = InferSchemaType<typeof overtureReleaseSchema>;
export const OvertureRelease: Model<OvertureReleaseDoc> =
  models.OvertureRelease || model<OvertureReleaseDoc>("OvertureRelease", overtureReleaseSchema);
