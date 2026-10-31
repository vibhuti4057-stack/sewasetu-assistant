# Sewa Setu assistant: design note

**What I built.** Two MCP servers over streamable HTTP, one for citizens (`/citizen/mcp`) and one for block officers (`/officer/mcp`), running in the same image as the portal. The portal stays the system of record. Sign-in is the portal's own OAuth 2.1 flow; the assistant only verifies the token.

## Three decisions that mattered

1. **The assistant reuses the portal's own rules.** `apply_for_pension` calls the portal's `validate_application()` and `create_application()` rather than re-implementing them. The assistant can never admit an application the web form would refuse, and when the policy changes (age, window, blocks) it changes in one place.
2. **Identity comes from the token, never from tool arguments.** No tool takes a mobile number or officer name. `sub` in the verified JWT decides whose applications are read or changed, so no prompt can make a citizen act for someone else (eval P11 tries exactly this).
3. **Separate endpoints, scopes and audiences for citizen and officer.** A citizen's agent never even lists the officer tools, and a citizen token is rejected by the officer endpoint with 401. Tools also fail safely: deciding is a single `UPDATE ... WHERE status = 'PENDING'`, and a mobile gets an advisory lock so two simultaneous submissions cannot both succeed.

## One thing I would do differently

My evals are scripted: the personas speak in fixed lines and the tool calls are fixed. They prove the tools and the database behave, but not that a small model reading my tool descriptions picks the right tool and asks the right questions. With more time I would add an LLM-driven eval, a small model playing each persona through a real MCP client with a turn limit, and score the same database checks.

## Left alone on purpose

The scheme window (31/10/2026 23:59 IST) is the portal's rule, so the assistant refuses politely afterwards instead of faking success.
