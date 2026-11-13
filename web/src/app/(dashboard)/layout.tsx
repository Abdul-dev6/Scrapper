import Nav from "@/components/Nav";
import { requireUserPage } from "@/lib/auth";

export default async function DashboardLayout({ children }: { children: React.ReactNode }) {
  const user = await requireUserPage();
  return (
    <div className="min-h-screen md:grid md:grid-cols-[14rem_1fr]">
      <aside className="border-b border-line bg-surface p-4 md:sticky md:top-0 md:h-screen md:border-b-0 md:border-r">
        <Nav email={user.email} />
      </aside>
      <main className="px-4 py-8 sm:px-8">{children}</main>
    </div>
  );
}
