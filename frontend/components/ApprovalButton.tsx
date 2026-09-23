"use client";

import { useState } from "react";

export function ApprovalButton({ actionId }: { actionId: string }) {
  const [nonce, setNonce] = useState("");
  const [message, setMessage] = useState("");
  const approve = async () => {
    setMessage("Checking exact action and evidence…");
    const csrfResponse = await fetch("/manager-api/api/csrf");
    const { token } = await csrfResponse.json();
    const response = await fetch(`/manager-api/api/actions/${actionId}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
      body: JSON.stringify({ nonce }),
    });
    const result = await response.json();
    setMessage(response.ok ? "Approved and queued." : result.detail ?? "Approval failed.");
  };
  return <div className="approvalControl"><label><span>Approval code from email</span><input value={nonce} onChange={event => setNonce(event.target.value)} placeholder="Paste single-use code"/></label><button type="button" onClick={approve} disabled={nonce.length < 20}>Approve exact action</button>{message && <small role="status">{message}</small>}</div>;
}
