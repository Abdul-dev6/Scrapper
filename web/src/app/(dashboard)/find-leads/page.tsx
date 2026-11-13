import PageHeader from "@/components/PageHeader";
import FindLeadsForm from "./find-leads-form";

export default function FindLeadsPage() {
  return (
    <>
      <PageHeader
        title="Find leads"
        description="Pick a location and the kinds of businesses you sell to. We look them up on OpenStreetMap (and optionally Google Maps), then read each official website for published contact details."
      />
      <FindLeadsForm />
    </>
  );
}
