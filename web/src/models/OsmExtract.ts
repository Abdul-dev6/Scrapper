import { Schema, model, models, type InferSchemaType, type Model } from "mongoose";

// OpenStreetMap extract cache METADATA (Phase 2b). Written only by the worker (worker/app/osm/extracts.py).
// The filtered gym files themselves live on the worker's Railway Volume (OSM_CACHE_DIR); MongoDB never
// holds PBF or cache contents. Shared across users: public OpenStreetMap data (ODbL), no user data.
const versionSchema = new Schema(
  {
    version: String, // Geofabrik's dated file name, e.g. "great-britain-261113"
    file: String, // path relative to OSM_CACHE_DIR (immutable file)
    sha256: String, // of the filtered file
    bytes: Number,
    features: Number, // gym elements in the file
    sourceUrl: String,
    sourceBytes: Number, // original .osm.pbf size
    sourceMd5: String, // Geofabrik's published MD5 (null if it published none)
    sourceEtag: String,
    osmTimestamp: String, // data as of (Geofabrik state.txt)
    createdAt: Date,
    refreshedAt: Date, // last time it was confirmed current (freshness = OSM_REFRESH_DAYS)
  },
  { _id: false }
);

const osmExtractSchema = new Schema(
  {
    _id: { type: String, required: true }, // Geofabrik region id, e.g. "great-britain", "us/california"
    name: String,
    parent: String,
    pbfUrl: String,
    status: { type: String, enum: ["missing", "ready", "evicted"], default: "missing" },
    current: { type: versionSchema, default: null }, // only set after the file passed validation
    previous: { type: versionSchema, default: null }, // kept until no job reads it any more
    refreshRequestedAt: Date, // a search found it stale: the background refresher picks it up
    refreshLease: { owner: String, until: Date }, // cross-process: only the owner may refresh/publish
    readers: [{ _id: false, lease: String, owner: String, jobId: Schema.Types.ObjectId, file: String, until: Date }],
    lastError: { at: Date, message: String, type: { type: String } },
    lastAccessAt: Date, // least-recently-used eviction
    createdAt: Date,
  },
  { collection: "osmExtracts", versionKey: false }
);
osmExtractSchema.index({ refreshRequestedAt: 1 }); // background refresher: what's waiting
osmExtractSchema.index({ status: 1, lastAccessAt: 1 }); // eviction order / planning
osmExtractSchema.index({ "readers.lease": 1 }); // pin renewal / release
osmExtractSchema.index({ "refreshLease.until": 1 }); // expired leases

export type OsmExtractDoc = InferSchemaType<typeof osmExtractSchema>;
export const OsmExtract: Model<OsmExtractDoc> = models.OsmExtract || model<OsmExtractDoc>("OsmExtract", osmExtractSchema);
