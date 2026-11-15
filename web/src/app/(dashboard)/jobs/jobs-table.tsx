"use client";

import { Fragment, useCallback, useEffect, useState } from "react";
import EmptyState from "@/components/EmptyState";
import GoogleUsagePanel, { useGoogleUsage } from "@/components/GoogleUsagePanel";
import { CATEGORIES } from "@/lib/categories";

type Status = "queued" | "running" | "completed" | "partial" | "failed" | "cancelled";
type Job = {
  _id: string;
  location: {
    query: string;
    displayName?: string;
    osmExtract?: { provider: string; extracts: { name: string; version: string; osmTimestamp?: string | null; features?: number }[] };
  };
  categories: string[];
  sources?: string[];
  status: Status;
  google?: GoogleReport;
  cancelRequested?: boolean;
  progress: { stage: string; pct: number; processed?: number; total?: number };
  stats: {
    discovered: number;
    newLeads: number;
    updated?: number;
    merged?: number;
    suppressed?: number;
    errors: number;
    websitesChecked?: number;
    websitesReachable?: number;
    withEmail?: number;
  };
  lockedAt?: string;
  nextAttemptAt?: string; // set while a job waits to retry after a temporary problem
  attempts?: number;
  startedAt?: string;
  finishedAt?: string;
  createdAt: string;
  retryOf?: string;
};
type ErrorEntry = { at: string; stage: string; message: string; url?: string };
type GoogleReport = {
  mode: "off" | "osm-only" | "google+osm";
  note?: string;
  textSearchRequests?: number;
  placesFound?: number;
  newPlaces?: number;
  tilesSearched?: number;
  tilesSkipped?: number;
};
const MODE_LABEL: Record<GoogleReport["mode"], string> = {
  off: "OpenStreetMap",
  "osm-only": "OpenStreetMap only (Google paused)",
  "google+osm": "Google + OpenStreetMap",
};
// Google's documented link format for a place ID. Only the ID is stored, never Google's name/address.
const mapsLink = (placeId: string) => `https://www.google.com/maps/place/?q=place_id:${encodeURIComponent(placeId)}`;

const STATUS_STYLE: Record<Status, string> = {
  queued: "text-muted",
  running: "text-canvas",
  completed: "text-ok",
  partial: "text-warn",
  failed: "text-bad",
  cancelled: "text-muted",
};
const STATUS_HELP: Partial<Record<Status, string>> = {
  partial: "Finished, but some parts could not be searched. Leads found so far are saved.",
  failed: "Stopped with an error. Leads found before the error are saved.",
  cancelled: "Stopped by you. Leads found so far are saved.",
};
const STAGE_NAMES: Record<string, string> = {
  discover: "Map search",
  save: "Saving",
  crawl: "Website check",
  google: "Google Places",
  pipeline: "Job",
};
const STALE_AFTER_MS = 3 * 60_000; // the worker reports at least every couple of minutes
const label = (id: string) => CATEGORIES.find((c) => c.id === id)?.label ?? id;

function duration(from?: string, to?: string) {
  if (!from) return "";
  const s = Math.max(0, Math.round(((to ? Date.parse(to) : Date.now()) - Date.parse(from)) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

async function readError(res: Response | null) {
  if (!res) return "Couldn't reach the server. Check your connection.";
  const body = await res.json().catch(() => null);
  return body?.error?.message ?? "Something went wrong.";
}

export default function JobsTable() {
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null); // job id with a button in flight
  const [open, setOpen] = useState<string | null>(null); // job id with details shown
  const [now, setNow] = useState(() => Date.now());
  const usage = useGoogleUsage(30_000);

  const load = useCallback(async () => {
    const res = await fetch("/api/jobs", { cache: "no-store" }).catch(() => null);
    if (!res?.ok) return setError("Couldn't load jobs. Retrying…");
    setError(null);
    setJobs((await res.json()).jobs);
    setNow(Date.now());
  }, []);

  const active = jobs?.some((j) => j.status === "queued" || j.status === "running");
  useEffect(() => {
    load();
  }, [load]);
  useEffect(() => {
    if (!active) return; // only poll while something is in progress
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [active, load]);

  async function act(job: Job, action: "cancel" | "retry") {
    if (action === "cancel" && !window.confirm(`Stop the search for "${job.location.query}"? Leads found so far are kept.`)) return;
    setBusy(job._id);
    setNotice(null);
    const res = await fetch(`/api/jobs/${job._id}/${action}`, { method: "POST" }).catch(() => null);
    setBusy(null);
    if (!res?.ok) return setNotice(await readError(res));
    const body = action === "retry" ? await res.json().catch(() => null) : null;
    setNotice(
      action === "retry"
        ? `Started a new search for "${job.location.query}".` +
            (body?.googleRemoved ? " It uses OpenStreetMap only: Retry never re-runs Google. To use Google, start a new search and tick it." : "")
        : null
    );
    load();
  }

  if (jobs === null) return <p className="text-muted">{error ?? "Loading jobs…"}</p>;
  if (jobs.length === 0) return <EmptyState message="No searches yet." action={{ href: "/find-leads", label: "Find leads" }} />;

  return (
    <div className="space-y-3">
      <GoogleUsagePanel usage={usage} />
      {notice && (
        <p role="status" className="rounded-md border border-line bg-surface px-4 py-2 text-sm">
          {notice}
        </p>
      )}
      <div className="overflow-x-auto rounded-lg border border-line bg-surface">
        {error && <p className="px-4 py-2 text-sm text-warn">{error}</p>}
        <table className="w-full min-w-[900px] text-left text-sm">
          <thead className="border-b border-line text-muted">
            <tr>
              <th className="px-4 py-3 font-medium">Location</th>
              <th className="px-4 py-3 font-medium">Business types</th>
              <th className="px-4 py-3 font-medium">Status</th>
              <th className="px-4 py-3 text-right font-medium">Found</th>
              <th className="px-4 py-3 text-right font-medium">New</th>
              <th className="px-4 py-3 text-right font-medium" title="Websites that answered / websites checked">
                Websites
              </th>
              <th className="px-4 py-3 text-right font-medium" title="Leads from this search that have an email">
                With email
              </th>
              <th className="px-4 py-3 font-medium">Started</th>
              <th className="px-4 py-3 font-medium">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((j) => {
              const running = j.status === "running";
              const stale = running && j.lockedAt && now - Date.parse(j.lockedAt) > STALE_AFTER_MS;
              const canCancel = (j.status === "queued" || running) && !j.cancelRequested;
              const canRetry = j.status === "failed" || j.status === "partial" || j.status === "cancelled";
              const checked = j.stats.websitesChecked ?? 0;
              return (
                <Fragment key={j._id}>
                  <tr className="border-b border-line last:border-0 align-top">
                    <td className="px-4 py-3 font-medium">
                      {j.location.displayName ?? j.location.query}
                      {j.retryOf && <span className="block text-xs font-normal text-muted">Retry</span>}
                    </td>
                    <td className="px-4 py-3 text-muted">
                      {j.categories.map(label).join(", ")}
                      {j.google && <span className="block text-xs">{MODE_LABEL[j.google.mode] ?? j.google.mode}</span>}
                    </td>
                    <td className={`px-4 py-3 ${STATUS_STYLE[j.status]}`}>
                      <span className="font-semibold capitalize">{j.cancelRequested && running ? "Cancelling" : j.status}</span>
                      {running && (
                        <span className="mt-1 block">
                          <span
                            className="block h-1.5 w-40 overflow-hidden rounded bg-line"
                            role="progressbar"
                            aria-valuenow={j.progress.pct}
                            aria-valuemin={0}
                            aria-valuemax={100}
                          >
                            <span className="block h-full bg-canvas transition-[width] duration-500" style={{ width: `${j.progress.pct}%` }} />
                          </span>
                          <span className="text-xs text-muted">
                            {j.progress.pct}% · {j.progress.stage} · {duration(j.startedAt)}
                          </span>
                        </span>
                      )}
                      {j.status === "queued" &&
                        (j.nextAttemptAt && Date.parse(j.nextAttemptAt) > now ? (
                          <span className="mt-1 block max-w-xs text-xs text-warn">
                            Temporary problem; retrying at {new Date(j.nextAttemptAt).toLocaleTimeString()} (attempt {(j.attempts ?? 0) + 1}).
                            It continues where it stopped.
                          </span>
                        ) : (
                          <span className="mt-1 block text-xs text-muted">Waiting for the worker…</span>
                        ))}
                      {stale && (
                        <span className="mt-1 block max-w-xs text-xs text-warn">
                          No update from the worker for {duration(j.lockedAt)}. Is the worker running? If it restarted, the
                          job continues automatically within 10 minutes.
                        </span>
                      )}
                      {(j.status === "failed" || j.status === "partial") && j.progress.stage !== j.status && (
                        <span className="mt-1 block max-w-xs text-xs text-muted">{j.progress.stage}</span>
                      )}
                      {!running && j.finishedAt && (
                        <span className="block text-xs text-muted">took {duration(j.startedAt, j.finishedAt)}</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums">{j.stats.discovered}</td>
                    <td className="px-4 py-3 text-right tabular-nums">{j.stats.newLeads}</td>
                    <td className="px-4 py-3 text-right tabular-nums">{checked ? `${j.stats.websitesReachable ?? 0} / ${checked}` : "–"}</td>
                    <td className="px-4 py-3 text-right tabular-nums">{j.stats.withEmail ?? "–"}</td>
                    <td className="px-4 py-3 text-muted">{new Date(j.createdAt).toLocaleString()}</td>
                    <td className="px-4 py-3">
                      <div className="flex justify-end gap-2 whitespace-nowrap">
                        {canCancel && (
                          <button
                            onClick={() => act(j, "cancel")}
                            disabled={busy === j._id}
                            className="rounded-md border border-line px-3 py-1 text-bad hover:bg-bad/5 disabled:opacity-60"
                          >
                            Cancel
                          </button>
                        )}
                        {canRetry && (
                          <button
                            onClick={() => act(j, "retry")}
                            disabled={busy === j._id}
                            className="rounded-md bg-canvas px-3 py-1 font-semibold text-white hover:bg-canvas-dark disabled:opacity-60"
                          >
                            Retry
                          </button>
                        )}
                        <button
                          onClick={() => setOpen(open === j._id ? null : j._id)}
                          aria-expanded={open === j._id}
                          className="rounded-md border border-line px-3 py-1 hover:bg-chalk"
                        >
                          {open === j._id ? "Hide" : "Details"}
                        </button>
                      </div>
                    </td>
                  </tr>
                  {open === j._id && (
                    <tr className="border-b border-line bg-chalk/60">
                      <td colSpan={9} className="px-4 py-4">
                        <JobDetails job={j} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function JobDetails({ job }: { job: Job }) {
  const [log, setLog] = useState<ErrorEntry[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [googleOnly, setGoogleOnly] = useState<string[]>([]);
  const errors = job.stats.errors;

  useEffect(() => {
    let alive = true;
    fetch(`/api/jobs/${job._id}`, { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((d) => {
        if (!alive) return;
        setLog(d.job.errorLog ?? []);
        setGoogleOnly(d.googleOnly ?? []);
      })
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, [job._id, errors]); // reload when new errors arrive

  const s = job.stats;
  const facts: [string, string | number][] = [
    ["Businesses found", s.discovered],
    ["New leads", s.newLeads],
    ["Updated leads", s.updated ?? 0],
    ["Duplicates merged", s.merged ?? 0],
    ["Skipped (opt-out list)", s.suppressed ?? 0],
    ["Websites checked", s.websitesChecked ?? 0],
    ["Websites that answered", s.websitesReachable ?? 0],
    ["Leads with email", s.withEmail ?? 0],
    ["Problems logged", s.errors],
  ];

  const g = job.google;
  const googleFacts: [string, string | number][] = g && g.mode !== "off"
    ? [
        ["Google searches", g.textSearchRequests ?? 0],
        ["Places Google found", g.placesFound ?? 0],
        ["New to you", g.newPlaces ?? 0],
        ["Map areas skipped (searched recently)", g.tilesSkipped ?? 0],
      ]
    : [];

  const osm = job.location.osmExtract;
  return (
    <div className="space-y-4">
      {STATUS_HELP[job.status] && <p className="text-sm">{STATUS_HELP[job.status]}</p>}
      {osm?.extracts?.length ? (
        <p className="text-sm text-muted">
          OpenStreetMap data (Geofabrik extract{osm.extracts.length > 1 ? "s" : ""}):{" "}
          {osm.extracts
            .map((e) => `${e.name}, data of ${(e.osmTimestamp || e.version).slice(0, 10)}`)
            .join(" · ")}
          {" "}· © OpenStreetMap contributors (ODbL)
        </p>
      ) : null}
      {g && g.mode !== "off" && (
        <div className="space-y-2">
          <h3 className="text-sm font-semibold">Google Places · {MODE_LABEL[g.mode] ?? g.mode}</h3>
          {g.note && <p className="text-sm text-muted">{g.note}</p>}
          {googleFacts.length > 0 && (
            <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-3">
              {googleFacts.map(([k, v]) => (
                <div key={k}>
                  <dt className="text-muted">{k}</dt>
                  <dd className="font-semibold tabular-nums">{v}</dd>
                </div>
              ))}
            </dl>
          )}
          {googleOnly.length > 0 && (
            <details className="text-sm">
              <summary className="cursor-pointer">
                {googleOnly.length} place{googleOnly.length === 1 ? "" : "s"} Google Maps found (links only)
              </summary>
              <p className="mt-1 text-xs text-muted">
                Google&apos;s terms don&apos;t allow saving their names or addresses, and these are never matched with your
                leads, so they are links only. Some may already be among your leads.
              </p>
              <ul className="mt-2 grid gap-1 sm:grid-cols-2">
                {googleOnly.map((id, i) => (
                  <li key={id}>
                    <a href={mapsLink(id)} target="_blank" rel="noopener noreferrer nofollow" className="text-canvas underline">
                      Place {i + 1}: open in Google Maps
                    </a>
                  </li>
                ))}
              </ul>
            </details>
          )}
        </div>
      )}
      <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-4">
        {facts.map(([k, v]) => (
          <div key={k}>
            <dt className="text-muted">{k}</dt>
            <dd className="font-semibold tabular-nums">{v}</dd>
          </div>
        ))}
      </dl>
      <div>
        <h3 className="mb-2 text-sm font-semibold">Problems</h3>
        {failed && <p className="text-sm text-warn">Couldn&apos;t load the problem list.</p>}
        {!failed && log === null && <p className="text-sm text-muted">Loading…</p>}
        {log?.length === 0 && <p className="text-sm text-muted">None. Everything worked.</p>}
        {log && log.length > 0 && (
          <>
            <ul className="max-h-64 space-y-1 overflow-y-auto text-sm">
              {log.map((e, i) => (
                <li key={i} className="rounded border border-line bg-surface px-3 py-1.5">
                  <span className="font-medium">{STAGE_NAMES[e.stage] ?? e.stage}:</span> {e.message}
                  {e.url && (
                    <a href={e.url} target="_blank" rel="noopener noreferrer nofollow" className="ml-1 break-all text-canvas underline">
                      {e.url}
                    </a>
                  )}
                  <span className="ml-2 text-xs text-muted">{new Date(e.at).toLocaleTimeString()}</span>
                </li>
              ))}
            </ul>
            {s.errors > log.length && (
              <p className="mt-1 text-xs text-muted">Showing the latest {log.length} of {s.errors} problems.</p>
            )}
          </>
        )}
      </div>
    </div>
  );
}
