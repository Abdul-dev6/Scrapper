import { Schema, model, models, type InferSchemaType, type Model } from "mongoose";
import { CATEGORY_IDS, SOURCES } from "@/lib/categories";

export const JOB_STATUSES = ["queued", "running", "completed", "partial", "failed", "cancelled"] as const;

const jobSchema = new Schema(
  {
    ownerId: { type: Schema.Types.ObjectId, ref: "User", required: true },
    location: {
      query: { type: String, required: true, maxlength: 120 },
      displayName: String, // filled by the worker after geocoding
      country: String,
      countryCode: String,
      bbox: { type: [Number], default: undefined }, // [south, west, north, east]
      // Phase 2a: the gazetteer place the user picked (copied, so the job is reproducible) and the exact
      // boundary data the worker used to build the search area (source, id, version).
      place: { type: Schema.Types.Mixed, default: undefined },
      boundary: { type: Schema.Types.Mixed, default: undefined },
      // Phase 2b: the exact OpenStreetMap extract versions this job read (provider, region, version,
      // sha256, data date). Written by the worker; shown in job details.
      osmExtract: { type: Schema.Types.Mixed, default: undefined },
      // Phase 2c: the Overture release + tiles this job read (provider, release, license).
      overture: { type: Schema.Types.Mixed, default: undefined },
    },
    categories: [{ type: String, enum: CATEGORY_IDS, required: true }],
    sources: [{ type: String, enum: SOURCES, required: true }],
    status: { type: String, enum: JOB_STATUSES, default: "queued" },
    progress: {
      stage: { type: String, default: "queued" },
      pct: { type: Number, default: 0, min: 0, max: 100 },
      processed: { type: Number, default: 0 },
      total: { type: Number, default: 0 },
    },
    stats: {
      discovered: { type: Number, default: 0 },
      withWebsite: { type: Number, default: 0 },
      newLeads: { type: Number, default: 0 },
      updated: { type: Number, default: 0 },
      merged: { type: Number, default: 0 }, // same business under another OSM id, merged into an existing lead
      duplicates: { type: Number, default: 0 },
      suppressed: { type: Number, default: 0 },
      errors: { type: Number, default: 0 },
      // Step 3: website crawl
      websitesChecked: { type: Number, default: 0 },
      websitesReachable: { type: Number, default: 0 },
      withEmail: { type: Number, default: 0 },
      // Phase 2c: Overture lane counts (kept apart from the OSM counts above)
      overture: { type: Schema.Types.Mixed, default: undefined },
    },
    // Step 6: Google Places report (mode, requests, places found, matched, Google-only...). Written by the worker.
    google: { type: Schema.Types.Mixed, default: undefined },
    // Not named "errors": that key is reserved on Mongoose documents.
    errorLog: {
      type: [{ at: Date, stage: String, url: String, message: String, _id: false }],
      default: [],
    },
    cancelRequested: { type: Boolean, default: false },
    retryOf: { type: Schema.Types.ObjectId, ref: "ScrapingJob" }, // set when created by the Retry button
    workerId: String,
    lease: String, // worker-only: random token of the current claim (a worker that lost the job can't write to it)
    lockedAt: Date, // heartbeat; a stale lock means the worker died and the job is re-queued
    attempts: { type: Number, default: 0 },
    // Worker-only: how far the job got (place, finished map pieces...), so a retry continues there.
    checkpoint: { type: Schema.Types.Mixed, default: undefined },
    // A re-queued job waits until this time before a worker may claim it (exponential backoff).
    nextAttemptAt: Date,
    startedAt: Date,
    finishedAt: Date,
  },
  { timestamps: true, collection: "scrapingJobs" }
);

jobSchema.index({ status: 1, createdAt: 1 }); // worker: oldest queued job first
jobSchema.index({ ownerId: 1, createdAt: -1 }); // UI: job history
jobSchema.index({ createdAt: -1 }); // global daily job cap (security review M3)
jobSchema.index({ status: 1, lockedAt: 1 }); // worker: find stale running jobs

export type ScrapingJobDoc = InferSchemaType<typeof jobSchema>;
export const ScrapingJob: Model<ScrapingJobDoc> =
  models.ScrapingJob || model<ScrapingJobDoc>("ScrapingJob", jobSchema);
