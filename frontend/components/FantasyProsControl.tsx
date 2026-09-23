"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { StatusPill } from "@/components/StatusPill";
import type { FantasyProsQuota } from "@/lib/types";

function duration(seconds: number): string {
  const safe = Math.max(Math.ceil(seconds), 0);
  const minutes = Math.floor(safe / 60);
  const remainder = safe % 60;
  return minutes ? `${minutes}m ${remainder}s` : `${remainder}s`;
}

export function FantasyProsControl({
  initial,
  averageDisagreement,
  rosterProjectionCount,
}: {
  initial: FantasyProsQuota;
  averageDisagreement: number | null;
  rosterProjectionCount: number;
}) {
  const router = useRouter();
  const [quota, setQuota] = useState(initial);
  const [cooldown, setCooldown] = useState(initial.cooldown_remaining_seconds ?? 0);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  useEffect(() => {
    if (cooldown <= 0) return;
    const timer = window.setInterval(() => setCooldown((value) => Math.max(value - 1, 0)), 1000);
    return () => window.clearInterval(timer);
  }, [cooldown]);

  const refresh = async () => {
    setBusy(true);
    setMessage("Refreshing our roster and opponent projections…");
    try {
      const csrfResponse = await fetch("/manager-api/api/csrf", { cache: "no-store" });
      if (!csrfResponse.ok) throw new Error("Could not create a secure refresh token");
      const { token } = await csrfResponse.json();
      const response = await fetch("/manager-api/api/fantasypros-refresh", {
        method: "POST",
        headers: { "X-CSRF-Token": token },
      });
      const result = (await response.json()) as FantasyProsQuota & { detail?: string };
      if (!response.ok) throw new Error(result.detail ?? "FantasyPros refresh failed");
      setQuota(result);
      setCooldown(result.cooldown_remaining_seconds ?? 0);
      setMessage(
        result.refresh_status === "cooldown"
          ? "Recent data is still inside the safety cooldown; no quota was spent."
          : `Refresh complete. ${result.requests_this_refresh ?? 0} requests used.`,
      );
      router.refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "FantasyPros refresh failed");
    } finally {
      setBusy(false);
    }
  };

  const canRefresh = Boolean(quota.configured && (quota.total_remaining ?? 0) > 0 && cooldown === 0);
  const coverage = Math.round((quota.target_player_coverage ?? 0) * 100);
  return <section className="panel full">
    <div className="panelHeader"><div><p className="eyebrow">Independent projection signal</p><h2>FantasyPros coverage and quota</h2></div><StatusPill status={quota.status}/></div>
    <div className="metricGrid">
      <article className="metric"><small>Matchup coverage</small><strong>{coverage}%</strong><span>{quota.target_player_count ?? 0} mapped targets</span></article>
      <article className="metric"><small>Our roster projections</small><strong>{rosterProjectionCount}</strong><span>independent player matches</span></article>
      <article className="metric"><small>Mean disagreement</small><strong>{averageDisagreement == null ? "—" : averageDisagreement.toFixed(1)}</strong><span>fantasy points across signals</span></article>
      <article className="metric"><small>Rolling quota</small><strong>{quota.total_remaining ?? "—"}/{quota.daily_limit ?? 50}</strong><span>{quota.standard_remaining ?? 0} routine · {quota.reserved_requests ?? 0} protected</span></article>
    </div>
    <div className="refreshControl"><div><strong>{quota.last_refresh_at ? `Updated ${new Date(quota.last_refresh_at).toLocaleString()}` : "No successful refresh yet"}</strong><small>{cooldown > 0 ? `Emergency refresh available in ${duration(cooldown)}` : "Emergency refresh may use protected quota"}</small></div><button type="button" onClick={refresh} disabled={!canRefresh || busy}>{busy ? "Refreshing…" : "Refresh now"}</button>{message ? <small className="controlMessage" role="status">{message}</small> : null}</div>
  </section>;
}
