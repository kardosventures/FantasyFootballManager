"use client";

import { useEffect, useState } from "react";

import { DraftRecommendation } from "@/components/DraftRecommendation";
import { StatusPill } from "@/components/StatusPill";
import type { DraftRoom } from "@/lib/types";

const POSITIONS = ["QB", "RB", "WR", "TE", "K", "DEF"];

export function LiveDraftRoom({ initialDraft }: { initialDraft: DraftRoom }) {
  const [draft, setDraft] = useState(initialDraft);
  const [connected, setConnected] = useState(true);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    let active = true;
    const refresh = async () => {
      try {
        const response = await fetch("/manager-api/api/draft", {
          cache: "no-store",
        });
        if (!response.ok) throw new Error("draft feed unavailable");
        const next = (await response.json()) as DraftRoom;
        if (active) {
          setDraft(next);
          setConnected(true);
        }
      } catch {
        if (active) setConnected(false);
      }
    };
    void refresh();
    const timer = window.setInterval(refresh, 2000);
    const clockTimer = window.setInterval(() => setNow(Date.now()), 250);
    return () => {
      active = false;
      window.clearInterval(timer);
      window.clearInterval(clockTimer);
    };
  }, []);

  const roster = draft.recommendation?.roster_summary;
  const slotPlan = draft.slot_plan;
  const format = draft.format;
  const deadline = draft.clock?.deadline ? Date.parse(draft.clock.deadline) : undefined;
  const submitAt = draft.autopick?.submit_at
    ? Date.parse(draft.autopick.submit_at)
    : draft.clock?.auto_submit_at
      ? Date.parse(draft.clock.auto_submit_at)
      : undefined;
  const secondsRemaining = deadline == null
    ? undefined
    : Math.max(Math.ceil((deadline - now) / 1000), 0);
  const submitIn = submitAt == null
    ? undefined
    : Math.max(Math.ceil((submitAt - now) / 1000), 0);
  const plannedPlayer = draft.autopick?.candidate
    ?? draft.recommendation?.recommendation?.player_name;
  const clockLabel =
    draft.status === "complete"
      ? "Draft complete"
      : draft.on_clock
        ? secondsRemaining == null
          ? "YOU ARE ON THE CLOCK"
          : `${secondsRemaining}s left`
        : draft.picks_until_on_clock === 0
          ? (draft.status ?? "pre draft")
          : `${draft.picks_until_on_clock ?? "–"} picks away`;

  return (
    <>
      <header className="pageHeader">
        <div>
          <p className="eyebrow">Autonomous draft manager</p>
          <h1>Draft room</h1>
          <p>
            A whole-roster expert model reasons over the verified live board,
            every team build, league scoring, tiers, and turn dynamics. Playwright
            submits only that model&apos;s validated choice.
          </p>
        </div>
        <div className="draftConnection">
          <span className={connected ? "liveDot" : "liveDot disconnected"} />
          <span>{connected ? "Live · 2 sec" : "Reconnecting"}</span>
          <StatusPill
            status={draft.on_clock ? "on clock" : (draft.status ?? "pre draft")}
          />
        </div>
      </header>
      {draft.on_clock && (
        <div className="onClockBanner">
          <strong>
            {plannedPlayer ? `Planned pick: ${plannedPlayer}` : "Codex is choosing your player"}
          </strong>
          <span>
            {draft.autopick?.state === "scheduled" && submitIn != null
              ? draft.clock?.submit_policy === "mock_delay"
                ? `Mock auto-submit begins in ${submitIn}s (${draft.clock.auto_submit_delay_seconds ?? 0}s configured delay).`
                : `Auto-submit begins in ${submitIn}s, at approximately ${draft.clock?.auto_submit_seconds_remaining ?? 15}s remaining.`
              : draft.autopick?.state === "queued"
                ? "Playwright is submitting the displayed player now."
                : draft.autopick?.message ?? "Automatic pick preflight is running."}
          </span>
          <small>
            To course-correct, make a different pick in Sleeper before auto-submit begins.
            The pending command rechecks the live board before it can click.
          </small>
        </div>
      )}
      <section className="draftHero">
        <div>
          <small>Your slot</small>
          <strong>{draft.owner_slot ?? "–"}</strong>
        </div>
        <div>
          <small>Next pick</small>
          <strong>
            {draft.next_owner_pick ? `#${draft.next_owner_pick}` : "–"}
          </strong>
        </div>
        <div>
          <small>Distance</small>
          <strong>{clockLabel}</strong>
        </div>
        <div>
          <small>Room</small>
          <strong>
            {format ? `${format.teams} teams · ${format.rounds} rd` : "Waiting"}
          </strong>
        </div>
      </section>
      <div className="contentGrid">
        <section className="panel spanTwo">
          <div className="panelHeader">
            <div>
              <p className="eyebrow">Decision board</p>
              <h2>
                {draft.recommendation?.decision_source === "expert_model"
                  ? "Expert whole-roster choice + fallbacks"
                  : "Grounding preview — expert decides on the clock"}
              </h2>
            </div>
            <span className="freshness">
              ECR {draft.provenance?.latest_source_date ?? "loading"}
            </span>
          </div>
          {draft.status === "complete" ? (
            <div className="emptyState">
              <strong>Draft complete</strong>
              <span>
                Start another mock to rehearse the live recommendations.
              </span>
            </div>
          ) : (
            <DraftRecommendation
              recommendation={draft.recommendation?.recommendation}
              fallbacks={draft.recommendation?.fallbacks}
            />
          )}
          {draft.recommendation?.expert_decision && (
            <>
              <div className="callout">
                <strong>
                  {(draft.recommendation.expert_decision.strategy_state
                    ?? draft.recommendation.expert_decision.roster_strategy
                  ).replaceAll("_", " ")}
                </strong>
                <span>{draft.recommendation.expert_decision.fit_summary}</span>
              </div>
              {(draft.recommendation.expert_decision.roster_risk
                || draft.recommendation.expert_decision.next_two_turn_plan) && (
                <p className="methodNote">
                  {draft.recommendation.expert_decision.roster_risk && (
                    <><strong>Roster risk:</strong>{" "}
                      {draft.recommendation.expert_decision.roster_risk}{" "}</>
                  )}
                  {draft.recommendation.expert_decision.next_two_turn_plan && (
                    <><strong>Next two turns:</strong>{" "}
                      {draft.recommendation.expert_decision.next_two_turn_plan}</>
                  )}
                </p>
              )}
            </>
          )}
          <p className="methodNote">
            The programmatic layer supplies exact state and evidence. It does not
            make the executable selection. The expert model optimizes the assembled
            roster and may override the displayed grounding order when team fit,
            scarcity, opponents, or the next turn justify it.
          </p>
        </section>
        <section className="panel">
          <div className="panelHeader">
            <div>
              <p className="eyebrow">Your build</p>
              <h2>Roster construction</h2>
            </div>
            <span className="freshness">
              {roster?.remaining_picks ?? "–"} picks left
            </span>
          </div>
          <div className="rosterGrid">
            {POSITIONS.map((position) => (
              <div key={position}>
                <span>{position}</span>
                <strong>{roster?.counts[position] ?? 0}</strong>
                <small>target {roster?.targets[position] ?? 0}</small>
              </div>
            ))}
          </div>
        </section>
        {slotPlan && (
          <section className="panel spanTwo">
            <div className="panelHeader">
              <div>
                <p className="eyebrow">Pick {slotPlan.owner_slot} plan</p>
                <h2>{format?.rounds ?? slotPlan.rounds}-round market baseline</h2>
              </div>
              <span className="freshness">
                ADP {draft.provenance?.market_source_date ?? "loading"}
              </span>
            </div>
            <div className="planGrid">
              {slotPlan.projected_roster.map((pick) => (
                <div key={`${pick.round}-${pick.player_id}`}>
                  <span>
                    R{pick.round} · #{pick.pick}
                  </span>
                  <strong>{pick.player_name}</strong>
                  <small>
                    {pick.position} · {pick.team || "FA"}
                    {pick.market_adp ? ` · ADP ${pick.market_adp.toFixed(1)}` : ""}
                  </small>
                  <small>
                    {pick.available_at_pick_probability != null
                      ? `${Math.round(pick.available_at_pick_probability * 100)}% market availability`
                      : "Availability unavailable"}
                    {pick.injury_status ? ` · ${pick.injury_status}` : ""}
                  </small>
                </div>
              ))}
            </div>
            <p className="methodNote">{slotPlan.strategy}</p>
          </section>
        )}
        <section className="panel spanTwo">
          <div className="panelHeader">
            <div>
              <p className="eyebrow">Pick stream</p>
              <h2>Most recent selections</h2>
            </div>
            <span className="freshness">{draft.pick_count ?? 0} complete</span>
          </div>
          <div className="recentPicks">
            {(draft.recent_picks ?? [])
              .slice()
              .reverse()
              .map((pick) => (
                <div
                  className={pick.is_mine ? "mine" : ""}
                  key={`${pick.pick_no}-${pick.player_id}`}
                >
                  <span>#{pick.pick_no ?? "–"}</span>
                  <strong>{pick.player_name || "Unknown player"}</strong>
                  <small>
                    {pick.position} · {pick.team || "FA"}
                    {pick.is_mine ? " · YOUR PICK" : ""}
                  </small>
                </div>
              ))}
          </div>
        </section>
        <section className="panel">
          <div className="panelHeader">
            <div>
              <p className="eyebrow">Automation</p>
              <h2>{draft.autopick?.state === "blocked" ? "Not armed" : "Auto-pick armed"}</h2>
            </div>
          </div>
          <p className="methodNote">
            Expert: {draft.expert_manager?.provider === "codex_cli"
              ? "Codex subscription"
              : "OpenAI API"} · {draft.expert_manager?.model ?? "not configured"} · {" "}
            {draft.expert_manager?.state?.replaceAll("_", " ") ?? "waiting"}
            {draft.expert_manager?.decision
              ? ` · ${Math.round(draft.expert_manager.decision.latency_ms)} ms`
              : ""}
          </p>
          <div className="checkList">
            {(draft.execution_blockers?.length
              ? draft.execution_blockers
              : [draft.autopick?.message ?? "Waiting for draft monitor"]
            ).map((message) => (
              <div key={message}>
                <span
                  className={
                    draft.execution_blockers?.length
                      ? "checkMark warning"
                      : "checkMark"
                  }
                >
                  {draft.execution_blockers?.length ? "!" : "✓"}
                </span>
                <span>
                  <strong>{message}</strong>
                  <small>
                    {draft.execution_blockers?.length
                      ? "No click will occur until resolved"
                      : "League, clock, player, and pick stream rechecked before one click"}
                  </small>
                </span>
              </div>
            ))}
          </div>
          <p className="methodNote">
            Updated{" "}
            {draft.generated_at
              ? new Date(draft.generated_at).toLocaleTimeString()
              : "not yet"}
          </p>
        </section>
      </div>
    </>
  );
}
