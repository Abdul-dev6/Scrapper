import PageHeader from "@/components/PageHeader";
import { requireUserPage } from "@/lib/auth";
import OptOutList from "./opt-out-list";
import PasswordForm from "./password-form";
import SignOutEverywhere from "./sign-out-everywhere";

export default async function Page() {
  const user = await requireUserPage();
  return (
    <>
      <PageHeader title="Settings" description="Your account and your opt-out list." />
      <div className="max-w-3xl space-y-8">
        <section className="rounded-lg border border-line bg-surface p-6">
          <h2 className="mb-1 text-lg font-semibold">Account</h2>
          <p className="text-sm text-muted">Signed in as {user.email}</p>
          <PasswordForm />
          <SignOutEverywhere />
        </section>
        <section className="rounded-lg border border-line bg-surface p-6">
          <h2 className="mb-1 text-lg font-semibold">Opt-out list</h2>
          <p className="mb-4 text-sm text-muted">
            Businesses that asked not to be contacted. Matching leads are deleted now, skipped by future searches and never
            exported. Values are stored scrambled (as a hash), so this list can&apos;t be read back, only matched.
          </p>
          <OptOutList />
        </section>
      </div>
    </>
  );
}
