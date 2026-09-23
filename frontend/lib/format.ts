export function operationalStatus(ready: boolean, blockerCount: number): string {
  return ready ? "Ready" : `${blockerCount} blocker${blockerCount === 1 ? "" : "s"}`;
}

export function isTerminalAction(status: string): boolean {
  return ["verified", "unverified", "failed", "blocked", "cancelled", "superseded"].includes(status);
}
