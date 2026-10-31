# -*- coding: utf-8 -*-
"""
Sewa Setu assistant: two MCP servers (citizen, officer) over streamable HTTP.

Design: this service runs in the same image as the portal and imports the portal's own
validate_application / create_application / write_audit, so the assistant can never
admit an application the web form would refuse. Identity always comes from the OAuth
token (sub = citizen:<mobile> or officer:<username>), never from tool arguments.
"""
import os
import re
import base64
import binascii
import contextlib
import html
import json
from datetime import datetime, timedelta

from starlette.applications import Starlette
from starlette.responses import JSONResponse, HTMLResponse
from starlette.routing import Mount, Route
from mcp.server.fastmcp import FastMCP, Context

import app as portal                       # the portal: rules, DB, SMS helpers
from oauth import verify_token, ISSUER     # the portal's token verification

BASE = os.environ.get("ASSISTANT_BASE_URL", "http://localhost:8001").rstrip("/")
STATUSES = ("PENDING", "APPROVED", "REJECTED", "WITHDRAWN", "DEEMED_APPROVED")
GENDERS = {"male": "Male", "female": "Female", "other": "Other"}
MARITAL = {"married": "Married", "unmarried": "Unmarried", "widowed": "Widowed"}

try:  # the server sits behind our own proxy, so the Host header is not localhost
    from mcp.server.transport_security import TransportSecuritySettings
    _SEC = {"transport_security": TransportSecuritySettings(enable_dns_rebinding_protection=False)}
except ImportError:
    _SEC = {}


def make(name, instructions):
    return FastMCP(name, instructions=instructions, stateless_http=True,
                   json_response=True, **_SEC)


@contextlib.contextmanager
def db():
    conn = portal.get_db()
    try:
        yield conn, conn.cursor()
    finally:
        conn.close()          # uncommitted work is rolled back


def fmt(v):
    return v.isoformat(sep=" ", timespec="minutes") if isinstance(v, datetime) else (
        v.isoformat() if hasattr(v, "isoformat") else v)


def _canon_block(b):
    m = {x.lower(): x for x in portal.BLOCKS}
    return m.get((b or '').strip().lower(), b)


def who(ctx: Context):
    """(role, id) of the signed-in person, from the already verified token."""
    claims = ctx.request_context.request.scope["auth_claims"]
    role, _, ident = claims["sub"].partition(":")
    return role, ident


# =========================================================================
# CITIZEN
# =========================================================================
citizen = make(
    "Sewa Setu - Citizen",
    "You help ONE signed-in citizen with the Old Age Pension scheme: learn the rules, apply, "
    "check status, withdraw. Speak the citizen's language and keep it simple; many are elderly. "
    "Never invent or guess any detail (name, date of birth, bank, document): ask for it. "
    "Read the details back and get a clear yes before calling apply_for_pension. "
    "If the citizen is not eligible, say so kindly and do not submit anything.")


@citizen.tool()
def scheme_info() -> dict:
    """Rules of the Old Age Pension scheme: minimum age, valid blocks, allowed values,
    document needed, deadline and whether applications are open right now.
    Call this first when someone asks about eligibility or wants to apply."""
    return {
        "minimum_age_years": portal.MIN_AGE,
        "blocks": portal.BLOCKS,
        "gender_values": list(GENDERS.values()),
        "marital_status_values": list(MARITAL.values()),
        "date_of_birth_format": "DD/MM/YYYY",
        "bank_account": "9 to 18 digits",
        "ifsc_example": "SBIN0003077",
        "age_proof": "one JPG, PNG or PDF file, up to 5 MB, sent as base64",
        "one_active_application_per_mobile": True,
        "decision_time_days": portal.SLA_DAYS,
        "applications_open": portal.deadline_remaining() is not None,
        "window_closes": portal.SCHEME_DEADLINE.strftime("%d/%m/%Y %H:%M IST"),
    }


@citizen.tool()
def my_application(ctx: Context) -> dict:
    """Status of the signed-in citizen's own applications (most recent first), and whether
    they can apply now. Use for any 'what happened to my application' question."""
    _, mobile = who(ctx)
    with db() as (conn, cur):
        cur.execute("SELECT application_no, status, applicant_name, village, block, "
                    "submitted_at, decided_at, decided_by FROM applications "
                    "WHERE mobile = %s ORDER BY submitted_at DESC LIMIT 5", (mobile,))
        rows = cur.fetchall()
        active = portal.active_application(cur, mobile)
    keys = ("application_no", "status", "applicant_name", "village", "block",
            "submitted_at", "decided_at", "decided_by")
    return {
        "applications": [{k: fmt(v) for k, v in zip(keys, r)} for r in rows],
        "has_active_application": bool(active),
        "can_apply_now": (not active) and portal.deadline_remaining() is not None,
    }


@citizen.tool()
def apply_for_pension(ctx: Context, applicant_name: str, dob: str, gender: str,
                      marital_status: str, village: str, block: str, bank_account: str,
                      ifsc: str, age_proof_filename: str, age_proof_base64: str,
                      husband_name: str = "") -> dict:
    """Submit the pension application for the signed-in citizen. Nothing is saved unless
    every rule passes; on failure you get a list of plain-language problems to fix with
    the citizen. dob is DD/MM/YYYY. age_proof_* is the citizen's real document
    (.jpg/.jpeg/.png/.pdf, max 5 MB) as base64: never fabricate one. husband_name is
    optional. The citizen's mobile number comes from their sign-in."""
    _, mobile = who(ctx)
    errors = []

    gender_v = GENDERS.get((gender or "").strip().lower())
    marital_v = MARITAL.get((marital_status or "").strip().lower())
    if not gender_v:
        errors.append("gender must be one of: " + ", ".join(GENDERS.values()))
    if not marital_v:
        errors.append("marital status must be one of: " + ", ".join(MARITAL.values()))

    # age-proof document: checked here, written to disk only after everything passes
    fname = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(age_proof_filename or ""))[:80]
    blob = b""
    if fname and age_proof_base64:
        if fname.rsplit(".", 1)[-1].lower() not in ("pdf", "jpg", "jpeg", "png"):
            errors.append("age proof must be a JPG, PNG or PDF file")
            fname = ""
        try:
            blob = base64.b64decode(age_proof_base64, validate=True)
        except (binascii.Error, ValueError):
            errors.append("age proof file is not valid base64")
            fname = ""
        if len(blob) > 5 * 1024 * 1024:
            errors.append("age proof file is larger than 5 MB")
            fname = ""
        if fname and not blob:
            fname = ""
    else:
        fname = ""
    doc_path = os.path.join(portal.UPLOAD_DIR, "%s_%s" % (mobile, fname.lower())) if fname else ""

    data = {"applicant_name": applicant_name, "dob": dob, "gender": gender_v or "",
            "marital_status": marital_v or "", "husband_name": husband_name,
            "village": village, "block": _canon_block(block), "bank_account": bank_account, "ifsc": ifsc}
    rule_errors, cleaned = portal.validate_application(data, doc_path)
    errors += [e for e in rule_errors if e not in errors]
    if errors:
        return {"submitted": False, "problems": errors}

    with db() as (conn, cur):
        # one application at a time per mobile, even if the assistant is called twice at once
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (mobile,))
        existing = portal.active_application(cur, mobile)
        if existing:
            return {"submitted": False, "problems": [
                "Application %s is already %s for this mobile number. A pending application "
                "can be withdrawn first if the citizen wants to apply afresh." % (existing[1], existing[2])]}
        os.makedirs(portal.UPLOAD_DIR, exist_ok=True)
        with open(doc_path, "wb") as out:
            out.write(blob)
        new_id, app_no = portal.create_application(cur, mobile, cleaned)
        conn.commit()
    portal.send_sms(mobile, "Sewa Setu: application %s received. Track at the status portal "
                            "with mobile no. and password (DOB as DDMMYYYY)." % app_no)
    return {"submitted": True, "application_no": app_no, "status": "PENDING",
            "decision_within_days": portal.SLA_DAYS}


@citizen.tool()
def withdraw_my_application(ctx: Context) -> dict:
    """Withdraw the signed-in citizen's PENDING application so they can apply afresh.
    Only pending applications can be withdrawn. Ask the citizen to confirm first."""
    _, mobile = who(ctx)
    with db() as (conn, cur):
        cur.execute("UPDATE applications SET status = 'WITHDRAWN', decided_at = %s, "
                    "decided_by = 'APPLICANT' WHERE status = 'PENDING' AND id = ("
                    "SELECT id FROM applications WHERE mobile = %s AND status = 'PENDING' "
                    "ORDER BY submitted_at DESC LIMIT 1) RETURNING id, application_no",
                    (datetime.now(), mobile))
        row = cur.fetchone()
        if not row:
            return {"withdrawn": False, "reason": "There is no pending application to withdraw."}
        portal.write_audit(cur, row[0], "WITHDRAW", "applicant:%s" % mobile)
        conn.commit()
    return {"withdrawn": True, "application_no": row[1], "can_apply_again": True}


# =========================================================================
# OFFICER
# =========================================================================
officer = make(
    "Sewa Setu - Block Officer",
    "You help a signed-in block officer work the pension queue: see counts, list and open "
    "applications, approve or reject PENDING ones. Oldest first. Never decide anything the "
    "officer did not clearly ask for; confirm the application number before approving or rejecting. "
    "A rejection needs a reason that is shown in the audit trail.")


@officer.tool()
def queue_summary() -> dict:
    """Counts of applications by status, how many PENDING ones are past the statutory
    deadline, and PENDING counts per block. Start here."""
    with db() as (conn, cur):
        cur.execute("SELECT status, count(*) FROM applications GROUP BY status")
        by_status = {s: n for s, n in cur.fetchall()}
        cur.execute("SELECT count(*) FROM applications WHERE status = 'PENDING' AND submitted_at < %s",
                    (datetime.now() - timedelta(days=portal.SLA_DAYS),))
        overdue = cur.fetchone()[0]
        cur.execute("SELECT block, count(*) FROM applications WHERE status = 'PENDING' "
                    "GROUP BY block ORDER BY count(*) DESC")
        by_block = {b: n for b, n in cur.fetchall()}
    return {"by_status": by_status, "pending_overdue": overdue,
            "pending_by_block": by_block, "sla_days": portal.SLA_DAYS}


@officer.tool()
def list_applications(status: str = "PENDING", block: str = "", mobile: str = "",
                      page: int = 1) -> dict:
    """List applications, oldest first, 20 per page. status is one of PENDING, APPROVED,
    REJECTED, WITHDRAWN, DEEMED_APPROVED. Optional block filter. If mobile is given, all
    applications of that mobile are returned regardless of status."""
    page = max(1, int(page))
    cols = "id, application_no, applicant_name, mobile, block, village, status, submitted_at"
    with db() as (conn, cur):
        if mobile.strip():
            cur.execute("SELECT %s FROM applications WHERE mobile = %%s ORDER BY submitted_at ASC LIMIT 50" % cols,
                        (mobile.strip(),))
            rows, more = cur.fetchall(), False
        else:
            status = status.strip().upper()
            if status not in STATUSES:
                return {"error": "status must be one of: " + ", ".join(STATUSES)}
            if block.strip() and block.strip() not in portal.BLOCKS:
                return {"error": "block must be one of: " + ", ".join(portal.BLOCKS)}
            sql = "SELECT %s FROM applications WHERE status = %%s" % cols
            args = [status]
            if block.strip():
                sql += " AND block = %s"
                args.append(block.strip())
            sql += " ORDER BY submitted_at ASC, id ASC LIMIT 21 OFFSET %s"
            args.append((page - 1) * 20)
            cur.execute(sql, args)
            rows = cur.fetchall()
            more = len(rows) > 20
            rows = rows[:20]
    keys = cols.split(", ")
    return {"applications": [{k: fmt(v) for k, v in zip(keys, r)} for r in rows],
            "page": page, "has_more": more}


@officer.tool()
def get_application(application_id: int) -> dict:
    """Full details of one application (id from list_applications) with its audit trail.
    The bank account is shown masked."""
    with db() as (conn, cur):
        cur.execute("SELECT application_no, applicant_name, mobile, dob, gender, marital_status, "
                    "village, block, bank_account, ifsc, doc_path, status, submitted_at, "
                    "decided_at, decided_by FROM applications WHERE id = %s", (int(application_id),))
        r = cur.fetchone()
        if not r:
            return {"error": "No application with that id."}
        cur.execute("SELECT at, action, actor, note FROM audit_log WHERE application_id = %s "
                    "ORDER BY at ASC", (int(application_id),))
        audit = cur.fetchall()
    keys = ("application_no", "applicant_name", "mobile", "dob", "gender", "marital_status",
            "village", "block", "bank_account", "ifsc", "doc_path", "status", "submitted_at",
            "decided_at", "decided_by")
    out = {k: fmt(v) for k, v in zip(keys, r)}
    out["bank_account"] = "******" + (r[8] or "")[-4:]
    out["age_proof_on_file"] = bool(r[10])
    out.pop("doc_path")
    out["audit_trail"] = [{"at": fmt(a), "action": b, "actor": c, "note": d} for a, b, c, d in audit]
    return out


def _decide(ctx, application_id, new_status, note):
    _, user = who(ctx)
    note = (note or "").strip()[:500]
    with db() as (conn, cur):
        cur.execute("UPDATE applications SET status = %s, decided_at = %s, decided_by = %s "
                    "WHERE id = %s AND status = 'PENDING' RETURNING application_no",
                    (new_status, datetime.now(), user[:50], int(application_id)))
        row = cur.fetchone()
        if row:
            portal.write_audit(cur, int(application_id), new_status, "admin:%s" % user, note)
            conn.commit()
            return {"done": True, "application_no": row[0], "status": new_status}
        cur.execute("SELECT application_no, status FROM applications WHERE id = %s", (int(application_id),))
        cur_row = cur.fetchone()
    if not cur_row:
        return {"done": False, "reason": "No application with that id."}
    return {"done": False, "application_no": cur_row[0], "current_status": cur_row[1],
            "reason": "Only a PENDING application can be decided."}


@officer.tool()
def approve_application(ctx: Context, application_id: int, note: str = "") -> dict:
    """Approve one PENDING application. Fails, changing nothing, if it is not pending."""
    return _decide(ctx, application_id, "APPROVED", note)


@officer.tool()
def reject_application(ctx: Context, application_id: int, note: str) -> dict:
    """Reject one PENDING application. A reason (note) is required and goes in the audit
    trail. Fails, changing nothing, if the application is not pending."""
    if not (note or "").strip():
        return {"done": False, "reason": "A reason is required to reject an application."}
    return _decide(ctx, application_id, "REJECTED", note)


# =========================================================================
# Plumbing: token check in front of each MCP endpoint, discovery, startup
# =========================================================================
class Guard:
    """Only lets through a valid portal token whose audience is THIS endpoint and whose
    scope/role/subject all match. Otherwise 401 pointing the client at the sign-in flow."""

    def __init__(self, inner, role, path):
        self.inner, self.role = inner, role
        self.audiences = [BASE + path, BASE + path + "/"]
        self.meta = "%s/.well-known/oauth-protected-resource%s" % (BASE, path)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        auth = dict(scope["headers"]).get(b"authorization", b"").decode()
        claims = verify_token(auth[7:].strip(), self.audiences) if auth.lower().startswith("bearer ") else None
        if not (claims and claims.get("scope") == self.role and claims.get("role") == self.role
                and str(claims.get("sub", "")).startswith(self.role + ":")):
            resp = JSONResponse({"error": "invalid_token"}, status_code=401, headers={
                "WWW-Authenticate": 'Bearer resource_metadata="%s"' % self.meta})
            return await resp(scope, receive, send)
        scope["auth_claims"] = claims
        return await self.inner(scope, receive, send)


def resource_doc(path, role, name):
    async def handler(request):
        return JSONResponse({"resource": BASE + path, "authorization_servers": [ISSUER],
                             "scopes_supported": [role], "bearer_methods_supported": ["header"],
                             "resource_name": name})
    return Route("/.well-known/oauth-protected-resource" + path, handler)


async def healthz(request):
    return JSONResponse({"ok": True})



# =========================================================================
# Eval dashboard: what the scripted personas did and what the database showed afterwards
# =========================================================================
def _e(v):
    return html.escape(str(v))


def _persona_card(row):
    pid, status, meta, steps, checks, started, ms, run_id = row
    colour = {"PASS": "#1a7f37", "FAIL": "#cf222e", "SKIPPED": "#9a6700"}.get(status, "#555")
    s_html = []
    for s in steps:
        if s.get("kind") == "say":
            s_html.append("<li class='say'><b>%s:</b> &ldquo;%s&rdquo;</li>" % (_e(s["who"]), _e(s["text"])))
        else:
            s_html.append("<li class='tool'><code>%s/%s(%s)</code><br>&rarr; <code>%s</code></li>" % (
                _e(s.get("role", "")), _e(s["tool"]), _e(json.dumps(s.get("args", {}), ensure_ascii=False)[:300]),
                _e(s.get("result", ""))))
    c_html = []
    for c in checks:
        c_html.append("<li class='%s'>%s %s%s</li>" % (
            "ok" if c["ok"] else "bad", "&#10003;" if c["ok"] else "&#10007;", _e(c["name"]),
            "" if c["ok"] else " <small>(%s)</small>" % _e(c.get("detail", ""))))
    return ("<section><h2>%s &middot; %s <span class='pill' style='background:%s'>%s</span></h2>"
            "<p class='meta'>%s, age %s, %s &middot; <i>%s</i></p>"
            "<p><b>Goal:</b> %s<br><b>Must be true in the database:</b> %s</p>"
            "<details><summary>Conversation and tool calls (%d steps)</summary><ol>%s</ol></details>"
            "<h3>Checks against the real database / files</h3><ul class='checks'>%s</ul>"
            "<p class='meta'>run %s &middot; %s &middot; %s ms</p></section>") % (
        _e(pid), _e(meta["name"]), colour, status, _e(meta["name"]), _e(meta["age"]), _e(meta["village"]),
        _e(meta["style"]), _e(meta["goal"]), _e(meta["expect"]), len(steps), "".join(s_html),
        "".join(c_html), _e(run_id), _e(fmt(started)), ms)


def _eval_data():
    with db() as (conn, cur):
        cur.execute("SELECT DISTINCT ON (persona_id) persona_id, status, persona, steps, checks, "
                    "started_at, duration_ms, run_id FROM eval_results "
                    "ORDER BY persona_id, (status = 'SKIPPED') ASC, id DESC")
        rows = cur.fetchall()
        cur.execute("SELECT run_id, min(started_at), "
                    "count(*) FILTER (WHERE status = 'PASS'), "
                    "count(*) FILTER (WHERE status IN ('PASS','FAIL')) "
                    "FROM eval_results GROUP BY run_id ORDER BY 2 DESC LIMIT 10")
        runs = cur.fetchall()
    return rows, runs


async def eval_dashboard(request):
    try:
        rows, runs = _eval_data()
    except Exception:
        rows, runs = [], []
    real = [r for r in rows if r[1] in ("PASS", "FAIL")]
    passed = sum(1 for r in real if r[1] == "PASS")
    checks_total = sum(len(r[4]) for r in real)
    checks_ok = sum(1 for r in real for c in r[4] if c["ok"])
    findings = ""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "findings.md"),
                  encoding="utf-8") as fh:
            findings = fh.read()
    except OSError:
        pass
    head = ("<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Sewa Setu assistant: evals</title><style>"
            "body{font-family:system-ui,sans-serif;max-width:920px;margin:24px auto;padding:0 14px;color:#1f2328;line-height:1.45}"
            "section{border:1px solid #d0d7de;border-radius:8px;padding:12px 16px;margin:14px 0}"
            "h1{margin-bottom:4px}h2{font-size:1.1rem;margin:0 0 4px}h3{font-size:.95rem;margin:10px 0 4px}"
            ".pill{color:#fff;border-radius:10px;padding:1px 9px;font-size:.8rem;margin-left:6px}"
            ".meta{color:#57606a;font-size:.85rem;margin:2px 0}.say{color:#0550ae}.ok{color:#1a7f37}.bad{color:#cf222e;font-weight:600}"
            "code{font-size:.78rem;word-break:break-all}ul.checks{list-style:none;padding-left:4px}"
            "pre{white-space:pre-wrap;background:#f6f8fa;padding:10px;border-radius:6px}table{border-collapse:collapse}"
            "td,th{border:1px solid #d0d7de;padding:3px 9px;font-size:.85rem}</style></head><body>")
    summary = ("<h1>Sewa Setu assistant: persona evals</h1>"
               "<p>Scripted citizens and officers sign in through the real OAuth flow, call the real MCP tools, "
               "then the runner reads the portal&rsquo;s database and upload folder to see what is actually true. "
               "<b>%d of %d personas passed</b> &middot; %d of %d checks. Personas that need the window open are "
               "skipped (their last real result is shown) once the scheme closes on 31/10/2026 23:59 IST.</p>") % (
        passed, len(real), checks_ok, checks_total)
    if not rows:
        summary += "<p><b>No runs yet.</b></p>"
    hist = ""
    if runs:
        hist = "<h3>Recent runs</h3><table><tr><th>run</th><th>started</th><th>passed</th></tr>%s</table>" % "".join(
            "<tr><td>%s</td><td>%s</td><td>%d / %d</td></tr>" % (_e(a), _e(fmt(b)), c, d) for a, b, c, d in runs)
    fnd = ("<h2>What the evals found, and what I changed</h2><pre>%s</pre>" % _e(findings)) if findings else ""
    body = "".join(_persona_card(r) for r in sorted(rows, key=lambda r: r[0]))
    return HTMLResponse(head + summary + hist + fnd + body + "</body></html>")


async def eval_json(request):
    try:
        rows, _ = _eval_data()
    except Exception:
        rows = []
    return JSONResponse([{"persona": r[0], "status": r[1], "name": r[2]["name"],
                          "checks": r[4], "run_id": r[7]} for r in sorted(rows, key=lambda r: r[0])],
                        )


citizen_app = citizen.streamable_http_app()
officer_app = officer.streamable_http_app()


@contextlib.asynccontextmanager
async def lifespan(_):
    async with contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(citizen.session_manager.run())
        await stack.enter_async_context(officer.session_manager.run())
        yield


app = Starlette(
    routes=[
        Route("/healthz", healthz),
        Route("/eval-dashboard", eval_dashboard),
        Route("/eval-dashboard.json", eval_json),
        resource_doc("/citizen/mcp", "citizen", "Sewa Setu citizen assistant"),
        resource_doc("/officer/mcp", "officer", "Sewa Setu block officer assistant"),
        Mount("/citizen", app=Guard(citizen_app, "citizen", "/citizen/mcp")),
        Mount("/officer", app=Guard(officer_app, "officer", "/officer/mcp")),
    ],
    lifespan=lifespan,
)

