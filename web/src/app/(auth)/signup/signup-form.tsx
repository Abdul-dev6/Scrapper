"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

export default function SignupForm() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    const form = new FormData(e.currentTarget);
    const password = String(form.get("password") ?? "");
    if (password !== form.get("confirm")) {
      setError("The two passwords don't match.");
      return;
    }

    setPending(true);
    const res = await fetch("/api/auth/signup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: form.get("email"), password }),
    }).catch(() => null);

    if (res?.ok) {
      router.replace("/login?created=1");
      return;
    }
    const data = await res?.json().catch(() => null);
    const fieldErrors = data?.error?.details ? Object.values(data.error.details).flat().join(" ") : "";
    setError(fieldErrors || data?.error?.message || "Could not reach the server. Check your connection and try again.");
    setPending(false);
  }

  return (
    <form onSubmit={onSubmit} className="mt-8 space-y-4 rounded-lg border border-line bg-surface p-6">
      <label className="block">
        <span className="text-sm font-medium">Email</span>
        <input name="email" type="email" required maxLength={254} autoComplete="email"
          className="mt-1 w-full rounded-md border border-line px-3 py-2" />
      </label>
      <label className="block">
        <span className="text-sm font-medium">Password</span>
        <span className="block text-xs text-muted">At least 12 characters.</span>
        <input name="password" type="password" required minLength={12} maxLength={72} autoComplete="new-password"
          className="mt-1 w-full rounded-md border border-line px-3 py-2" />
      </label>
      <label className="block">
        <span className="text-sm font-medium">Confirm password</span>
        <input name="confirm" type="password" required minLength={12} maxLength={72} autoComplete="new-password"
          className="mt-1 w-full rounded-md border border-line px-3 py-2" />
      </label>
      {error && <p role="alert" className="text-sm text-bad">{error}</p>}
      <button type="submit" disabled={pending}
        className="w-full rounded-md bg-canvas px-4 py-2 font-semibold text-white hover:bg-canvas-dark disabled:opacity-60">
        {pending ? "Creating account…" : "Create account"}
      </button>
    </form>
  );
}
