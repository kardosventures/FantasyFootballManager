import Link from "next/link";
import { DraftRecommendation } from "@/components/DraftRecommendation";
import { StatusPill } from "@/components/StatusPill";
import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function Overview() {
  const [readiness, actions, draft, calendar, sources] = await Promise.all([api.readiness(), api.actions(), api.draft(), api.calendar(), api.sources()]);
  const capabilities = readiness.capabilities ?? {};
  const healthySources = sources.filter(source => source.status === "healthy").length;
  const pending = actions.filter(action => !["verified", "failed", "blocked"].includes(action.status));
  const orderedChecks = [...readiness.checks].sort((left, right) => Number(right.status === "block") - Number(left.status === "block"));
  return <>
    <header className="pageHeader"><div><p className="eyebrow">2026 · Nederlanders Fantasy League</p><h1>Operations command center</h1><p>Everything the system knows, plans, and is allowed to do for Jim.ai.</p></div><StatusPill status={readiness.ready ? "ready" : "blocked"}/></header>
    <section className="metricGrid" aria-label="Operational status">
      <article className="metric"><small>Waiver decisions</small><strong>{capabilities.waiver_decision_ready ? "Ready" : "Blocked"}</strong><span>season-long roster reasoning</span></article>
      <article className="metric"><small>Action queue</small><strong>{pending.length}</strong><span>{pending.filter(action => action.approval_required).length} need approval</span></article>
      <article className="metric"><small>Source health</small><strong>{healthySources}/{sources.length || 2}</strong><span>public feeds healthy</span></article>
      <article className="metric"><small>Waiver submission</small><strong>{capabilities.waiver_execution_ready ? "Enabled" : capabilities.waiver_submission_qualified ? "Qualified" : capabilities.waiver_selector_qualified ? "Selector qualified" : "Unqualified"}</strong><span>{capabilities.waiver_execution_ready ? "automatic claims live" : capabilities.waiver_submission_qualified ? "verified; automation disabled" : capabilities.waiver_selector_qualified ? "controlled submission still required" : "automatic claims disabled"}</span></article>
      <article className="metric"><small>Fallback claims</small><strong>{capabilities.waiver_fallback_execution_ready ? "Enabled" : capabilities.waiver_fallback_qualified ? "Qualified" : "Gated"}</strong><span>ordered season-long claim tree</span></article>
      <article className="metric"><small>Free agents</small><strong>{capabilities.free_agent_execution_ready ? "Enabled" : capabilities.free_agent_submission_qualified ? "Qualified" : "Gated"}</strong><span>{capabilities.sequential_free_agent_execution_ready ? "verified move, refresh, then re-plan" : "single immediate add/drop only"}</span></article>
      <article className="metric"><small>IR management</small><strong>{capabilities.ir_applicability === "not_applicable" ? "Not available" : capabilities.ir_execution_ready ? "Enabled" : capabilities.ir_decision_ready ? "Analysis ready" : "Gated"}</strong><span>{capabilities.ir_applicability === "not_applicable" ? "league has no IR slots" : "exact reserve-state changes"}</span></article>
      <article className="metric"><small>Trades</small><strong>{capabilities.trade_execution_ready ? "Enabled" : capabilities.trade_submission_qualified ? "Qualified" : capabilities.trade_intelligence_ready ? "Analysis ready" : "Gated"}</strong><span>{capabilities.trade_execution_ready ? `${capabilities.transaction_guard_mode === "trust_then_verify" ? "trust-then-verify" : "qualified"}; high-upside only` : "exact-offer execution remains gated"}</span></article>
      <article className="metric"><small>Outcome loop</small><strong>{capabilities.acquisition_reconciliation_ready ? "Ready" : "Waiting"}</strong><span>results, fallbacks, and alerts</span></article>
    </section>
    <div className="contentGrid">
      <section className="panel spanTwo"><div className="panelHeader"><div><p className="eyebrow">Draft room</p><h2>Automatic choice for the next pick</h2></div><Link href="/draft">Open room →</Link></div><DraftRecommendation recommendation={draft.recommendation?.recommendation} fallbacks={draft.recommendation?.fallbacks}/>{draft.execution_blockers?.length ? <div className="callout warning"><strong>Auto-pick locked</strong><span>{draft.execution_blockers[0]}</span></div> : <div className="callout"><strong>Auto-pick armed</strong><span>{draft.autopick?.message ?? "Waiting for the draft monitor"}</span></div>}</section>
      <section className="panel"><div className="panelHeader"><div><p className="eyebrow">Safety gates</p><h2>Readiness</h2></div></div><div className="checkList">{orderedChecks.slice(0, 7).map(check => <div key={check.id}><span className={`checkMark ${check.status}`}>{check.status === "pass" ? "✓" : "!"}</span><span><strong>{check.summary}</strong><small>{check.status === "pass" ? "Confirmed from live data" : "Blocks unattended execution"}</small></span></div>)}</div></section>
      <section className="panel spanTwo"><div className="panelHeader"><div><p className="eyebrow">Action outbox</p><h2>Next decisions</h2></div><Link href="/actions">View all →</Link></div>{pending.length ? <div className="actionRows">{pending.slice(0, 4).map(action => <div className="actionRow" key={action.id}><span className={`priority p${action.priority}`}>P{action.priority}</span><span><strong>{action.title}</strong><small>{action.primary_reason}</small></span><StatusPill status={action.status}/></div>)}</div> : <div className="emptyState"><strong>No queued changes</strong><span>The monitor will place reasoned, policy-checked actions here.</span></div>}</section>
      <section className="panel"><div className="panelHeader"><div><p className="eyebrow">Calendar</p><h2>Next 14 days</h2></div><Link href="/schedule">Details →</Link></div>{calendar.events.length ? calendar.events.slice(0, 3).map((event, index) => <div className="calendarItem" key={index}><span>{String(event.priority ?? "P2")}</span><div><strong>{String(event.title)}</strong><small>{new Date(String(event.starts_at)).toLocaleString("en-US", { dateStyle: "medium", timeStyle: "short", timeZone: "America/Denver" })} MT</small></div></div>) : <div className="emptyState compact"><strong>No events in range</strong><span>Draft and game windows appear after sync.</span></div>}</section>
    </div>
  </>;
}
