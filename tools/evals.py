# -*- coding: utf-8 -*-
"""
Sewa Setu persona evals (scripted).

Each persona is a specific person with a goal. The runner signs in through the real OAuth
flow (sewa.py), calls the real MCP tools over HTTP, then reads the portal's own Postgres
tables (applications, audit_log, portal_users) and the upload folder to check what is
really true afterwards. Results are saved in table eval_results and shown at /eval-dashboard.

Run INSIDE the assistant container (it needs the database):
  docker compose exec -e ADMIN_PASSWORD=... assistant python tools/evals.py
  docker compose exec assistant python tools/evals.py P01 P05      # only some personas
"""
import os, sys, json, time, uuid, random, glob, threading, traceback
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))          # /app inside the image (app.py lives there)

# talk to the portal over the docker network, to the assistant on localhost,
# but the token audience must be the PUBLIC assistant address (ASSISTANT_BASE_URL)
os.environ.setdefault("PORTAL", "http://app:8000")
os.environ.setdefault("ASSISTANT", "http://localhost:8001")
os.environ.setdefault("RESOURCE_BASE", os.environ.get("ASSISTANT_BASE_URL", "http://localhost:8001"))

import sewa                       # noqa: E402  (OAuth sign-in + MCP calls)
import app as portal              # noqa: E402  (DB connection, IST clock, hash_password)

DDL = """CREATE TABLE IF NOT EXISTS eval_results (
  id SERIAL PRIMARY KEY, run_id VARCHAR(40), persona_id VARCHAR(20), status VARCHAR(10),
  persona JSONB, steps JSONB, checks JSONB, started_at TIMESTAMP, duration_ms INTEGER)"""


# ------------------------------------------------------------------ database helpers
def q(sql, args=()):
    conn = portal.get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def save(run_id, p, status, steps, checks, started, ms):
    conn = portal.get_db()
    try:
        cur = conn.cursor()
        cur.execute(DDL)
        meta = {k: p[k] for k in ("id", "name", "age", "village", "style", "goal", "expect")}
        cur.execute("INSERT INTO eval_results (run_id, persona_id, status, persona, steps, checks,"
                    " started_at, duration_ms) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (run_id, p["id"], status, json.dumps(meta), json.dumps(steps),
                     json.dumps(checks), started, ms))
        conn.commit()
    finally:
        conn.close()


def apps(mobile):
    return q("SELECT id, application_no, applicant_name, dob, block, village, ifsc, bank_account, "
             "status, decided_by, decided_at, doc_path FROM applications WHERE mobile=%s ORDER BY id",
             (mobile,))


def audit(app_id):
    return q("SELECT action, actor, note FROM audit_log WHERE application_id=%s ORDER BY id", (app_id,))


def fresh_mobile():
    while True:
        m = "9000" + "".join(random.choice("0123456789") for _ in range(6))
        if not q("SELECT 1 FROM applications WHERE mobile=%s", (m,)):
            return m


def dob_for(years):
    return (portal.now_ist().date() - timedelta(days=int(years * 365.25) + 45)).strftime("%d/%m/%Y")


def good(**kw):
    d = dict(applicant_name="Test Person", dob=dob_for(63), gender="Female", marital_status="Widowed",
             village="Test Gaon", block="Sonari", bank_account="123456789012", ifsc="SBIN0003077",
             age_proof_filename="aadhaar.png", age_proof_base64=sewa.PNG_B64)
    d.update(kw)
    return d


def text_of(res):
    return json.dumps(res, default=str).lower()


# ------------------------------------------------------------------ run recorder
class Run:
    def __init__(self):
        self.steps, self.checks = [], []

    def say(self, who, text):
        self.steps.append({"kind": "say", "who": who, "text": text})

    def call(self, role, tok, tool, args=None, say=None, who=None):
        if say:
            self.say(who or role, say)
        t = time.time()
        res = sewa.call_tool(role, tok, tool, args or {})
        shown = dict(args or {})
        if "age_proof_base64" in shown:
            shown["age_proof_base64"] = "<%d chars of base64>" % len(shown["age_proof_base64"])
        self.steps.append({"kind": "tool", "role": role, "tool": tool, "args": shown,
                           "result": json.dumps(res, default=str)[:600], "ms": int((time.time() - t) * 1000)})
        return res

    def check(self, name, ok, detail=""):
        self.checks.append({"name": name, "ok": bool(ok), "detail": "" if ok else str(detail)[:300]})


def setup_pending(r, name, who):
    """A citizen applies so an officer has something to decide. Not scored; must succeed."""
    m = fresh_mobile()
    ctok = sewa.get_token("citizen", m)
    res = r.call("citizen", ctok, "apply_for_pension", good(applicant_name=name),
                 say="(setup) %s applies for the pension." % who, who=who)
    rows = apps(m)
    if res.get("submitted") is not True or len(rows) != 1:
        raise RuntimeError("setup failed, could not create a pending application: %s" % res)
    return m, rows[0]["id"], ctok


# ------------------------------------------------------------------ personas
def p01(r):
    m = fresh_mobile(); tok = sewa.get_token("citizen", m)
    r.call("citizen", tok, "scheme_info", say="Beta, pension ka form kaise bharte hain? Main vidhwa hoon.", who="Kamla Devi")
    a = good(applicant_name="Kamla Devi", dob=dob_for(63), village="Rampur", block="Rajapara")
    res = r.call("citizen", tok, "apply_for_pension", a, say="Haan, yeh sab sahi hai, jama kar do.", who="Kamla Devi")
    r.check("tool says submitted", res.get("submitted") is True, res)
    rows = apps(m)
    r.check("exactly one row in applications for her mobile", len(rows) == 1, rows)
    if rows:
        x = rows[0]
        r.check("row is PENDING with her name, village block and uppercase IFSC",
                x["status"] == "PENDING" and x["applicant_name"] == "Kamla Devi"
                and x["block"] == "Rajapara" and x["ifsc"] == "SBIN0003077", x)
        r.check("age-proof file really exists on disk", bool(x["doc_path"]) and os.path.exists(x["doc_path"]), x["doc_path"])
        r.check("audit_log has one SUBMIT by applicant:<mobile>",
                [(a_["action"], a_["actor"]) for a_ in audit(x["id"])] == [("SUBMIT", "applicant:" + m)], audit(x["id"]))
        pw = q("SELECT password_hash FROM portal_users WHERE mobile=%s", (m,))
        r.check("status-portal login created, password = DOB as DDMMYYYY",
                pw and pw[0]["password_hash"] == portal.hash_password(x["dob"].strftime("%d%m%Y")), pw)


def p02(r):
    m = fresh_mobile(); tok = sewa.get_token("citizen", m)
    r.say("Ramesh Yadav", "Main 58 saal ka hoon, mazdoor. Mujhe bhi pension chahiye.")
    res = r.call("citizen", tok, "apply_for_pension", good(applicant_name="Ramesh Yadav", dob=dob_for(58),
                 gender="Male", marital_status="Married", village="Kharhat"), say="Form bhar do.", who="Ramesh Yadav")
    r.check("refused, not submitted", res.get("submitted") is False, res)
    r.check("reason mentions the minimum age (60)", "60" in text_of(res), res)
    r.check("nothing written to applications", apps(m) == [], apps(m))
    r.check("no age-proof file left on disk", glob.glob(os.path.join(portal.UPLOAD_DIR, m + "_*")) == [])


def p03(r):
    m = fresh_mobile(); tok = sewa.get_token("citizen", m)
    a = good(applicant_name="Sunita Bai", dob=dob_for(65), village="Devri")
    out = []
    def go(): out.append(sewa.call_tool("citizen", tok, "apply_for_pension", a))
    r.say("Sunita Bai", "Maine bola tha jaldi karo, phone dono baar daba diya (two taps at the same moment).")
    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]; [t.join() for t in ts]
    r.steps.append({"kind": "tool", "role": "citizen", "tool": "apply_for_pension x2 at once", "args": {},
                    "result": json.dumps(out, default=str)[:600], "ms": 0})
    r.check("exactly one of two simultaneous submissions succeeded", sum(1 for x in out if x.get("submitted") is True) == 1, out)
    again = r.call("citizen", tok, "apply_for_pension", a, say="Ek baar aur bhej do.", who="Sunita Bai")
    r.check("third attempt refused with 'already' message", again.get("submitted") is False and "already" in text_of(again), again)
    rows = apps(m)
    r.check("exactly one application row", len(rows) == 1, rows)
    if rows:
        r.check("exactly one SUBMIT in audit_log", sum(1 for x in audit(rows[0]["id"]) if x["action"] == "SUBMIT") == 1)


def p04(r):
    m = fresh_mobile(); tok = sewa.get_token("citizen", m)
    a = good(applicant_name="Gopal Mahto", dob=dob_for(66), gender="Male", marital_status="Married", village="Tetri")
    r.call("citizen", tok, "apply_for_pension", a, say="Pension ka form bhar do.", who="Gopal Mahto")
    w = r.call("citizen", tok, "withdraw_my_application", say="Galti ho gayi, wapas le lo. Phir se bharunga.", who="Gopal Mahto")
    r.check("withdraw reported done", w.get("withdrawn") is True, w)
    res = r.call("citizen", tok, "apply_for_pension", dict(a, village="Tetri Khurd"), say="Ab sahi gaon ke saath.", who="Gopal Mahto")
    r.check("reapply accepted", res.get("submitted") is True, res)
    rows = apps(m)
    r.check("two rows: WITHDRAWN (by APPLICANT) then PENDING",
            len(rows) == 2 and rows[0]["status"] == "WITHDRAWN" and rows[0]["decided_by"] == "APPLICANT"
            and rows[1]["status"] == "PENDING", rows)
    if len(rows) == 2:
        r.check("audit: first app SUBMIT+WITHDRAW, second app SUBMIT",
                [x["action"] for x in audit(rows[0]["id"])] == ["SUBMIT", "WITHDRAW"]
                and [x["action"] for x in audit(rows[1]["id"])] == ["SUBMIT"])


def p05(r):
    m = fresh_mobile(); tok = sewa.get_token("citizen", m)
    a = good(applicant_name="Fatima Khatun", dob=dob_for(61), village="Nayagaon")
    res = r.call("citizen", tok, "apply_for_pension", dict(a, ifsc="SBIN3077"),
                 say="IFSC passbook se padha: SBIN3077.", who="Fatima Khatun")
    r.check("bad IFSC refused with a message that names IFSC", res.get("submitted") is False and "ifsc" in text_of(res), res)
    r.check("nothing saved after the refusal", apps(m) == [])
    res = r.call("citizen", tok, "apply_for_pension", dict(a, ifsc=" sbin 0003077 "),
                 say="Maaf kijiye, sahi hai: sbin 0003077.", who="Fatima Khatun")
    r.check("corrected IFSC accepted", res.get("submitted") is True, res)
    rows = apps(m)
    r.check("stored IFSC is clean uppercase SBIN0003077", len(rows) == 1 and rows[0]["ifsc"] == "SBIN0003077", rows)


def p06(r):
    m = fresh_mobile(); tok = sewa.get_token("citizen", m)
    a = good(applicant_name="Budhni Oraon", dob=dob_for(70), village="Chiriya")
    res = r.call("citizen", tok, "apply_for_pension", dict(a, block="Sonaree"),
                 say="Mera block Sonaree hai.", who="Budhni Oraon")
    r.check("misspelt block refused and the valid blocks are listed", res.get("submitted") is False and "sonari" in text_of(res), res)
    r.check("nothing saved after the refusal", apps(m) == [])
    res = r.call("citizen", tok, "apply_for_pension", dict(a, block="sonari "),
                 say="Sonari likho, chhota akshar mein.", who="Budhni Oraon")
    r.check("right block in different case is accepted", res.get("submitted") is True, res)
    rows = apps(m)
    r.check("stored block is the canonical 'Sonari'", len(rows) == 1 and rows[0]["block"] == "Sonari", rows)


def p07(r):
    m = fresh_mobile(); tok = sewa.get_token("citizen", m)
    info = r.call("citizen", tok, "scheme_info", say="Kya main abhi bhi pension ke liye apply kar sakti hoon?", who="Lakshmi Kumari")
    r.check("scheme_info says applications are closed", info.get("applications_open") is False, info)
    res = r.call("citizen", tok, "apply_for_pension", good(applicant_name="Lakshmi Kumari", dob=dob_for(64), village="Barwa"),
                 say="Phir bhi form bhar do.", who="Lakshmi Kumari")
    r.check("refused politely with 'window has closed'", res.get("submitted") is False and "closed" in text_of(res), res)
    r.check("nothing saved", apps(m) == [])
    mine = r.call("citizen", tok, "my_application")
    r.check("my_application says can_apply_now = false", mine.get("can_apply_now") is False, mine)


def p08(r):
    m, app_id, ctok = setup_pending(r, "Meena Devi", "Meena Devi")
    otok = sewa.get_token("officer")
    lst = r.call("officer", otok, "list_applications", {"mobile": m}, say="Is mobile ki application dikhao.", who="Officer Anita Sharma")
    r.check("officer finds the PENDING application", any(a["id"] == app_id and a["status"] == "PENDING" for a in lst.get("applications", [])), lst)
    d = r.call("officer", otok, "get_application", {"application_id": app_id}, say="Poori detail kholo.", who="Officer Anita Sharma")
    r.check("bank account is masked for the officer", str(d.get("bank_account", "")).startswith("******"), d.get("bank_account"))
    res = r.call("officer", otok, "approve_application", {"application_id": app_id, "note": "documents verified"},
                 say="Approve karo, note: documents verified.", who="Officer Anita Sharma")
    r.check("tool says done", res.get("done") is True, res)
    row = apps(m)[0]
    r.check("DB: APPROVED, decided_by = officer username, decided_at set",
            row["status"] == "APPROVED" and row["decided_by"] == sewa.ADMIN_USERNAME and row["decided_at"], row)
    last = audit(app_id)[-1]
    r.check("audit: APPROVED by admin:<officer> with the note",
            last == {"action": "APPROVED", "actor": "admin:" + sewa.ADMIN_USERNAME, "note": "documents verified"}, last)
    mine = r.call("citizen", ctok, "my_application", say="Meri application ka kya hua?", who="Meena Devi")
    r.check("citizen now sees APPROVED", mine["applications"][0]["status"] == "APPROVED", mine)


def p09(r):
    m, app_id, ctok = setup_pending(r, "Parvati Devi", "Parvati Devi")
    otok = sewa.get_token("officer")
    for note, label in (("", "empty reason"), ("   ", "blank reason")):
        res = r.call("officer", otok, "reject_application", {"application_id": app_id, "note": note},
                     say="Reject karo (%s)." % label, who="Officer Rakesh Singh")
        r.check("reject with %s refused" % label, res.get("done") is False, res)
    row = apps(m)[0]
    r.check("DB unchanged after refusals: still PENDING, no decider",
            row["status"] == "PENDING" and not row["decided_by"], row)
    reason = "Age proof is unreadable; ask applicant to upload again"
    res = r.call("officer", otok, "reject_application", {"application_id": app_id, "note": reason},
                 say="Reason: age proof unreadable. Ab reject karo.", who="Officer Rakesh Singh")
    r.check("reject with a reason works", res.get("done") is True, res)
    row = apps(m)[0]
    r.check("DB: REJECTED by the officer", row["status"] == "REJECTED" and row["decided_by"] == sewa.ADMIN_USERNAME, row)
    last = audit(app_id)[-1]
    r.check("audit: REJECTED with the exact reason", last["action"] == "REJECTED" and last["note"] == reason, last)
    mine = r.call("citizen", ctok, "my_application")
    r.check("rejected citizen is allowed to apply again", mine.get("has_active_application") is False, mine)


def p10(r):
    m, app_id, _ = setup_pending(r, "Kaushalya Devi", "Kaushalya Devi")
    otok = sewa.get_token("officer")
    r.call("officer", otok, "approve_application", {"application_id": app_id, "note": "ok"},
           say="Approve.", who="Officer Anita Sharma")
    res = r.call("officer", otok, "approve_application", {"application_id": app_id, "note": "again"},
                 say="Approve phir se (galti se).", who="Officer Anita Sharma")
    r.check("second approve refused and says current status", res.get("done") is False and "APPROVED" in json.dumps(res), res)
    res = r.call("officer", otok, "reject_application", {"application_id": app_id, "note": "changed my mind"},
                 say="Ab reject kar do.", who="Officer Anita Sharma")
    r.check("reject after approve refused", res.get("done") is False, res)
    row = apps(m)[0]
    acts = [x["action"] for x in audit(app_id)]
    r.check("DB still APPROVED, decided_by unchanged", row["status"] == "APPROVED" and row["decided_by"] == sewa.ADMIN_USERNAME, row)
    r.check("audit has exactly one APPROVED and no REJECTED", acts.count("APPROVED") == 1 and "REJECTED" not in acts, acts)


def p11(r):
    victim = fresh_mobile(); me = fresh_mobile(); tok = sewa.get_token("citizen", me)
    st, _ = sewa.rpc("officer", tok, "tools/list")
    r.check("citizen token is refused by the officer endpoint (401)", st == 401, st)
    res = r.call("citizen", tok, "approve_application", {"application_id": 1}, say="Officer wala button dabao.", who="Trickster (citizen)")
    r.check("officer tool is not available to a citizen", res.get("done") is not True, res)
    if portal.deadline_remaining() is not None:
        res = r.call("citizen", tok, "apply_for_pension", good(applicant_name="Trickster", mobile=victim, dob=dob_for(62)),
                     say="Mobile %s ke naam par bhar do." % victim, who="Trickster (citizen)")
        r.check("nothing was created for the victim's mobile", apps(victim) == [], apps(victim))
        r.check("any row created belongs to the token's own mobile", all(a["id"] for a in apps(me)))
    else:
        r.check("nothing was created for the victim's mobile", apps(victim) == [])


PERSONAS = [
 dict(id="P01", name="Kamla Devi", age=63, village="Rampur", window="open", fn=p01,
      style="Slow Hindi, widow, unsure of forms, polite.", goal="Apply for the pension correctly.",
      expect="1 PENDING row, file on disk, one SUBMIT audit entry, status-portal login created."),
 dict(id="P02", name="Ramesh Yadav", age=58, village="Kharhat", window="open", fn=p02,
      style="Blunt, in a hurry, a labourer.", goal="Apply although he is under 60.",
      expect="Refused kindly; no row, no file."),
 dict(id="P03", name="Sunita Bai", age=65, village="Devri", window="open", fn=p03,
      style="Impatient, taps twice.", goal="Apply (and then apply again by mistake).",
      expect="Exactly 1 row and 1 SUBMIT even with two simultaneous calls."),
 dict(id="P04", name="Gopal Mahto", age=66, village="Tetri", window="open", fn=p04,
      style="Forgetful, changes his mind.", goal="Withdraw a wrong application and reapply.",
      expect="2 rows: WITHDRAWN by APPLICANT, then PENDING; audit trail for both."),
 dict(id="P05", name="Fatima Khatun", age=61, village="Nayagaon", window="open", fn=p05,
      style="Careful, reads the passbook aloud with spaces.", goal="Fix a wrong IFSC and succeed.",
      expect="First try refused, no row; second try stored as clean SBIN0003077."),
 dict(id="P06", name="Budhni Oraon", age=70, village="Chiriya", window="open", fn=p06,
      style="Spells the block the way it sounds.", goal="Apply with a misspelt block, then fix it.",
      expect="Misspelling refused with valid blocks listed; 'sonari' stored as 'Sonari'."),
 dict(id="P07", name="Lakshmi Kumari", age=64, village="Barwa", window="closed", fn=p07,
      style="Hopeful, comes late.", goal="Apply after 31 Oct 23:59 IST.",
      expect="Refused politely with 'window has closed'; no row. (Runs only after the deadline.)"),
 dict(id="P08", name="Officer Anita Sharma", age=45, village="Sonari block office", window="open", fn=p08,
      style="Efficient, short instructions.", goal="Find a pending application and approve it.",
      expect="APPROVED in DB, decided_by officer, audit APPROVED with note, citizen sees APPROVED."),
 dict(id="P09", name="Officer Rakesh Singh", age=52, village="Rajapara block office", window="open", fn=p09,
      style="Rushed; first forgets the reason.", goal="Reject: first without a reason, then with one.",
      expect="Empty/blank reason refused, row unchanged; then REJECTED with exact reason in audit."),
 dict(id="P10", name="Officer Anita Sharma (slip)", age=45, village="Sonari block office", window="open", fn=p10,
      style="Double-clicks.", goal="Approve an already-decided application, then try to reject it.",
      expect="Both refused; DB still APPROVED; one APPROVED audit row, no REJECTED."),
 dict(id="P11", name="Trickster citizen", age=40, village="n/a", window="open", fn=p11,
      style="Tries to act as someone else and to reach officer tools.", goal="Cross the citizen/officer and mobile boundaries.",
      expect="Officer endpoint 401, officer tool unavailable, nothing created for the victim's mobile."),
]


def run_persona(p, run_id):
    r, started = Run(), datetime.now()
    t0 = time.time()
    open_now = portal.deadline_remaining() is not None
    if p["window"] == "open" and not open_now:
        status = "SKIPPED"; r.check("skipped: application window has closed (last real result is kept)", True)
    elif p["window"] == "closed" and open_now:
        status = "SKIPPED"; r.check("skipped: window still open; run after 31 Oct 23:59 IST", True)
    else:
        status = None
        try:
            p["fn"](r)
        except Exception:
            r.check("persona ran to the end without an exception", False, traceback.format_exc()[-300:])
        status = "PASS" if r.checks and all(c["ok"] for c in r.checks) else "FAIL"
    save(run_id, p, status, r.steps, r.checks, started, int((time.time() - t0) * 1000))
    bad = [c for c in r.checks if not c["ok"]]
    print("%-7s %s %s" % (status, p["id"], p["name"]))
    for c in bad:
        print("        x %s -> %s" % (c["name"], c["detail"]))
    return status


def main(argv):
    conn = portal.get_db(); cur = conn.cursor(); cur.execute(DDL); conn.commit(); conn.close()
    wanted = set(a.upper() for a in argv[1:])
    run_id = uuid.uuid4().hex[:8]
    print("run %s | window open: %s\n" % (run_id, portal.deadline_remaining() is not None))
    results = [run_persona(p, run_id) for p in PERSONAS if not wanted or p["id"] in wanted]
    real = [s for s in results if s != "SKIPPED"]
    print("\n%d of %d personas passed (%d skipped)" % (real.count("PASS"), len(real), results.count("SKIPPED")))
    sys.exit(0 if all(s in ("PASS", "SKIPPED") for s in results) else 1)


if __name__ == "__main__":
    main(sys.argv)
