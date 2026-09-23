# Security model

- The public Sleeper integration is a typed GET-only client. Static tests scan for non-GET methods, private endpoints, credentials, Selenium, and Playwright in the backend.
- The API binds to loopback. Only the dashboard is exposed, and only on an explicitly configured private or loopback address.
- The dashboard is intentionally login-free. Mutations require a signed same-site CSRF token. Protected-drop approvals additionally require a private client address and signed, single-use, expiring nonce.
- The Playwright agent uses a separate bearer secret on loopback endpoints. Rotate `APP_SECRET_KEY` and `SHIM_SHARED_SECRET` independently.
- The persistent Sleeper profile is dedicated to this project, git-ignored, and must not be shared with a normal browsing profile.
- Secrets belong only in `.env`, which is ignored by git. No Sleeper credential is required or accepted.
- Evidence hashes and the policy version form part of approval and command identity. A material change requires a new decision and approval.
- Optional Codex execution is ephemeral and read-only. The prompt treats evidence as untrusted data and limits the result to a summary.
