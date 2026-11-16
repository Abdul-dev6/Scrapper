"use client";

import { useState } from "react";

export default function PasswordForm() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (next !== confirm) return setMsg({ ok: false, text: "The new passwords don't match." });
    setBusy(true);
    setMsg(null);
    const res = await fetch("/api/auth/password", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ current, next }),
    }).catch(() => null);
    setBusy(false);
    if (res?.ok) {
      setCurrent("");
      setNext("");
      setConfirm("");
      return setMsg({ ok: true, text: "Password changed. You were signed out on all other devices." });
    }
    const body = await res?.json().catch(() => null);
    const field = body?.error?.details?.next?.[0];
    setMsg({ ok: false, text: field ?? body?.error?.message ?? "Couldn't change the password." });
  }

  const input = "w-full rounded-md border border-line px-3 py-1.5";
  return (
    <form onSubmit={onSubmit} className="mt-4 grid max-w-sm gap-3 text-sm">
      <h3 className="font-semibold">Change password</h3>
      <label className="grid gap-1">
        <span className="text-muted">Current password</span>
        <input type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required className={input} />
      </label>
      <label className="grid gap-1">
        <span className="text-muted">New password (at least 12 characters)</span>
        <input type="password" autoComplete="new-password" minLength={12} value={next} onChange={(e) => setNext(e.target.value)} required className={input} />
      </label>
      <label className="grid gap-1">
        <span className="text-muted">New password again</span>
        <input type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} required className={input} />
      </label>
      {msg && <p role="status" className={msg.ok ? "text-ok" : "text-bad"}>{msg.text}</p>}
      <button disabled={busy} className="justify-self-start rounded-md bg-canvas px-4 py-1.5 font-semibold text-white hover:bg-canvas-dark disabled:opacity-60">
        {busy ? "Saving…" : "Change password"}
      </button>
    </form>
  );
}
