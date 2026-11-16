import { Schema, model, models, type InferSchemaType, type Model } from "mongoose";

// One short lock per user around "check the job limits, then create the job" (lib/jobs.ts).
// Without it, simultaneous requests all counted the same open jobs and all created one.
const jobStartLockSchema = new Schema(
  {
    _id: { type: Schema.Types.ObjectId, required: true }, // the user's id: at most one lock per user
    token: { type: String, required: true }, // only the holder releases it
    until: { type: Date, required: true }, // a crashed request's lock is free again after this
    expiresAt: { type: Date, required: true }, // TTL clean-up of old locks (correctness uses `until`)
  },
  { collection: "jobStartLocks", versionKey: false }
);

jobStartLockSchema.index({ expiresAt: 1 }, { expireAfterSeconds: 0 });

export type JobStartLockDoc = InferSchemaType<typeof jobStartLockSchema>;
export const JobStartLock: Model<JobStartLockDoc> =
  models.JobStartLock || model<JobStartLockDoc>("JobStartLock", jobStartLockSchema);
