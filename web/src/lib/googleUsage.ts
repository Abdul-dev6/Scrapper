import { ApiUsage } from "@/models";

// Must match the worker (worker/app/google/usage.py): Google's free monthly usage is counted per
// calendar month in Pacific time. These are display defaults only; the worker enforces the limits.
const TZ = process.env.GOOGLE_USAGE_TIMEZONE || "America/Los_Angeles";
const DEFAULT_LIMITS = { textSearchPro: 4500, placeDetailsEssentials: 9000 };
const FREE_CAPS = { textSearchPro: 5000, placeDetailsEssentials: 10000 };

export function monthKey(now: Date, tz = TZ): string {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit" }).formatToParts(now);
  const y = parts.find((p) => p.type === "year")!.value;
  const m = parts.find((p) => p.type === "month")!.value;
  return `${y}-${m}`;
}

export type GoogleUsageStatus = {
  month: string;
  used: { textSearchPro: number; placeDetailsEssentials: number };
  limits: { textSearchPro: number; placeDetailsEssentials: number };
  freeCaps: { textSearchPro: number; placeDetailsEssentials: number };
  failed: { textSearchPro: number; placeDetailsEssentials: number };
  estimatedCostUsd: number;
  mode: "google+osm" | "osm-only";
  resetsAt: string | null;
};

/** Global (all users) usage for the current month. Contains no user data. */
export async function googleUsageStatus(now = new Date()): Promise<GoogleUsageStatus> {
  const month = monthKey(now);
  const doc = await ApiUsage.findById(`google:${month}`).lean();
  const used = {
    textSearchPro: doc?.textSearchPro ?? 0,
    placeDetailsEssentials: doc?.placeDetailsEssentials ?? 0,
  };
  const limits = {
    textSearchPro: doc?.limits?.textSearchPro ?? DEFAULT_LIMITS.textSearchPro,
    placeDetailsEssentials: doc?.limits?.placeDetailsEssentials ?? DEFAULT_LIMITS.placeDetailsEssentials,
  };
  const reached = used.textSearchPro >= limits.textSearchPro || used.placeDetailsEssentials >= limits.placeDetailsEssentials;
  return {
    month,
    used,
    limits,
    freeCaps: FREE_CAPS,
    failed: {
      textSearchPro: doc?.failed?.textSearchPro ?? 0,
      placeDetailsEssentials: doc?.failed?.placeDetailsEssentials ?? 0,
    },
    estimatedCostUsd: doc?.estimatedCostUsd ?? 0,
    mode: reached ? "osm-only" : "google+osm",
    resetsAt: doc?.resetsAt ? new Date(doc.resetsAt).toISOString() : null,
  };
}
