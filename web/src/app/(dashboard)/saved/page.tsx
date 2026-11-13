import { Suspense } from "react";
import PageHeader from "@/components/PageHeader";
import LeadsBrowser from "@/components/leads/LeadsBrowser";

export default function Page() {
  return (
    <>
      <PageHeader title="Saved leads" description="Businesses you've marked ★ to follow up." />
      <Suspense fallback={<p className="text-muted">Loading leads…</p>}>
        <LeadsBrowser savedOnly />
      </Suspense>
    </>
  );
}
