import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function SchedulePage() {
  const calendar = await api.calendar();
  return <><header className="pageHeader"><div><p className="eyebrow">America/Denver</p><h1>Next 14 days</h1><p>Draft, waiver, lineup, kickoff, escalation, and verification windows.</p></div></header><section className="panel full"><div className="timeline">{calendar.events.map((event, index) => <article key={index}><span className="timelineMarker">{String(event.priority ?? "P2")}</span><div><h2>{String(event.title)}</h2><p>{new Date(String(event.starts_at)).toLocaleString("en-US", { dateStyle: "full", timeStyle: "short", timeZone: "America/Denver" })} Mountain</p></div></article>)}</div>{!calendar.events.length && <div className="emptyState"><strong>No operational events in this window</strong><span>The calendar is regenerated from the digital twin on every bootstrap.</span></div>}</section></>;
}
