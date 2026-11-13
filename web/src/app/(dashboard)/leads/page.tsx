import { Suspense } from "react";
import PageHeader from "@/components/PageHeader";
import LeadsBrowser from "@/components/leads/LeadsBrowser";

export default function Page() {
  return (
    <>
      <PageHeader title="All leads" description="Every business found, with where each contact detail came from. Click a name for details." />
      {/* Suspense: the browser reads the filters from the URL */}
      <Suspense fallback={<p className="text-muted">Loading leads…</p>}>
        <LeadsBrowser />
      </Suspense>
    </>
  );
}
