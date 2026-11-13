import { Schema, model, models, type InferSchemaType, type Model } from "mongoose";

// Global monthly Google Places usage, written ONLY by the worker (atomic reserve-before-request).
// One document per calendar month in Google's billing time zone: _id = "google:2026-11".
const apiUsageSchema = new Schema(
  {
    _id: { type: String, required: true },
    provider: String,
    month: String,
    timezone: String,
    textSearchPro: { type: Number, default: 0 },
    placeDetailsEssentials: { type: Number, default: 0 },
    failed: { textSearchPro: Number, placeDetailsEssentials: Number },
    limits: { textSearchPro: Number, placeDetailsEssentials: Number },
    freeCaps: { textSearchPro: Number, placeDetailsEssentials: Number },
    estimatedCostUsd: Number,
    exhaustedAt: Date,
    resetsAt: Date,
    createdAt: Date,
    updatedAt: Date,
  },
  { collection: "apiUsage", versionKey: false }
);

// One row per Google request (success or failure) with its estimated cost. Kept 90 days.
const apiUsageLogSchema = new Schema(
  {
    at: { type: Date, required: true },
    provider: String,
    sku: String,
    month: String,
    jobId: Schema.Types.ObjectId,
    ok: Boolean,
    httpStatus: Number,
    error: String,
    what: String,
    listPriceUsd: Number,
    estimatedCostUsd: Number,
    counter: Number,
  },
  { collection: "apiUsageLog", versionKey: false }
);
apiUsageLogSchema.index({ at: 1 }, { expireAfterSeconds: 60 * 60 * 24 * 90 });
apiUsageLogSchema.index({ month: 1, sku: 1 });

// Google place references: ONLY the place ID (Google allows storing it indefinitely). Never linked to a
// lead (decision 2026-11-14). location/leadId/"matched" exist only on refs written by the earlier version;
// cached coordinates are purged after 30 days. NO name, address or other Google content.
const googlePlaceRefSchema = new Schema(
  {
    ownerId: { type: Schema.Types.ObjectId, ref: "User", required: true },
    placeId: { type: String, required: true },
    status: { type: String, enum: ["seen", "matched", "unmatched", "gone"], required: true },
    leadId: { type: Schema.Types.ObjectId, ref: "Lead" },
    area: String, // normalized search text, e.g. "london, uk"
    location: { lat: Number, lon: Number },
    locationExpiresAt: Date,
    jobIds: [{ type: Schema.Types.ObjectId, ref: "ScrapingJob" }],
    lastJobId: Schema.Types.ObjectId,
    firstSeenAt: Date,
    lastSeenAt: Date,
  },
  { collection: "googlePlaceRefs", versionKey: false }
);
googlePlaceRefSchema.index({ ownerId: 1, placeId: 1 }, { unique: true });
googlePlaceRefSchema.index({ ownerId: 1, area: 1, status: 1 });
googlePlaceRefSchema.index({ ownerId: 1, jobIds: 1, status: 1 });
googlePlaceRefSchema.index({ locationExpiresAt: 1 });

// Which map tiles were already searched with which query (counts only), so a repeated search
// goes deeper instead of paying for the same results. Expires after 30 days.
const googleCoverageSchema = new Schema(
  {
    _id: { type: String, required: true },
    ownerId: Schema.Types.ObjectId,
    query: String,
    bbox: [Number],
    results: Number,
    saturated: Boolean,
    searchedAt: { type: Date, required: true },
  },
  { collection: "googleCoverage", versionKey: false }
);
googleCoverageSchema.index({ searchedAt: 1 }, { expireAfterSeconds: 60 * 60 * 24 * 30 });

export type ApiUsageDoc = InferSchemaType<typeof apiUsageSchema>;
export const ApiUsage: Model<ApiUsageDoc> = models.ApiUsage || model<ApiUsageDoc>("ApiUsage", apiUsageSchema);
export type ApiUsageLogDoc = InferSchemaType<typeof apiUsageLogSchema>;
export const ApiUsageLog: Model<ApiUsageLogDoc> =
  models.ApiUsageLog || model<ApiUsageLogDoc>("ApiUsageLog", apiUsageLogSchema);
export type GooglePlaceRefDoc = InferSchemaType<typeof googlePlaceRefSchema>;
export const GooglePlaceRef: Model<GooglePlaceRefDoc> =
  models.GooglePlaceRef || model<GooglePlaceRefDoc>("GooglePlaceRef", googlePlaceRefSchema);
export type GoogleCoverageDoc = InferSchemaType<typeof googleCoverageSchema>;
export const GoogleCoverage: Model<GoogleCoverageDoc> =
  models.GoogleCoverage || model<GoogleCoverageDoc>("GoogleCoverage", googleCoverageSchema);
