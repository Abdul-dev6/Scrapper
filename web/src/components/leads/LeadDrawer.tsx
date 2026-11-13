"use client";

import { useEffect, useState } from "react";
import {
  effectiveContactStatus, emailMx, phoneFormatValid, type EmailChecks, type PhoneChecks, type VerifyContactInput,
} from "@/lib/contactStatus";
import {
  CONTACT_LABEL, SOCIAL_KEYS, STATUS_LABEL, categoryLabel, countryName, errorMessage, safeHref, type Social,
} from "./shared";

type Src = { sourceUrl?: string; sourceType?: string };
type FullLead = {
  _id: string;
  name: string;
  categories?: string[];
  address?: { full?: string; street?: string; city?: string; region?: string; postcode?: string; countryCode?: string };
  website?: { url?: string; reachable?: boolean; checkedAt?: string };
  emails?: ({ value: string; kind?: string; mxValid?: boolean; checks?: EmailChecks } & Src)[];
  phones?: ({ e164?: string; raw?: string; checks?: PhoneChecks } & Src)[];
  whatsapp?: ({ e164?: string; url?: string } & Src)[];
  socials?: Record<string, Social | undefined>;
  externalIds?: { osm?: string; osmAliases?: string[]; googlePlaceId?: string };
  verifiedAt?: string;
  contactStatus?: string;
  confidence?: number;
  saved?: boolean;
  status?: keyof typeof STATUS_LABEL;
  notes?: string;
  discoveredAt?: string;
};

const SOURCE_LABEL: Record<string, string> = { osm: "OpenStreetMap", website: "website", google: "Google Maps" };

function Source({ s }: { s: Src }) {
  const href = safeHref(s.sourceUrl);
  if (!href) return null;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer nofollow" className="ml-2 text-xs text-muted underline" title={href}>
      source: {SOURCE_LABEL[s.sourceType ?? ""] ?? "website"}
    </a>
  );
}

/** "Verify" toggle for one email/phone. Only a person can mark a contact as verified. */
function VerifyButton({ verified, disabled, onClick }: { verified: boolean; disabled: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`ml-2 rounded border px-1.5 text-xs disabled:opacity-60 ${verified ? "border-ok text-ok" : "border-line hover:bg-chalk"}`}
      title={verified ? "You verified this. Click to undo." : "Mark as verified after you confirmed it yourself (e.g. a reply or a call)"}
    >
      {verified ? "✓ verified by you" : "Mark verified"}
    </button>
  );
}

function ExtLink({ href, children }: { href?: string; children: React.ReactNode }) {
  const safe = safeHref(href);
  if (!safe) return <span>{children}</span>;
  return (
    <a href={safe} target="_blank" rel="noopener noreferrer nofollow" className="break-all text-canvas underline">
      {children}
    </a>
  );
}

export default function LeadDrawer({ id, onClose, onChanged }: { id: string; onClose: () => void; onChanged: () => void }) {
  const [lead, setLead] = useState<FullLead | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setLead(null);
    setError(null);
    fetch(`/api/leads/${id}`, { cache: "no-store" })
      .then(async (r) => (r.ok ? r.json() : Promise.reject(await errorMessage(r))))
      .then((d) => {
        if (!alive) return;
        setLead(d.lead);
        setNotes(d.lead.notes ?? "");
      })
      .catch((e) => alive && setError(typeof e === "string" ? e : "Couldn't load this lead."));
    return () => {
      alive = false;
    };
  }, [id]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function patch(body: Partial<Pick<FullLead, "saved" | "status" | "notes">> & { verifyContact?: VerifyContactInput }) {
    setSaving(true);
    setMsg(null);
    const res = await fetch(`/api/leads/${id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).catch(() => null);
    setSaving(false);
    if (!res?.ok) return setMsg(await errorMessage(res));
    const { lead: updated } = await res.json();
    setLead((l) => (l ? { ...l, ...updated } : l));
    if ("notes" in body) setMsg("Notes saved.");
    if (body.verifyContact) setMsg(body.verifyContact.verified ? "Marked as verified." : "Verification removed.");
    onChanged();
  }

  async function optOut() {
    if (!lead) return;
    const ok = window.confirm(
      `"${lead.name}" asked not to be contacted?\n\nIts emails, phones and website go on your opt-out list, ` +
        `and this lead (plus any other lead with the same contacts) is deleted. Future searches skip it.`
    );
    if (!ok) return;
    setSaving(true);
    const res = await fetch(`/api/leads/${id}/opt-out`, { method: "POST" }).catch(() => null);
    setSaving(false);
    if (!res?.ok) return setMsg(await errorMessage(res));
    onChanged();
    onClose();
  }

  const a = lead?.address;
  const addressLine = a ? [a.street || a.full, a.postcode, a.city, a.region, countryName(a.countryCode)].filter(Boolean).join(", ") : "";

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-ink/30" onClick={onClose}>
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Lead details"
        className="h-full w-full max-w-xl overflow-y-auto bg-surface p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-start justify-between gap-4">
          <h2 className="text-xl font-semibold">{lead?.name ?? "Lead"}</h2>
          <button onClick={onClose} className="rounded-md border border-line px-3 py-1 text-sm hover:bg-chalk">
            Close
          </button>
        </div>

        {error && <p className="text-bad">{error}</p>}
        {!lead && !error && <p className="text-muted">Loading…</p>}

        {lead && (
          <div className="space-y-6 text-sm">
            <section className="space-y-1">
              {lead.categories?.length ? <p className="text-muted">{lead.categories.map(categoryLabel).join(", ")}</p> : null}
              {addressLine && <p>{addressLine}</p>}
              <p className="text-muted">
                {CONTACT_LABEL[effectiveContactStatus(lead)]} · confidence {Math.round((lead.confidence ?? 0) * 100)}%
                {lead.discoveredAt && <> · found {new Date(lead.discoveredAt).toLocaleDateString()}</>}
              </p>
            </section>

            <section className="flex flex-wrap items-center gap-3">
              <button
                onClick={() => patch({ saved: !lead.saved })}
                disabled={saving}
                aria-pressed={!!lead.saved}
                className={`rounded-md px-3 py-1.5 font-semibold disabled:opacity-60 ${lead.saved ? "bg-canvas text-white" : "border border-line hover:bg-chalk"}`}
              >
                {lead.saved ? "★ Saved" : "☆ Save"}
              </button>
              <label className="flex items-center gap-2">
                <span className="text-muted">Status</span>
                <select
                  value={lead.status ?? "new"}
                  onChange={(e) => patch({ status: e.target.value as FullLead["status"] })}
                  disabled={saving}
                  className="rounded-md border border-line bg-surface px-2 py-1"
                >
                  {Object.entries(STATUS_LABEL).map(([v, l]) => (
                    <option key={v} value={v}>{l}</option>
                  ))}
                </select>
              </label>
            </section>

            <section>
              <h3 className="mb-2 font-semibold">Website</h3>
              {lead.website?.url ? (
                <p>
                  <ExtLink href={lead.website.url}>{lead.website.url}</ExtLink>
                  {lead.website.reachable === false && <span className="ml-2 text-xs text-warn">didn&apos;t answer when checked</span>}
                </p>
              ) : (
                <p className="text-muted">None listed</p>
              )}
            </section>

            <section>
              <h3 className="mb-2 font-semibold">Emails</h3>
              {lead.emails?.length ? (
                <ul className="space-y-1">
                  {lead.emails.map((e) => (
                    <li key={e.value}>
                      <span className="font-medium">{e.value}</span>
                      <span className="ml-2 text-xs text-muted">syntax ok</span>
                      {emailMx(e) === "no_mx" && <span className="ml-2 text-xs text-bad">domain has no mail server</span>}
                      {emailMx(e) === "valid" && <span className="ml-2 text-xs text-muted">· mail server found</span>}
                      {emailMx(e) === "unknown" && <span className="ml-2 text-xs text-muted">· mail server not checked</span>}
                      <VerifyButton
                        verified={Boolean(e.checks?.manuallyVerified)}
                        disabled={saving}
                        onClick={() => patch({ verifyContact: { kind: "email", value: e.value, verified: !e.checks?.manuallyVerified } })}
                      />
                      <Source s={e} />
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-muted">None published</p>
              )}
            </section>

            <section>
              <h3 className="mb-2 font-semibold">Phones</h3>
              {lead.phones?.length || lead.whatsapp?.length ? (
                <ul className="space-y-1">
                  {lead.phones?.map((p, i) => (
                    <li key={`p${i}`}>
                      <span className="font-medium">{p.e164 || p.raw}</span>
                      {p.e164 && p.raw && p.raw.replace(/\D/g, "") !== p.e164.replace(/\D/g, "") && (
                        <span className="ml-2 text-xs text-muted">written as {p.raw}</span>
                      )}
                      <span className={`ml-2 text-xs ${phoneFormatValid(p) ? "text-muted" : "text-warn"}`}>
                        {phoneFormatValid(p) ? "valid number format" : "not a valid number"}
                      </span>
                      <VerifyButton
                        verified={Boolean(p.checks?.manuallyVerified)}
                        disabled={saving}
                        onClick={() =>
                          patch({ verifyContact: { kind: "phone", value: (p.e164 || p.raw)!, verified: !p.checks?.manuallyVerified } })
                        }
                      />
                      <Source s={p} />
                    </li>
                  ))}
                  {lead.whatsapp?.map((w, i) => (
                    <li key={`w${i}`}>
                      WhatsApp: <ExtLink href={w.url}>{w.e164 || w.url}</ExtLink>
                      <Source s={w} />
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-muted">None published</p>
              )}
            </section>

            <section>
              <h3 className="mb-2 font-semibold">Social</h3>
              {SOCIAL_KEYS.some((k) => lead.socials?.[k]?.url) ? (
                <ul className="space-y-1">
                  {SOCIAL_KEYS.filter((k) => lead.socials?.[k]?.url).map((k) => (
                    <li key={k}>
                      <span className="capitalize text-muted">{k}: </span>
                      <ExtLink href={lead.socials![k]!.url}>{lead.socials![k]!.url}</ExtLink>
                      <Source s={lead.socials![k]!} />
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-muted">None linked</p>
              )}
            </section>

            {lead.externalIds?.osm && (
              <section className="space-y-1">
                {lead.externalIds?.osm && (
                  <p>
                    <ExtLink href={`https://www.openstreetmap.org/${lead.externalIds.osm}`}>View on OpenStreetMap</ExtLink>
                    <span className="ml-2 text-xs text-muted">© OpenStreetMap contributors (ODbL)</span>
                  </p>
                )}
                {lead.verifiedAt && (
                  <p className="text-xs text-muted">Contacts last checked {new Date(lead.verifiedAt).toLocaleDateString()}</p>
                )}
              </section>
            )}

            <section>
              <label htmlFor="notes" className="mb-2 block font-semibold">
                Notes
              </label>
              <textarea
                id="notes"
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
                maxLength={5000}
                rows={4}
                className="w-full rounded-md border border-line bg-surface p-2"
                placeholder="e.g. Called on Monday, interested in 10 punching bags"
              />
              <button
                onClick={() => patch({ notes })}
                disabled={saving || notes === (lead.notes ?? "")}
                className="mt-2 rounded-md bg-canvas px-4 py-1.5 font-semibold text-white hover:bg-canvas-dark disabled:opacity-60"
              >
                Save notes
              </button>
            </section>

            {msg && <p role="status" className="text-sm">{msg}</p>}

            <section className="border-t border-line pt-4">
              <button onClick={optOut} disabled={saving} className="rounded-md border border-bad px-3 py-1.5 text-bad hover:bg-bad/5 disabled:opacity-60">
                Business asked not to be contacted (opt-out)
              </button>
            </section>
          </div>
        )}
      </aside>
    </div>
  );
}
