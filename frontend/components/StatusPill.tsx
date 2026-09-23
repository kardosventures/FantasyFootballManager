export function StatusPill({ status }: { status?: string | null }) {
  const display = status || "unknown";
  const normalized = display.toLowerCase().replaceAll(" ", ".");
  return <span className={`pill pill-${normalized}`}>{display.replaceAll("_", " ")}</span>;
}
