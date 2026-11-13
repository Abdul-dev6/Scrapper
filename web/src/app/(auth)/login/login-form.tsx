"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";

export default function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setPending(true);
    setError(null);
    const form = new FormData(e.currentTarget);
    const res = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: form.get("email"), password: form.get("password") }),
    }).catch(() => null);

    if (res?.ok) {
      // only allow same-site relative paths, so ?next= can't redirect to another website
      const next = params.get("next");
      router.replace(next && next.startsWith("/") && !next.startsWith("//") ? next : "/dashboard");
      router.refresh();
      return;
    }
    const data = await res?.json().catch(() => null);
    setError(data?.error?.message ?? "Could not reach the server. Check your connection and try again.");
    setPending(false);
  }

  return (
    <form onSubmit={onSubmit} className="mt-8 space-y-4 rounded-lg border border-line bg-surface p-6">
      <label className="block">
        <span className="text-sm font-medium">Email</span>
        <input name="email" type="email" required autoComplete="email"
          className="mt-1 w-full rounded-md border border-line px-3 py-2" />
      </label>
      <label className="block">
        <span className="text-sm font-medium">Password</span>
        <input name="password" type="password" required autoComplete="current-password"
          className="mt-1 w-full rounded-md border border-line px-3 py-2" />
      </label>
      {params.get("created") === "1" && !error && (
        <p role="status" className="text-sm text-ok">Account created. Sign in with your new email and password.</p>
      )}
      {error && <p role="alert" className="text-sm text-bad">{error}</p>}
      <button type="submit" disabled={pending}
        className="w-full rounded-md bg-canvas px-4 py-2 font-semibold text-white hover:bg-canvas-dark disabled:opacity-60">
        {pending ? "Signing in…" : "Sign in"}
      </button>
    </form>
  );
}
