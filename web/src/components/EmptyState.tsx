import Link from "next/link";

export default function EmptyState({ message, action }: { message: string; action?: { href: string; label: string } }) {
  return (
    <div className="rounded-lg border border-dashed border-line bg-surface p-8 text-center">
      <p className="text-muted">{message}</p>
      {action && (
        <Link href={action.href} className="mt-4 inline-block rounded-md bg-canvas px-4 py-2 text-sm font-semibold text-white hover:bg-canvas-dark">
          {action.label}
        </Link>
      )}
    </div>
  );
}
