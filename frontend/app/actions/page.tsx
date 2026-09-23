import { ApprovalButton } from "@/components/ApprovalButton";
import { ActionControl } from "@/components/ActionControl";
import { StatusPill } from "@/components/StatusPill";
import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function ActionsPage() {
  const actions = await api.actions();
  return <><header className="pageHeader"><div><p className="eyebrow">Durable outbox</p><h1>Action queue</h1><p>Every proposed change, its evidence, safety status, attempt, and verification result.</p></div></header><section className="panel full">{actions.length ? <div className="actionCards">{actions.map(action => <article className="actionCard" id={`action-${action.id}`} key={action.id}><div className="actionCardTop"><span className={`priority p${action.priority}`}>P{action.priority}</span><div><h2>{action.title}</h2><p>{action.primary_reason}</p></div><StatusPill status={action.status}/></div><dl><div><dt>Confidence</dt><dd>{Math.round(action.confidence * 100)}%</dd></div><div><dt>Policy</dt><dd>{action.decision_policy_version}</dd></div><div><dt>Protection</dt><dd>{action.drop_protection_tier ?? "n/a"}</dd></div></dl><pre>{JSON.stringify(action.exact_action, null, 2)}</pre><ActionControl action={action}/>{action.status === "approval_required" ? <ApprovalButton actionId={action.id}/> : null}</article>)}</div> : <div className="emptyState"><strong>No actions recorded</strong><span>Nothing will execute until a deterministic engine proposes an eligible change.</span></div>}</section></>;
}
