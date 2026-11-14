"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import GoogleUsagePanel, { useGoogleUsage } from "@/components/GoogleUsagePanel";
import { CATEGORIES, type CategoryId } from "@/lib/categories";

type Candidate = { id: string; kind: string; label: string; detail: string };
type Attribution = { text: string; url: string };

export default function FindLeadsForm() {
  const router = useRouter();
  const [selected, setSelected] = useState<Set<CategoryId>>(new Set(["boxing_club", "mma_academy"]));
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [useGoogle, setUseGoogle] = useState(false); // Google is opt-in per search
  const usage = useGoogleUsage();
  // Place search: what the user typed -> candidates -> the one they picked (sent as placeId).
  const [query, setQuery] = useState("");
  const [candidates, setCandidates] = useState<Candidate[] | null>(null);
  const [placeId, setPlaceId] = useState<string | null>(null);
  const [finding, setFinding] = useState(false);
  const [attribution, setAttribution] = useState<Attribution | null>(null);

  async function findPlace() {
    setError(null);
    setPlaceId(null);
    if (query.trim().length < 2) return setError("Type a city, region or country first.");
    setFinding(true);
    const res = await fetch(`/api/places?q=${encodeURIComponent(query.trim())}`, { cache: "no-store" }).catch(() => null);
    setFinding(false);
    const data = await res?.json().catch(() => null);
    if (!res?.ok) return setError(data?.error?.message ?? "Couldn't search places. Check your connection and try again.");
    setCandidates(data.places);
    setAttribution(data.attribution ?? null);
    if (data.places.length === 1) setPlaceId(data.places[0].id); // only one match: preselect it
  }

  function toggle(id: CategoryId) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!placeId) return findPlace(); // Enter in the location box = find the place first
    setPending(true);
    setError(null);
    const chosen = candidates?.find((c) => c.id === placeId);
    const res = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        location: chosen?.label.slice(0, 120) ?? query.trim(),
        placeId,
        categories: [...selected],
        sources: useGoogle ? ["osm", "google"] : ["osm"],
      }),
    }).catch(() => null);

    if (res?.ok) {
      router.push("/jobs");
      return;
    }
    const data = await res?.json().catch(() => null);
    const fieldErrors = data?.error?.details ? Object.values(data.error.details).flat().join(" ") : "";
    setError(fieldErrors || data?.error?.message || "Could not start the search. Check your connection and try again.");
    setPending(false);
  }

  return (
    <form onSubmit={onSubmit} className="max-w-2xl space-y-6 rounded-lg border border-line bg-surface p-6">
      <div>
        <label htmlFor="location" className="font-semibold">Location</label>
        <span className="block text-sm text-muted">A city, region or country, for example “London, UK”.</span>
        <div className="mt-2 flex gap-2">
          <input id="location" name="location" required minLength={2} maxLength={120} placeholder="London, UK"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setCandidates(null); // a changed text needs a new match
              setPlaceId(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !placeId) {
                e.preventDefault(); // the Start button is disabled until a place is picked
                findPlace();
              }
            }}
            className="w-full rounded-md border border-line px-3 py-2" />
          <button type="button" onClick={findPlace} disabled={finding}
            className="whitespace-nowrap rounded-md border border-line px-4 py-2 hover:bg-chalk disabled:opacity-60">
            {finding ? "Finding…" : "Find place"}
          </button>
        </div>
        {candidates && candidates.length === 0 && (
          <p className="mt-2 text-sm text-warn">No place found. Try adding the country, for example “Leeds, UK”.</p>
        )}
        {candidates && candidates.length > 0 && (
          <fieldset className="mt-3">
            <legend className="text-sm text-muted">
              {candidates.length === 1 ? "Found this place:" : `${candidates.length} places match. Pick the one you mean:`}
            </legend>
            <div className="mt-2 space-y-2">
              {candidates.map((c) => (
                <label key={c.id} className="flex items-start gap-2 rounded-md border border-line px-3 py-2 has-[:checked]:border-canvas has-[:checked]:bg-canvas/5">
                  <input type="radio" name="place" value={c.id} checked={placeId === c.id} onChange={() => setPlaceId(c.id)}
                    className="mt-1 accent-canvas" />
                  <span>
                    <span className="block">{c.label}</span>
                    <span className="block text-xs text-muted">{c.detail}</span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>
        )}
        {attribution && (
          <p className="mt-2 text-xs text-muted">
            <a href={attribution.url} target="_blank" rel="noopener noreferrer" className="underline">{attribution.text}</a>
          </p>
        )}
      </div>

      <fieldset>
        <legend className="font-semibold">Business types</legend>
        <div className="mt-2 grid gap-2 sm:grid-cols-2">
          {CATEGORIES.map((c) => (
            <label key={c.id} className="flex items-center gap-2 rounded-md border border-line px-3 py-2 has-[:checked]:border-canvas has-[:checked]:bg-canvas/5">
              <input type="checkbox" checked={selected.has(c.id)} onChange={() => toggle(c.id)} className="accent-canvas" />
              <span>{c.label}</span>
            </label>
          ))}
        </div>
      </fieldset>

      <fieldset className="space-y-3">
        <legend className="font-semibold">Sources</legend>
        <p className="text-sm text-muted">OpenStreetMap is always searched, and each business&apos;s own website is checked for contact details.</p>
        <label className="flex items-start gap-2 rounded-md border border-line px-3 py-2 has-[:checked]:border-canvas has-[:checked]:bg-canvas/5">
          <input type="checkbox" checked={useGoogle} onChange={(e) => setUseGoogle(e.target.checked)} className="mt-1 accent-canvas" />
          <span>
            <span className="block">Also search Google Maps</span>
            <span className="block text-sm text-muted">
              Lists places Google Maps knows in this area as links you can open (max 50 Google searches per location).
              Google results never become or change a lead, and nothing from Google is saved or exported except an internal
              place reference.
            </span>
          </span>
        </label>
        {useGoogle && <GoogleUsagePanel usage={usage} />}
      </fieldset>

      {error && <p role="alert" className="text-sm text-bad">{error}</p>}
      <button type="submit" disabled={pending || selected.size === 0 || !placeId}
        className="rounded-md bg-canvas px-5 py-2 font-semibold text-white hover:bg-canvas-dark disabled:opacity-60">
        {pending ? "Starting…" : "Start search"}
      </button>
    </form>
  );
}
