"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

import { StatusPill } from "@/components/StatusPill";
import type { TrashTalkStatus } from "@/lib/types";

export function TrashTalkControl({ initial }: { initial: TrashTalkStatus }) {
  const router = useRouter();
  const [state, setState] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const setEnabled = async (enabled: boolean) => {
    setBusy(true);
    setMessage("");
    try {
      const csrfResponse = await fetch("/manager-api/api/csrf", { cache: "no-store" });
      if (!csrfResponse.ok) throw new Error("Could not create a secure control token");
      const { token } = await csrfResponse.json();
      const response = await fetch(`/manager-api/api/trash-talk/${enabled ? "resume" : "pause"}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
        body: JSON.stringify({ reason: enabled ? null : "Paused from manager dashboard" }),
      });
      if (!response.ok) throw new Error("Trash-talk control update failed");
      const result = await response.json();
      setState((current) => ({ ...current, enabled: result.enabled, paused_reason: result.paused_reason }));
      setMessage(enabled && !state.configured ? "Control is ready, but TRASH_TALK_ENABLED is still false." : enabled ? "Autonomous posting resumed." : "Autonomous posting paused immediately.");
      router.refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Control update failed");
    } finally {
      setBusy(false);
    }
  };

  const recent = state.posts.slice(0, 6);
  return <section className="panel full">
    <div className="panelHeader"><div><p className="eyebrow">League entertainment</p><h2>Jim.ai trash talk</h2></div><StatusPill status={state.enabled ? "ready" : state.configured ? "paused" : "disabled"}/></div>
    <div className="metricGrid">
      <article className="metric"><small>This week</small><strong>{state.weekly_count}/{state.weekly_limit}</strong><span>target {state.weekly_target}; never forced</span></article>
      <article className="metric"><small>Posting</small><strong>{state.enabled ? "Automatic" : "Stopped"}</strong><span>{state.paused_reason ?? "PG-13 fantasy outcomes only"}</span></article>
      <article className="metric"><small>Images</small><strong>{state.image_upload_configured ? "Ready" : "Text only"}</strong><span>original robot scorecards</span></article>
      <article className="metric"><small>Opt-outs</small><strong>{state.opted_out_roster_ids.length}</strong><span>team-level anti-pile-on control</span></article>
    </div>
    <div className="refreshControl"><div><strong>Immediate kill switch</strong><small>Pausing does not affect operational Slack alerts.</small></div><button type="button" onClick={() => setEnabled(!state.enabled)} disabled={busy}>{busy ? "Updating…" : state.enabled ? "Pause trash talk" : "Resume trash talk"}</button>{message ? <small className="controlMessage" role="status">{message}</small> : null}</div>
    {recent.length ? <div className="waiverList">{recent.map((post) => <article key={post.id}><div><strong>{post.trigger_type.replaceAll("_", " ")} · {post.target_label ?? "league"}</strong><small>{new Date(post.created_at).toLocaleString()} · {post.status} · {post.format}</small><p>{post.message ?? post.suppression_reason ?? "No copy generated"}</p></div></article>)}</div> : <div className="emptyState compact"><strong>No trash talk yet</strong><span>The system waits for a specific, verified fantasy event.</span></div>}
  </section>;
}
