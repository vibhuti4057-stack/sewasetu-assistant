# -*- coding: utf-8 -*-
"""
Sewa Setu MCP tester. Needs only the `requests` package (already inside the assistant image).

It signs in exactly like a real client would (dynamic registration, PKCE, portal login,
consent, token), then calls the MCP tools over streamable HTTP.

Commands
  python sewa.py demo                       run the full smoke test (prints PASS / FAIL)
  python sewa.py tools citizen              list the citizen tools
  python sewa.py tools officer              list the officer tools
  python sewa.py call citizen scheme_info '{}'
  python sewa.py call officer queue_summary '{}'

Settings (environment variables, all optional)
  PORTAL         where the portal is reached from this script    default http://localhost:8000
  ASSISTANT      where the assistant is reached from this script default http://localhost:8001
  RESOURCE_BASE  the assistant's public address (must equal ASSISTANT_BASE_URL on the server)
                 default = ASSISTANT
  ADMIN_USERNAME / ADMIN_PASSWORD   officer login   default admin / MyPass12345
  CITIZEN_MOBILE                    default: a random 10-digit number
"""
import base64
import hashlib
import json
import os
import random
import re
import secrets
import sys
import time
from urllib.parse import urlparse, parse_qs, urljoin

import requests

PORTAL = os.environ.get("PORTAL", "http://localhost:8000").rstrip("/")
ASSISTANT = os.environ.get("ASSISTANT", "http://localhost:8001").rstrip("/")
RESOURCE_BASE = os.environ.get("RESOURCE_BASE", ASSISTANT).rstrip("/")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "MyPass12345")
REDIRECT = "http://localhost:53682/callback"
PATHS = {"citizen": "/citizen/mcp", "officer": "/officer/mcp"}

# a real (tiny) 1x1 PNG, used as the age-proof document in tests
PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _otp_for(mobile, after_id, wait=20):
    """Read the OTP from the simulated SMS gateway (delivery takes 1-4 seconds)."""
    end = time.time() + wait
    while time.time() < end:
        r = requests.get(PORTAL + "/__gateway/api/messages", params={"to": mobile}, timeout=10)
        for m in sorted(r.json().get("messages", []), key=lambda x: x["id"], reverse=True):
            if m["id"] > after_id and m.get("delivered") and m.get("text"):
                found = re.search(r"OTP is (\d{6})", m["text"])
                if found:
                    return found.group(1)
        time.sleep(1)
    raise RuntimeError("OTP did not arrive in %ss" % wait)


def _last_msg_id(mobile):
    r = requests.get(PORTAL + "/__gateway/api/messages", params={"to": mobile}, timeout=10)
    ids = [m["id"] for m in r.json().get("messages", [])]
    return max(ids) if ids else 0


def get_token(role, mobile=None):
    """Full OAuth sign-in. Returns an access token whose audience is the role's MCP URL."""
    resource = RESOURCE_BASE + PATHS[role]
    reg = requests.post(PORTAL + "/oauth/register", timeout=15, json={
        "client_name": "sewa.py tester", "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none"})
    reg.raise_for_status()
    client_id = reg.json()["client_id"]

    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    s = requests.Session()
    r = s.get(PORTAL + "/oauth/authorize", allow_redirects=False, timeout=15, params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "scope": role, "resource": resource, "state": "x"})
    if r.status_code not in (301, 302, 303):
        raise RuntimeError("authorize failed: %s %s" % (r.status_code, r.text[:300]))
    login_url = urljoin(r.url, r.headers["Location"])
    rid = parse_qs(urlparse(login_url).query)["rid"][0]
    login_url = PORTAL + "/oauth/login?rid=" + rid
    consent_url = PORTAL + "/oauth/consent?rid=" + rid

    if role == "citizen":
        before = _last_msg_id(mobile)
        s.post(login_url, data={"mobile": mobile}, timeout=15)
        code = _otp_for(mobile, before)
        r = s.post(login_url, data={"otp": code}, allow_redirects=False, timeout=15)
    else:
        r = s.post(login_url, data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
                   allow_redirects=False, timeout=15)
    if r.status_code not in (301, 302, 303):
        raise RuntimeError("sign-in was refused (wrong OTP or password?)")

    r = s.post(consent_url, data={"decision": "allow"}, allow_redirects=False, timeout=15)
    loc = r.headers.get("Location", "")
    auth_code = parse_qs(urlparse(loc).query).get("code", [None])[0]
    if not auth_code:
        raise RuntimeError("no authorization code returned: %s" % loc[:200])

    t = requests.post(PORTAL + "/oauth/token", timeout=15, data={
        "grant_type": "authorization_code", "code": auth_code, "client_id": client_id,
        "redirect_uri": REDIRECT, "code_verifier": verifier, "resource": resource})
    t.raise_for_status()
    return t.json()["access_token"]


def rpc(role, token, method, params=None, rid=1):
    """One JSON-RPC call to the role's MCP endpoint. Returns (http_status, parsed_body)."""
    r = requests.post(ASSISTANT + PATHS[role], timeout=60, json={
        "jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}, headers={
        "Authorization": "Bearer " + token if token else "",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json"})
    body = r.text
    if "text/event-stream" in r.headers.get("content-type", ""):
        datas = [ln[5:].strip() for ln in body.splitlines() if ln.startswith("data:")]
        body = datas[-1] if datas else "{}"
    try:
        return r.status_code, json.loads(body)
    except ValueError:
        return r.status_code, {"raw": body[:300]}


def call_tool(role, token, name, args=None):
    status, body = rpc(role, token, "tools/call", {"name": name, "arguments": args or {}})
    if status != 200:
        return {"_http": status, "_body": body}
    result = body.get("result") or {}
    if result.get("structuredContent"):
        out = result["structuredContent"]
        return out.get("result", out) if list(out) == ["result"] else out
    for c in result.get("content", []):
        if c.get("type") == "text":
            try:
                return json.loads(c["text"])
            except ValueError:
                return {"text": c["text"], "_isError": result.get("isError")}
    return body


def list_tools(role, token):
    status, body = rpc(role, token, "tools/list")
    return status, [t["name"] for t in (body.get("result") or {}).get("tools", [])]


# ------------------------------------------------------------------------------------
# smoke test
# ------------------------------------------------------------------------------------
RESULTS = []


def check(name, ok, detail=""):
    ok = bool(ok)
    RESULTS.append(ok)
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, ("   -> " + str(detail)[:200]) if (detail and not ok) else ""))


def demo():
    mobile = os.environ.get("CITIZEN_MOBILE") or "9" + "".join(random.choice("0123456789") for _ in range(9))
    print("Portal %s | assistant %s | audience base %s | test mobile %s\n" % (
        PORTAL, ASSISTANT, RESOURCE_BASE, mobile))
    good = dict(applicant_name="Kamla Devi", dob="01/01/1960", gender="Female", marital_status="Widowed",
                village="Test Gaon", block="Sonari", bank_account="123456789012", ifsc="SBIN0003077",
                age_proof_filename="proof.png", age_proof_base64=PNG_B64)

    ctok = get_token("citizen", mobile)
    st, names = list_tools("citizen", ctok)
    check("citizen sees citizen tools only", st == 200 and "apply_for_pension" in names
          and "approve_application" not in names, names)

    info = call_tool("citizen", ctok, "scheme_info")
    check("scheme_info works", "minimum_age_years" in info, info)
    print("      (applications_open = %s)" % info.get("applications_open"))

    young = dict(good, dob="01/01/1990")
    r = call_tool("citizen", ctok, "apply_for_pension", young)
    check("under-age applicant refused", r.get("submitted") is False, r)

    r = call_tool("citizen", ctok, "apply_for_pension", dict(good, ifsc="BAD"))
    check("bad IFSC refused with a clear message", r.get("submitted") is False and r.get("problems"), r)

    r = call_tool("citizen", ctok, "apply_for_pension", good)
    first_ok = r.get("submitted") is True
    check("valid application submitted", first_ok, r)
    if not first_ok:
        print("\nIf the message says the window has closed, the scheme deadline has passed.")
        return finish()

    r = call_tool("citizen", ctok, "apply_for_pension", good)
    check("second application on same mobile blocked", r.get("submitted") is False, r)

    r = call_tool("citizen", ctok, "my_application")
    check("my_application shows PENDING",
          r.get("applications") and r["applications"][0]["status"] == "PENDING", r)

    r = call_tool("citizen", ctok, "withdraw_my_application")
    check("withdraw works", r.get("withdrawn") is True, r)
    r = call_tool("citizen", ctok, "apply_for_pension", good)
    check("can apply again after withdrawing", r.get("submitted") is True, r)

    st, _ = rpc("officer", ctok, "tools/list")
    check("citizen token is rejected by the officer endpoint", st == 401, st)

    otok = get_token("officer")
    st, onames = list_tools("officer", otok)
    check("officer sees officer tools only", st == 200 and "approve_application" in onames
          and "apply_for_pension" not in onames, onames)
    q = call_tool("officer", otok, "queue_summary")
    check("queue_summary works", "by_status" in q, q)

    lst = call_tool("officer", otok, "list_applications", {"mobile": mobile})
    pending = [a for a in lst.get("applications", []) if a["status"] == "PENDING"]
    check("officer finds the citizen's pending application", len(pending) == 1, lst)
    if not pending:
        return finish()
    app_id = pending[0]["id"]

    r = call_tool("officer", otok, "reject_application", {"application_id": app_id, "note": ""})
    check("reject without a reason is refused", r.get("done") is False, r)
    r = call_tool("officer", otok, "approve_application", {"application_id": app_id, "note": "docs ok"})
    check("approve works", r.get("done") is True, r)
    r = call_tool("officer", otok, "approve_application", {"application_id": app_id})
    check("approving an already-decided application is refused", r.get("done") is False, r)
    d = call_tool("officer", otok, "get_application", {"application_id": app_id})
    actions = [a["action"] for a in d.get("audit_trail", [])]
    check("audit trail has SUBMIT and APPROVED", "SUBMIT" in actions and "APPROVED" in actions, actions)
    check("bank account is masked", str(d.get("bank_account", "")).startswith("******"), d.get("bank_account"))

    r = call_tool("citizen", ctok, "my_application")
    check("citizen now sees APPROVED", r["applications"][0]["status"] == "APPROVED", r)
    finish()


def finish():
    print("\n%d of %d checks passed" % (sum(RESULTS), len(RESULTS)))
    sys.exit(0 if all(RESULTS) else 1)


def main(argv):
    if len(argv) >= 2 and argv[1] == "demo":
        return demo()
    if len(argv) >= 3 and argv[1] == "tools":
        role = argv[2]
        mobile = os.environ.get("CITIZEN_MOBILE") or "9" + "".join(random.choice("0123456789") for _ in range(9))
        print(list_tools(role, get_token(role, mobile)))
        return
    if len(argv) >= 4 and argv[1] == "call":
        role, tool = argv[2], argv[3]
        args = json.loads(argv[4]) if len(argv) > 4 else {}
        mobile = os.environ.get("CITIZEN_MOBILE") or "9" + "".join(random.choice("0123456789") for _ in range(9))
        print(json.dumps(call_tool(role, get_token(role, mobile), tool, args), indent=2, default=str))
        return
    print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
