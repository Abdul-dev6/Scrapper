import Link from "next/link";
import SignupForm from "./signup-form";

export default function SignupPage() {
  return (
    <main className="grid min-h-screen place-items-center px-4">
      <div className="w-full max-w-sm">
        <h1 className="page-title text-4xl">Lead Finder</h1>
        <p className="mt-2 text-muted">Create an account to start finding leads.</p>
        <SignupForm />
        <p className="mt-4 text-center text-sm text-muted">
          Already have an account?{" "}
          <Link href="/login" className="font-semibold text-canvas hover:underline">Sign in</Link>
        </p>
      </div>
    </main>
  );
}
