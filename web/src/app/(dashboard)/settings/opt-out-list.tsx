"use client";

import { useCallback, useEffect, useState } from "react";

type Entry = { _id: string; type: "email" | "phone" | "domain"; reason?: string; addedAt: string };
const PLACEHOLDER = { email: "info@examplegym.co.uk", phone: "+44 20 7946 0000", domain: "examplegym.co.uk" };
const TYPE_LABEL = { email: "Email", phone: "Phone", domain: "Website / domain" };

export default function OptOutList() {
  const [entries, setEntries] = useState<Entry[] | null>(null);
  const [type, setType] = useState<Entry["type"]>("email");
  const [value, setValue] = useState("");
  const [reason, setReason] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const res = await fetch("/api/suppression", { cache: "no-store" }).catch(() => null);
    if (res?.ok) setEntries((await res.json()).entries);
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    const res = await fetch("/api/suppression", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ type, value, reason: reason || undefined }),
    }).catch(() => null);
    setBusy(false);
    const body = await res?.json().catch(() => null);
    if (!res?.ok) return setMsg({ ok: false, text: body?.error?.message ?? "Couldn't add it." });
    setValue("");
    setReason("");
    const n = body.removedLeads as number;
    setMsg({ ok: true, text: `Added.${n ? ` ${n} matching ${n === 1 ? "lead was" : "leads were"} deleted.` : ""}` });
    load();
  }

  async function remove(entry: Entry) {
    if (!window.confirm("Remove this entry? Future searches may find this business again.")) return;
    const res = await fetch(`/api/suppression/${entry._id}`, { method: "DELETE" }).catch(() => null);
    if (res?.ok) load();
    else setMsg({ ok: false, text: "Couldn't remove it." });
  }

  return (
    <div className="space-y-4 text-sm">
      <form onSubmit={add} className="flex flex-wrap items-end gap-3">
        <label className="grid gap-1">
          <span className="text-muted">Type</span>
          <select value={type} onChange={(e) => setType(e.target.value as Entry["type"])} className="rounded-md border border-line bg-surface px-2 py-1.5">
            {Object.entries(TYPE_LABEL).map(([k, l]) => (
              <option key={k} value={k}>{l}</option>
            ))}
          </select>
        </label>
        <label className="grid min-w-56 flex-1 gap-1">
          <span className="text-muted">{type === "phone" ? "Phone, with country code" : TYPE_LABEL[type]}</span>
          <input value={value} onChange={(e) => setValue(e.target.value)} placeholder={PLACEHOLDER[type]} required maxLength={254} className="rounded-md border border-line px-3 py-1.5" />
        </label>
        <label className="grid min-w-40 gap-1">
          <span className="text-muted">Note (optional)</span>
          <input value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} placeholder="e.g. replied: no thanks" className="rounded-md border border-line px-3 py-1.5" />
        </label>
        <button disabled={busy} className="rounded-md bg-canvas px-4 py-1.5 font-semibold text-white hover:bg-canvas-dark disabled:opacity-60">
          {busy ? "Adding…" : "Add"}
        </button>
      </form>
      {msg && <p role="status" className={msg.ok ? "text-ok" : "text-bad"}>{msg.text}</p>}

      {entries === null ? (
        <p className="text-muted">Loading…</p>
      ) : entries.length === 0 ? (
        <p className="text-muted">Nobody on the list yet.</p>
      ) : (
        <ul className="divide-y divide-line rounded-md border border-line">
          {entries.map((e) => (
            <li key={e._id} className="flex items-center justify-between gap-3 px-3 py-2">
              <span>
                <span className="font-medium">{TYPE_LABEL[e.type]}</span>
                {e.reason && <span className="ml-2 text-muted">{e.reason}</span>}
                <span className="ml-2 text-xs text-muted">{new Date(e.addedAt).toLocaleDateString()}</span>
              </span>
              <button onClick={() => remove(e)} className="rounded-md border border-line px-2 py-1 text-xs hover:bg-chalk">
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
