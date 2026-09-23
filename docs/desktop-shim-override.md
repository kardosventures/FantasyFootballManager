# Approved desktop-shim override

> Superseded on 2026-09-08 by the [Playwright browser adapter](browser-adapter.md). This file remains as the audit record for the original write-path decision.

The supplied product specification originally treats undocumented Sleeper writes as unavailable. The owner explicitly approved a local macOS Sleeper desktop shim as the only write path, subject to these constraints:

- Sleeper HTTP remains documented, public, credential-free, and GET-only.
- The shim runs outside Docker in the logged-in macOS session.
- Commands are semantic, idempotent, time-bounded, evidence-bound, and scoped only to Jim.ai roster 8.
- Accessibility elements are preferred. Anchored screenshots and CGEvent input may be implemented per action only after recorded fixtures and rehearsal; raw coordinate commands are prohibited.
- Version 149.1 is the initial qualified app version. Any change blocks writes until requalification.
- Every attempt captures evidence and is verified from the public API. Ambiguous submission blocks retries pending reconciliation.
- Trades and commissioner settings are permanently excluded.

The implementation currently supplies the command contract, health/preflight inspection, audit state machine, fake/dry-run verification, and a fail-closed placeholder for live action drivers. This preserves the staged activation requirement.
