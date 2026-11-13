import EmptyState from "@/components/EmptyState";
import PageHeader from "@/components/PageHeader";
import { requireUserPage } from "@/lib/auth";
import { connectDB } from "@/lib/db";
import { Lead, ScrapingJob } from "@/models";

export const dynamic = "force-dynamic";

export default async function DashboardPage() {
  const user = await requireUserPage();
  await connectDB();
  const [total, withEmail, withPhone, saved, openJobs] = await Promise.all([
    Lead.countDocuments({ ownerId: user.id }),
    Lead.countDocuments({ ownerId: user.id, "emails.0": { $exists: true } }),
    Lead.countDocuments({ ownerId: user.id, "phones.0": { $exists: true } }),
    Lead.countDocuments({ ownerId: user.id, saved: true }),
    ScrapingJob.countDocuments({ ownerId: user.id, status: { $in: ["queued", "running"] } }),
  ]);

  const stats = [
    { label: "Businesses found", value: total },
    { label: "With an email", value: withEmail },
    { label: "With a phone number", value: withPhone },
    { label: "Saved", value: saved },
  ];

  return (
    <>
      <PageHeader title="Dashboard" description="Your leads at a glance." />
      {total === 0 ? (
        <EmptyState
          message={openJobs ? `${openJobs} search running. Leads appear here as they're found.` : "No leads yet. Search a city to find gyms, clubs and sports stores."}
          action={openJobs ? { href: "/jobs", label: "View jobs" } : { href: "/find-leads", label: "Find leads" }}
        />
      ) : (
        <dl className="grid max-w-4xl grid-cols-2 gap-px overflow-hidden rounded-lg border border-line bg-line lg:grid-cols-4">
          {stats.map((s) => (
            <div key={s.label} className="bg-surface p-5">
              <dt className="text-sm text-muted">{s.label}</dt>
              <dd className="mt-1 text-3xl font-bold tabular-nums">{s.value.toLocaleString()}</dd>
            </div>
          ))}
        </dl>
      )}
    </>
  );
}
