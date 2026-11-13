import Link from "next/link";
import { Suspense } from "react";
import LoginForm from "./login-form";

export default function LoginPage() {
  return (
    <main className="grid min-h-screen place-items-center px-4">
      <div className="w-full max-w-sm">
        <h1 className="page-title text-4xl">Lead Finder</h1>
        <p className="mt-2 text-muted">Sign in to find gyms, clubs and sports stores near any city.</p>
        <Suspense>
          <LoginForm />
        </Suspense>
        <p className="mt-4 text-center text-sm text-muted">
          New here?{" "}
          <Link href="/signup" className="font-semibold text-canvas hover:underline">Create an account</Link>
        </p>
      </div>
    </main>
  );
}
