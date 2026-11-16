import type { Metadata } from "next";
import { connection } from "next/server";
import "@fontsource-variable/archivo/wdth.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "Lead Finder",
  description: "Find gyms, clubs and sports stores by location and collect their published contact details.",
  robots: { index: false, follow: false },
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  // Render every page per request: the Content-Security-Policy (middleware.ts) uses a fresh nonce per
  // request, and Next.js can only put that nonce on its <script> tags when the page is rendered then.
  // (A page pre-rendered at build time would carry no nonce and its scripts would be blocked.)
  await connection();
  return (
    <html lang="en">
      <body className="min-h-screen font-sans antialiased">{children}</body>
    </html>
  );
}
