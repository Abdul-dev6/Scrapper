"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import GoogleUsagePanel, { useGoogleUsage } from "@/components/GoogleUsagePanel";
import { CATEGORIES, type CategoryId } from "@/lib/categories";

export default function FindLeadsForm() {
  const router = useRouter();
  const [selected, setSelected] = useState<Set<CategoryId>>(new Set(["boxing_club", "mma_academy"]));
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [useGoogle, setUseGoogle] = useState(false); // Google is opt-in per search
  const usage = useGoogleUsage();

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
    setPending(true);
    setError(null);
    const location = String(new FormData(e.currentTarget).get("location") ?? "");
    const res = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ location, categories: [...selected], sources: useGoogle ? ["osm", "google"] : ["osm"] }),
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
      <label className="block">
        <span className="font-semibold">Location</span>
        <span className="block text-sm text-muted">A city, area or country, for example “London, UK”.</span>
        <input name="location" required minLength={2} maxLength={120} placeholder="London, UK"
          className="mt-2 w-full rounded-md border border-line px-3 py-2" />
      </label>

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
      <button type="submit" disabled={pending || selected.size === 0}
        className="rounded-md bg-canvas px-5 py-2 font-semibold text-white hover:bg-canvas-dark disabled:opacity-60">
        {pending ? "Starting…" : "Start search"}
      </button>
    </form>
  );
}
