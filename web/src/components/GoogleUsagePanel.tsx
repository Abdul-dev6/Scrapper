"use client";

import { useEffect, useState } from "react";
import type { GoogleUsageStatus } from "@/lib/googleUsage";

const ROWS = [
  { key: "textSearchPro", label: "Text Search (discovery)" },
  { key: "placeDetailsEssentials", label: "Place Details (not used any more)" },
] as const;

export function useGoogleUsage(refreshMs = 0) {
  const [usage, setUsage] = useState<GoogleUsageStatus | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () =>
      fetch("/api/usage", { cache: "no-store" })
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => alive && d && setUsage(d.google))
        .catch(() => {});
    load();
    const t = refreshMs ? setInterval(load, refreshMs) : undefined;
    return () => {
      alive = false;
      if (t) clearInterval(t);
    };
  }, [refreshMs]);
  return usage;
}

/** This month's shared Google quota. Everyone's searches count against the same limits. */
export default function GoogleUsagePanel({ usage }: { usage: GoogleUsageStatus | null }) {
  if (!usage) return null;
  const resets = usage.resetsAt ? new Date(usage.resetsAt).toLocaleDateString() : "the 1st of next month";
  return (
    <section aria-label="Google usage this month" className="rounded-lg border border-line bg-surface p-4 text-sm">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="font-semibold">Google Places usage · {usage.month}</h2>
        <span className={usage.mode === "osm-only" ? "font-semibold text-warn" : "text-ok"}>
          {usage.mode === "osm-only" ? `Limit reached: OpenStreetMap only until ${resets}` : "Google + OpenStreetMap"}
        </span>
      </div>
      <dl className="mt-3 space-y-2">
        {ROWS.map((r) => {
          const used = usage.used[r.key];
          const limit = usage.limits[r.key];
          const pct = Math.min(100, Math.round((used / Math.max(limit, 1)) * 100));
          return (
            <div key={r.key}>
              <dt className="flex justify-between text-muted">
                <span>{r.label}</span>
                <span className="tabular-nums">
                  {used.toLocaleString()} / {limit.toLocaleString()}
                  <span className="ml-1 text-xs">(Google free: {usage.freeCaps[r.key].toLocaleString()})</span>
                </span>
              </dt>
              <dd className="mt-1 h-1.5 overflow-hidden rounded bg-line" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
                <span className={`block h-full ${pct >= 100 ? "bg-warn" : "bg-canvas"}`} style={{ width: `${pct}%` }} />
              </dd>
            </div>
          );
        })}
      </dl>
      <p className="mt-3 text-xs text-muted">
        Shared by all searches. When either limit is reached, Google is paused and searches use OpenStreetMap only;
        it resumes automatically on {resets}.
        {usage.failed.textSearchPro + usage.failed.placeDetailsEssentials > 0 &&
          ` Failed requests this month: ${usage.failed.textSearchPro + usage.failed.placeDetailsEssentials}.`}
        {usage.estimatedCostUsd > 0 && ` Estimated cost beyond free usage: $${usage.estimatedCostUsd.toFixed(2)}.`}
      </p>
    </section>
  );
}
