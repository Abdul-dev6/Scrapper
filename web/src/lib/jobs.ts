import type { Types } from "mongoose";
import { ScrapingJob } from "@/models";

export const MAX_OPEN_JOBS = 3; // queued + running at the same time
export const MAX_JOBS_PER_HOUR = 10;

/** Returns a friendly message if the user may not start another job right now, else null. */
export async function jobLimitError(ownerId: Types.ObjectId): Promise<string | null> {
  const [open, lastHour] = await Promise.all([
    ScrapingJob.countDocuments({ ownerId, status: { $in: ["queued", "running"] } }),
    ScrapingJob.countDocuments({ ownerId, createdAt: { $gte: new Date(Date.now() - 3_600_000) } }),
  ]);
  if (open >= MAX_OPEN_JOBS) return `You already have ${open} jobs waiting or running. Wait for one to finish.`;
  if (lastHour >= MAX_JOBS_PER_HOUR) return `Limit is ${MAX_JOBS_PER_HOUR} searches per hour. Try again later.`;
  return null;
}

/** Fields the browser may see. errorLog is only sent by the single-job endpoint. */
export const JOB_LIST_FIELDS = "-errorLog -workerId -ownerId -checkpoint"; // checkpoint is internal and can be large
