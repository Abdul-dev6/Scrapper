"use client";

import { effectiveContactStatus } from "@/lib/contactStatus";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import EmptyState from "@/components/EmptyState";
import { CATEGORIES } from "@/lib/categories";
import LeadDrawer from "./LeadDrawer";
import {
  CONTACT_LABEL, CONTACT_STYLE, SOCIAL_KEYS, SOCIAL_SHORT, STATUS_LABEL, categoryLabel, countryName, errorMessage, safeHref,
  type LeadRow,
} from "./shared";

type Facets = { countries: { code: string; count: number }[]; jobs: { _id: string; label: string; createdAt: string; found: number }[] };
type Page = { leads: LeadRow[]; total: number; page: number; pageSize: number };

const FILTER_KEYS = ["q", "country", "city", "category", "hasEmail", "hasPhone", "hasWebsite", "contactStatus", "status", "job", "sort"];
const select = "rounded-md border border-line bg-surface px-2 py-1.5 text-sm";

export default function LeadsBrowser({ savedOnly = false }: { savedOnly?: boolean }) {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const [data, setData] = useState<Page | null>(null);
  const [facets, setFacets] = useState<Facets | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [exporting, setExporting] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [search, setSearch] = useState(params.get("q") ?? "");
  const [city, setCity] = useState(params.get("city") ?? "");

  // The URL holds the filters, so a filtered view can be bookmarked or shared and Back works.
  const apiQuery = useMemo(() => {
    const p = new URLSearchParams();
    for (const k of [...FILTER_KEYS, "page"]) {
      const v = params.get(k);
      if (v) p.set(k, v);
    }
    if (savedOnly) p.set("saved", "1");
    return p.toString();
  }, [params, savedOnly]);

  const setParams = useCallback(
    (changes: Record<string, string | null>, keepPage = false) => {
      const p = new URLSearchParams(params.toString());
      for (const [k, v] of Object.entries(changes)) v ? p.set(k, v) : p.delete(k);
      if (!keepPage) p.delete("page"); // a new filter starts at page 1
      router.replace(`${pathname}${p.size ? `?${p}` : ""}`, { scroll: false });
    },
    [params, pathname, router]
  );

  const load = useCallback(async () => {
    const res = await fetch(`/api/leads?${apiQuery}`, { cache: "no-store" }).catch(() => null);
    if (!res?.ok) return setError(await errorMessage(res));
    setError(null);
    setData(await res.json());
  }, [apiQuery]);

  useEffect(() => {
    load();
  }, [load]);
  useEffect(() => {
    fetch("/api/leads/facets", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => d && setFacets(d))
      .catch(() => {});
  }, []);

  // Typing in the search box: wait 300 ms after the last key before searching.
  useEffect(() => {
    const t = setTimeout(() => {
      const q = search.trim();
      const c = city.trim();
      // One URL update for both boxes (two separate updates would overwrite each other).
      if ((params.get("q") ?? "") !== q || (params.get("city") ?? "") !== c) setParams({ q: q || null, city: c || null });
    }, 300);
    return () => clearTimeout(t);
  }, [search, city, params, setParams]);

  async function toggleSave(lead: LeadRow) {
    setData((d) => d && { ...d, leads: d.leads.map((l) => (l._id === lead._id ? { ...l, saved: !lead.saved } : l)) });
    const res = await fetch(`/api/leads/${lead._id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ saved: !lead.saved }),
    }).catch(() => null);
    if (!res?.ok) {
      setNotice(await errorMessage(res));
      load(); // undo the optimistic change
    } else if (savedOnly) load();
  }

  async function exportFile(format: "csv" | "xlsx") {
    setExporting(format);
    setNotice(null);
    const q = new URLSearchParams(apiQuery);
    q.delete("page");
    q.set("format", format);
    const res = await fetch(`/api/leads/export?${q}`).catch(() => null);
    setExporting(null);
    if (!res?.ok) return setNotice(await errorMessage(res));
    const blob = await res.blob();
    const name = /filename="([^"]+)"/.exec(res.headers.get("content-disposition") ?? "")?.[1] ?? `leads.${format}`;
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
    URL.revokeObjectURL(url);
    const rows = Number(res.headers.get("x-export-rows") ?? 0);
    const skipped = Number(res.headers.get("x-export-skipped-opt-out") ?? 0);
    setNotice(`Exported ${rows.toLocaleString()} leads.${skipped ? ` ${skipped} skipped (opt-out list).` : ""}`);
  }

  const v = (k: string) => params.get(k) ?? "";
  const anyFilter = FILTER_KEYS.some((k) => k !== "sort" && params.get(k));
  const pages = data ? Math.max(1, Math.ceil(data.total / data.pageSize)) : 1;
  const contactFilter = v("hasEmail") === "1" ? "email" : v("hasPhone") === "1" ? "phone" : v("hasWebsite") === "1" ? "website" : v("hasEmail") === "0" && v("hasPhone") === "0" ? "nothing" : "";

  return (
    <div className="space-y-4">
      {/* Filters */}
      <div className="flex flex-wrap items-end gap-3 rounded-lg border border-line bg-surface p-4">
        <label className="flex min-w-48 flex-1 flex-col gap-1 text-sm">
          <span className="text-muted">Search</span>
          <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Name, city or website" maxLength={100} className="rounded-md border border-line px-3 py-1.5" />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted">Country</span>
          <select value={v("country")} onChange={(e) => setParams({ country: e.target.value || null })} className={select}>
            <option value="">All countries</option>
            {facets?.countries.map((c) => (
              <option key={c.code} value={c.code}>
                {countryName(c.code)} ({c.count})
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted">City</span>
          <input value={city} onChange={(e) => setCity(e.target.value)} placeholder="Any" maxLength={100} className="w-32 rounded-md border border-line px-3 py-1.5" />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted">Business type</span>
          <select value={v("category")} onChange={(e) => setParams({ category: e.target.value || null })} className={select}>
            <option value="">All types</option>
            {CATEGORIES.map((c) => (
              <option key={c.id} value={c.id}>{c.label}</option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted">Has</span>
          <select
            value={contactFilter}
            onChange={(e) => {
              const x = e.target.value;
              setParams({
                hasEmail: x === "email" ? "1" : x === "nothing" ? "0" : null,
                hasPhone: x === "phone" ? "1" : x === "nothing" ? "0" : null,
                hasWebsite: x === "website" ? "1" : null,
              });
            }}
            className={select}
          >
            <option value="">Anything</option>
            <option value="email">An email</option>
            <option value="phone">A phone</option>
            <option value="website">A website</option>
            <option value="nothing">No email or phone</option>
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted">Contact check</span>
          <select value={v("contactStatus")} onChange={(e) => setParams({ contactStatus: e.target.value || null })} className={select}>
            <option value="">Any</option>
            {Object.entries(CONTACT_LABEL).map(([k, l]) => (
              <option key={k} value={k}>{l}</option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted">Lead status</span>
          <select value={v("status")} onChange={(e) => setParams({ status: e.target.value || null })} className={select}>
            <option value="">Any</option>
            {Object.entries(STATUS_LABEL).map(([k, l]) => (
              <option key={k} value={k}>{l}</option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted">From search</span>
          <select value={v("job")} onChange={(e) => setParams({ job: e.target.value || null })} className={`${select} max-w-48`}>
            <option value="">All searches</option>
            {facets?.jobs.map((j) => (
              <option key={j._id} value={j._id}>
                {j.label} · {new Date(j.createdAt).toLocaleDateString()}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted">Sort</span>
          <select value={v("sort") || "newest"} onChange={(e) => setParams({ sort: e.target.value === "newest" ? null : e.target.value })} className={select}>
            <option value="newest">Newest first</option>
            <option value="oldest">Oldest first</option>
            <option value="name">Name A–Z</option>
            <option value="confidence">Best contact first</option>
          </select>
        </label>
        {anyFilter && (
          <button
            onClick={() => {
              setSearch("");
              setCity("");
              router.replace(pathname, { scroll: false });
            }}
            className="rounded-md border border-line px-3 py-1.5 text-sm hover:bg-chalk"
          >
            Clear filters
          </button>
        )}
      </div>

      {/* Summary + export */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted" aria-live="polite">
          {data ? `${data.total.toLocaleString()} ${data.total === 1 ? "lead" : "leads"}` : "Loading…"}
        </p>
        <div className="flex gap-2">
          {(["csv", "xlsx"] as const).map((f) => (
            <button
              key={f}
              onClick={() => exportFile(f)}
              disabled={!data?.total || exporting !== null}
              className="rounded-md border border-line bg-surface px-3 py-1.5 text-sm font-semibold hover:bg-chalk disabled:opacity-50"
            >
              {exporting === f ? "Exporting…" : f === "csv" ? "Export CSV" : "Export Excel"}
            </button>
          ))}
        </div>
      </div>
      {notice && <p role="status" className="rounded-md border border-line bg-surface px-4 py-2 text-sm">{notice}</p>}
      {error && <p className="text-sm text-bad">{error}</p>}

      {/* Table */}
      {data && data.total === 0 ? (
        anyFilter ? (
          <EmptyState message="No leads match these filters." />
        ) : savedOnly ? (
          <EmptyState message="No saved leads yet. Press ☆ on a lead to save it." action={{ href: "/leads", label: "All leads" }} />
        ) : (
          <EmptyState message="No leads yet. Run a search first." action={{ href: "/find-leads", label: "Find leads" }} />
        )
      ) : (
        <div className="overflow-x-auto rounded-lg border border-line bg-surface">
          <table className="w-full min-w-[1000px] text-left text-sm">
            <thead className="border-b border-line text-muted">
              <tr>
                <th className="w-10 px-3 py-3"><span className="sr-only">Saved</span></th>
                <th className="px-3 py-3 font-medium">Business</th>
                <th className="px-3 py-3 font-medium">Location</th>
                <th className="px-3 py-3 font-medium">Email</th>
                <th className="px-3 py-3 font-medium">Phone</th>
                <th className="px-3 py-3 font-medium">Website</th>
                <th className="px-3 py-3 font-medium">Social</th>
                <th className="px-3 py-3 font-medium">Contact</th>
                <th className="px-3 py-3 font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {data?.leads.map((l) => {
                const phone = l.phones?.[0]?.e164 || l.phones?.[0]?.raw || l.whatsapp?.[0]?.e164;
                const phoneCount = (l.phones?.length ?? 0) + (l.whatsapp?.length ?? 0);
                const site = safeHref(l.website?.url);
                return (
                  <tr key={l._id} className="border-b border-line last:border-0 hover:bg-chalk/50">
                    <td className="px-3 py-2">
                      <button
                        onClick={() => toggleSave(l)}
                        aria-pressed={!!l.saved}
                        aria-label={l.saved ? `Unsave ${l.name}` : `Save ${l.name}`}
                        className={`text-lg leading-none ${l.saved ? "text-canvas" : "text-muted hover:text-canvas"}`}
                      >
                        {l.saved ? "★" : "☆"}
                      </button>
                    </td>
                    <td className="px-3 py-2">
                      <button onClick={() => setOpenId(l._id)} className="text-left font-medium text-canvas hover:underline">
                        {l.name}
                      </button>
                      <span className="block text-xs text-muted">{l.categories?.map(categoryLabel).join(", ")}</span>
                    </td>
                    <td className="px-3 py-2 text-muted">{[l.address?.city, countryName(l.address?.countryCode)].filter(Boolean).join(", ")}</td>
                    <td className="max-w-56 truncate px-3 py-2" title={l.emails?.map((e) => e.value).join(", ")}>
                      {l.emails?.[0]?.value ?? <span className="text-muted">–</span>}
                      {(l.emails?.length ?? 0) > 1 && <span className="ml-1 text-xs text-muted">+{l.emails!.length - 1}</span>}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2">
                      {phone ?? <span className="text-muted">–</span>}
                      {phoneCount > 1 && <span className="ml-1 text-xs text-muted">+{phoneCount - 1}</span>}
                    </td>
                    <td className="max-w-40 truncate px-3 py-2">
                      {site ? (
                        <a href={site} target="_blank" rel="noopener noreferrer nofollow" className="text-canvas underline">
                          {l.website?.domain || site}
                        </a>
                      ) : (
                        <span className="text-muted">–</span>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      <span className="flex gap-1.5">
                        {SOCIAL_KEYS.map((k) => {
                          const href = safeHref(l.socials?.[k]?.url);
                          return href ? (
                            <a key={k} href={href} target="_blank" rel="noopener noreferrer nofollow" title={k} className="rounded bg-chalk px-1.5 text-xs font-semibold hover:bg-line">
                              {SOCIAL_SHORT[k]}
                            </a>
                          ) : null;
                        })}
                      </span>
                    </td>
                    <td className="px-3 py-2">
                      <span className={CONTACT_STYLE[effectiveContactStatus(l)]}>{CONTACT_LABEL[effectiveContactStatus(l)]}</span>
                    </td>
                    <td className="px-3 py-2 text-muted">{STATUS_LABEL[l.status ?? "new"]}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* Pages */}
      {data && pages > 1 && (
        <nav className="flex items-center justify-end gap-3 text-sm" aria-label="Pages">
          <button
            onClick={() => setParams({ page: data.page > 2 ? String(data.page - 1) : null }, true)}
            disabled={data.page <= 1}
            className="rounded-md border border-line bg-surface px-3 py-1.5 disabled:opacity-50"
          >
            Previous
          </button>
          <span className="text-muted">
            Page {data.page} of {pages}
          </span>
          <button
            onClick={() => setParams({ page: String(data.page + 1) }, true)}
            disabled={data.page >= pages}
            className="rounded-md border border-line bg-surface px-3 py-1.5 disabled:opacity-50"
          >
            Next
          </button>
        </nav>
      )}

      {openId && <LeadDrawer id={openId} onClose={() => setOpenId(null)} onChanged={load} />}
    </div>
  );
}
