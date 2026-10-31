Run 1 (local, 30 Oct 2026): 10 of 10 active personas passed (P07 waits for the deadline). Smoke test: 19 of 19.
Live run (public ngrok address): see the dashboard.

What the evals confirmed:
- Rules come from the portal: under-60 refused, bad IFSC refused, a repeat application blocked, and two simultaneous submissions produced exactly one row.
- Refusals leave nothing behind: no database row and no uploaded file.
- Officer decisions are single-use: a second approve or a reject after approve is refused, the row is unchanged and the audit log has exactly one decision.
- Reject needs a reason; empty and blank reasons are refused.
- Identity comes from the token: a citizen cannot reach the officer endpoint (401) or send another mobile number.

Limits I know about:
- The personas are scripted, so they test the tools, not how a model uses the tool descriptions.
- P07 (applying after the window closes) runs after 31 Oct 23:59 IST.
