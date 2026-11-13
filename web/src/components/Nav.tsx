"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

const LINKS = [
  { href: "/dashboard", label: "Dashboard" },
  { href: "/find-leads", label: "Find leads" },
  { href: "/leads", label: "All leads" },
  { href: "/saved", label: "Saved leads" },
  { href: "/jobs", label: "Scraping jobs" },
  { href: "/settings", label: "Settings" },
];

export default function Nav({ email }: { email: string }) {
  const pathname = usePathname();
  const router = useRouter();

  async function signOut() {
    await fetch("/api/auth/logout", { method: "POST" });
    router.replace("/login");
    router.refresh();
  }

  return (
    <nav aria-label="Main" className="flex h-full flex-col gap-1">
      <p className="page-title mb-6 px-3 text-xl">Lead Finder</p>
      {LINKS.map((l) => {
        const active = pathname === l.href || pathname.startsWith(`${l.href}/`);
        return (
          <Link key={l.href} href={l.href} aria-current={active ? "page" : undefined}
            className={`rounded-md px-3 py-2 text-sm ${active ? "bg-canvas font-semibold text-white" : "text-ink hover:bg-line/50"}`}>
            {l.label}
          </Link>
        );
      })}
      <div className="mt-auto border-t border-line px-3 pt-4 text-sm">
        <p className="truncate text-muted" title={email}>{email}</p>
        <button onClick={signOut} className="mt-1 font-medium text-canvas hover:underline">Sign out</button>
      </div>
    </nav>
  );
}
