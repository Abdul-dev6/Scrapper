export default function PageHeader({ title, description }: { title: string; description?: string }) {
  return (
    <header className="mb-8 max-w-2xl">
      <h1 className="page-title text-3xl sm:text-4xl">{title}</h1>
      {description && <p className="mt-2 text-muted">{description}</p>}
    </header>
  );
}
