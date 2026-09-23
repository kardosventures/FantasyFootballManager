"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

import type { ActionItem } from "@/lib/types";

function duration(seconds: number): string {
  const safe = Math.max(0, Math.ceil(seconds));
  const hours = Math.floor(safe / 3600);
  const minutes = Math.floor((safe % 3600) / 60);
  const remainder = safe % 60;
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m ${remainder}s`;
  return `${remainder}s`;
}

function countdown(execution: NonNullable<ActionItem["execution"]>, now: number): string {
  const eligibleAt = new Date(execution.not_before).getTime();
  const expiresAt = new Date(execution.expires_at).getTime();
  if (Number.isNaN(eligibleAt) || Number.isNaN(expiresAt)) return "Execution time unavailable";
  if (now < eligibleAt) return `Browser click eligible in ${duration((eligibleAt - now) / 1000)}`;
  if (now < expiresAt) return `Eligible now · safe window closes in ${duration((expiresAt - now) / 1000)}`;
  return "Safe execution window closed";
}

export function ActionControl({ action }: { action: ActionItem }) {
  const router = useRouter();
  const [now, setNow] = useState(() => Date.now());
  const [message, setMessage] = useState("");
  const [cancelled, setCancelled] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!action.execution || ["verified", "unverified", "failed", "blocked", "cancelled"].includes(action.execution.status)) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [action.execution]);

  const timing = useMemo(
    () => action.execution ? countdown(action.execution, now) : action.status === "approval_required" ? "Awaiting owner approval" : "No browser command queued",
    [action.execution, action.status, now],
  );

  const cancel = async () => {
    setBusy(true);
    setMessage("Cancelling before the browser write boundary…");
    try {
      const csrfResponse = await fetch("/manager-api/api/csrf", { cache: "no-store" });
      if (!csrfResponse.ok) throw new Error("Could not create a secure cancellation token");
      const { token } = await csrfResponse.json();
      const response = await fetch(`/manager-api/api/actions/${action.id}/cancel`, {
        method: "POST",
        headers: { "X-CSRF-Token": token },
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail ?? "Cancellation failed");
      setCancelled(true);
      setMessage("Cancelled. The browser agent cannot lease this action.");
      router.refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Cancellation failed");
    } finally {
      setBusy(false);
    }
  };

  const canCancel = action.can_cancel && !cancelled;
  return <div className="actionControl">
    <div><strong suppressHydrationWarning>{cancelled ? "Cancelled" : timing}</strong>{action.execution ? <small>Command {action.execution.status} · {action.execution.attempt_count} attempt{action.execution.attempt_count === 1 ? "" : "s"}</small> : null}</div>
    {canCancel ? <button type="button" className="cancelButton" onClick={cancel} disabled={busy}>{busy ? "Cancelling…" : "Cancel action"}</button> : null}
    {message ? <small className="controlMessage" role="status">{message}</small> : null}
  </div>;
}
