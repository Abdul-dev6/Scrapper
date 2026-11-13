import PageHeader from "@/components/PageHeader";
import JobsTable from "./jobs-table";

export default function JobsPage() {
  return (
    <>
      <PageHeader title="Scraping jobs" description="Every search you've run, with live progress while it works." />
      <JobsTable />
    </>
  );
}
