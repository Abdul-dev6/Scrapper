"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

/** Ends every session of this account (other browsers, phones, a copied cookie), then this one. */
export default function SignOutEverywhere() {
  const router = useRouter();
  const [busy, setBusy] = useState(false);

  async function onClick() {
    if (!window.confirm("Sign out on every device, including this one?")) return;
    setBusy(true);
    await fetch("/api/auth/logout", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ everywhere: true }),
    }).catch(() => null);
    router.replace("/login");
    router.refresh();
  }

  return (
    <div className="mt-6 border-t border-line pt-4 text-sm">
      <h3 className="font-semibold">Sessions</h3>
      <p className="mt-1 text-muted">Lost a device or used a shared computer? End every session of this account.</p>
      <button
        onClick={onClick}
        disabled={busy}
        className="mt-2 rounded-md border border-bad px-3 py-1.5 text-bad hover:bg-bad/5 disabled:opacity-60"
      >
        {busy ? "Signing out…" : "Sign out on all devices"}
      </button>
    </div>
  );
}
