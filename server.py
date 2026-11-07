#!/usr/bin/env python3
"""NCI backend (stdlib only): Teams-style chat -> capture pipeline -> bitemporal assertion store -> agent API.
Run: python server.py   (open http://localhost:8080). Optional: configure a GAIK LLM provider (see README) to enable the curator slow path."""
import json, re, sqlite3, os, time, threading, hashlib, base64, hmac, datetime as dt, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

R = os.path.dirname(os.path.abspath(__file__)); LK = threading.RLock()
API_VERSION = 2  # the UI checks this and warns when an older server process is still running
if os.path.exists(os.path.join(R, ".env")):  # minimal .env loader
    for _l in open(os.path.join(R, ".env")):
        if "=" in _l and not _l.lstrip().startswith("#"):
            _k, _v = _l.split("=", 1); _v = re.split(r"\s+#", _v.strip(), 1)[0].strip().strip('"')  # drop inline comments
            if _v: os.environ.setdefault(_k.strip(), _v)  # empty values stay unset
DBP = os.environ.get("NCI_DB", os.path.join(R, "nci.db")); db = sqlite3.connect(DBP, check_same_thread=False)
import gaik_bridge as GB
import llm as LLM
import orchestrator as OM
try: from gaik.software_components.RAG.relevance_gate import RelevanceGate; GATE = RelevanceGate(floor=0.34, lower_is_better=False)
except Exception: GATE = None
ROLES = json.load(open(os.path.join(R, "data", "roles.json")))  # directory: author -> role
MEMBERS = json.load(open(os.path.join(R, "data", "members.json")))  # channel -> members (source ACL)
ALLOW = set(os.environ.get("NCI_CAPTURE_CHANNELS", "General,Transport,Maintenance").split(","))  # capture only from approved channels
db.execute("create table if not exists d(t text,id text,j text,primary key(t,id))")
def put(t, i, o):
    with LK: db.execute("insert or replace into d values(?,?,?)", (t, i, json.dumps(o))); db.commit()
def rows(t):
    with LK: return [json.loads(r[0]) for r in db.execute("select j from d where t=? order by length(id),id", (t,))]
def one(t, i):
    with LK: r = db.execute("select j from d where t=? and id=?", (t, i)).fetchone()
    return json.loads(r[0]) if r else None

# Registry standing in for the Physical / Runtime layers (inventory + change log)
ENT = {"HEL-DC-07": ("Site", []), "TRN-041": ("Transport node", []), "TRN-042": ("Transport node", []), "TRN-043": ("Transport node", []),
       "FW-8.4.1": ("Firmware", []), "FW-8.4.2": ("Firmware", []), "CAB-17": ("Cabinet", []),
       "GATE-NORTH-01": ("Access barrier", ["north service barrier", "north barrier"])}
REL = [("HEL-DC-07", "contains", "TRN-041"), ("HEL-DC-07", "contains", "TRN-042"), ("HEL-DC-07", "contains", "TRN-043"), ("TRN-042", "installed in", "CAB-17"), ("HEL-DC-07", "access controlled by", "GATE-NORTH-01")]
CHG = [("TRN-042", "2026-09-02", "FW-8.4.1"), ("TRN-042", "2026-11-03", "FW-8.4.2")]  # as-running firmware history
AUTH = {"access": ["Site coordinator"], "validation_gap": ["Network automation engineer"]}  # roles that can verify a kind
SLOT = {"workaround": 1, "access": 2, "hypothesis": 3, "validation_gap": 4}
ST = ["Observed", "Corroborated", "Verified", "Approved for automation"]
ORC = OM.Orch(put, rows, {k: v[1] for k, v in ENT.items()})  # created after ENT below is fixed; ents filled lazily
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
PRX = r"\b(rule of thumb|never|always|in my experience|lesson learned|tip:)\b"  # wording of field know-how
IDRE = r"\b(?:TRN-\d{3}|CAB-\d+|GATE-[A-Z]+-\d+|HEL-DC-\d+)\b"
today = lambda: dt.date.today().isoformat()
def fw_at(e, T):
    c = [x for x in CHG if x[0] == e and x[1] <= T]; return c[-1][2] if c else None
def chg_date(e, fw=None, T=None):
    c = [x for x in CHG if x[0] == e and (fw is None or x[2] == fw) and (T is None or x[1] <= T)]
    return (c[0] if fw else c[-1])[1] if c else None

def score(a):
    roles = [x["role"] for x in a["authors"]]; names = {x["name"] for x in a["authors"]}
    s = 2 if any(r in AUTH.get(a["kind"], []) for r in roles) else 1 if len(names) > 1 and a["kind"] in ("workaround", "access") else 0
    return min(2, s + a.get("boost", 0))

def newid(kind, A):
    used = {a["id"] for a in A}; p = SLOT.get(kind)
    if p and "C-%03d" % p not in used: return "C-%03d" % p
    n = 5
    while "C-%03d" % n in used: n += 1
    return "C-%03d" % n

def upsert(m, kind, ent, rel, nodes, stmt, scope, quote, vf=None, explicit=False, excl=()):
    A = rows("a"); key = "%s|%s|%s" % (kind, ent, rel); a = next((x for x in A if x["key"] == key), None); created = a is None
    day = m["ts"][:10]
    if created:
        a = dict(id=newid(kind, A), key=key, kind=kind, entity=ent, rel=rel, nodes=nodes, statement=stmt, scope=scope, excl=[], vf=vf or day,
                 vt=None, learned=day, lc=day, ch=[], st=0, boost=0, authors=[], ev=[], quotes=[], views=[], dis=False, src=m.get("src", "rules"))
    a["lc"] = max(a.get("lc") or "", day); a["ch"] = sorted(set(a.get("ch", [])) | {m.get("channel", "Transport")})
    if m["id"] not in a["ev"]:
        a["ev"].append(m["id"]); a["quotes"].append({"msg": m["id"], "author": m["author"], "text": quote})
    if not any(x["name"] == m["author"] for x in a["authors"]): a["authors"].append({"name": m["author"], "role": m["role"]})
    if explicit and vf: a["vf"] = vf
    a["excl"] = sorted(set(a["excl"]) | set(excl)); a["st"] = score(a); put("a", a["id"], a)
    return ("created" if created else "updated"), a

def pipeline(m):
    t0 = time.perf_counter(); S = {}; out = []; note = []; link = []
    def lap(k): S[k] = round((time.perf_counter() - t0) * 1000 - sum(S.values()), 2)
    ch = m.get("channel", "Transport"); ctx = one("ctx", ch) or {}; day = m["ts"][:10]
    txt = re.sub(r"\s+", " ", m["text"]).strip(); low = txt.lower(); lap("normalise")
    raw = list(dict.fromkeys(re.findall(IDRE, txt))); ents = [e for e in raw if e in ENT]; unk = [e for e in raw if e not in ENT]
    fws = [f for f in dict.fromkeys("FW-" + x for x in re.findall(r"\b(?:FW-)?(\d\.\d\.\d)\b", txt)) if f in ENT]
    ents += [e for e, (_, al) in ENT.items() if e not in ents and any(re.search(r"\b" + a + r"\b", low) for a in al)]
    bare = ["TRN-" + n for n in re.findall(r"(?<![-\d.])(0\d{2})(?![\d.])", txt) if "trn" in low and "TRN-" + n in ENT and "TRN-" + n not in ents]
    trn = [e for e in ents if e.startswith("TRN")]; cab = [e for e in ents if e.startswith("CAB")]; gate = [e for e in ents if e.startswith("GATE")]
    et = trn[0] if trn else ctx.get("trn"); fw = fws[0] if fws else ctx.get("fw")
    amb = [{"phrase": "the cabinet", "cands": [e for e, v in ENT.items() if v[0] == "Cabinet"]}] if re.search(r"\bthe cabinet\b", low) and not cab else []
    for x in amb:
        i = "%s:%s" % (m["id"], x["phrase"]); put("amb", i, dict(id=i, msg=m["id"], status="pending", **x))
    if trn: ctx["trn"] = trn[0]
    if fws: ctx["fw"] = fws[-1]
    put("ctx", ch, ctx); lap("resolve")
    sents = re.split(r"(?<=[.!?])\s+", txt)
    q = lambda rx: next((s for s in sents if re.search(rx, s, re.I)), sents[0])
    sec = re.search(r"(\d+)[- ]second", low); n = sec.group(1) if sec else None
    neg = re.search(r"(haven[’']t|have not|has not|not)\s+(yet\s+)?(re-?tested|revalidated|validated)", low)
    wk = lambda: next((a for a in reversed(rows("a")) if a["kind"] == "workaround" and a["entity"] == et), None)
    hit = False
    if neg and fw and et and n:
        out.append(upsert(m, "validation_gap", et, fw, [et, fw], "The %s-second workaround has not been revalidated on %s." % (n, fw), "%s + %s" % (et, fw), q(r"tested|validated"), day)); hit = True
    elif re.search(r"only tested|wouldn[’']t apply", low) and et and bare and wk():
        a = wk(); out.append(upsert(m, "workaround", et, a["rel"], a["nodes"], a["statement"], a["scope"], q(r"only tested|wouldn"), excl=bare)); hit = True
    elif n and et and fw and re.search(r"wait|workaround|reboot", low):
        out.append(upsert(m, "workaround", et, fw, [et, fw], "On %s running %s, wait %s seconds after a warm reboot before judging management-plane reachability; an immediate flap may self-clear." % (et, fw, n),
                          "%s on %s only (exact node + firmware)" % (et, fw), q(r"wait|workaround"), chg_date(et, fw) or day)); hit = True
    elif re.search(r"\bdisagree\b|marginal|voltage dip", low) and et:
        c = cab[0] if cab else "power feed"
        k, a = upsert(m, "hypothesis", et, c, [et] + cab, "The management-plane flap on %s may be caused by a marginal power feed (%s) rather than firmware behaviour." % (et, c),
                      "%s / %s" % (et, c), q(r"marginal|disagree|voltage"), chg_date(et, None, day) or day)
        a["views"].append({"author": m["author"], "view": "power feed may be the cause", "msg": m["id"]}); put("a", a["id"], a); out.append((k, a))
        if wk(): w = wk(); w["views"].append({"author": m["author"], "view": "disagrees that firmware is the only cause", "msg": m["id"]}); put("a", w["id"], w)
        hit = True
    elif "firmware" in low and et and not sec and wk():
        w = wk(); w["views"].append({"author": m["author"], "view": "timing looks firmware-related", "msg": m["id"]}); put("a", w["id"], w); note.append("opinion attached to " + w["id"]); link.append(w); hit = True
    elif gate and re.search(r"\b(lock|key|escort|desk|barrier)\b", low):
        d = re.search(r"\b(\d{1,2}) (%s)\b" % "|".join(MONTHS), low); vf = None
        if d: vf = "%s-%02d-%02d" % (day[:4], MONTHS.index(d.group(2)) + 1, int(d.group(1)))
        out.append(upsert(m, "access", gate[0], "constraint", [gate[0], "HEL-DC-07"], "%s requires a site-desk escort; the previous shared key no longer works." % gate[0], gate[0] + " at HEL-DC-07",
                          q(r"escort|key|lock|desk"), vf, bool(vf))); hit = True
    elif re.search(PRX, low) and ents:  # tacit expert know-how: rules of thumb, "always/never", "in my experience"
        main = next((e for e in ents if ENT[e][0] != "Site"), ents[0]); nodes = list(dict.fromkeys([main] + [e for e in ents if ENT[e][0] != "Site"]))
        out.append(upsert(m, "practice", main, m["id"], nodes, txt, " + ".join(nodes), q(PRX))); hit = True
    lap("classify")
    lap("consolidate")
    bad = [(a["id"], c["msg"]) for _, a in out for c in a["quotes"] if c["msg"] == m["id"] and re.sub(r"\s+", " ", c["text"]) not in txt]
    lap("verify")
    return dict(stages=S, ms=round(sum(S.values()), 2), created=[a["id"] for k, a in out if k == "created"], updated=[a["id"] for k, a in out if k == "updated"],
                unresolved=unk, ambiguous=amb, quote_failures=bad, notes=note, linked=[w["id"] for w in link],
                ents=sorted(set(ents + fws + [n for _, a in out for n in a["nodes"]] + [n for w in link for n in w["nodes"]])), items=[dict(id=a["id"], action=k, kind=a["kind"], statement=a["statement"], state=ST[a["st"]]) for k, a in out])

def ev(a, ent, fw, T, K):
    if a["learned"] > K: return "not yet known to the system on " + K
    if a["dis"]: return "disputed by a person; held for review"
    if T < a["vf"]: return "not yet valid (starts %s)" % a["vf"]
    if a["vt"] and T >= a["vt"]: return "expired " + a["vt"]
    k = a["kind"]
    if k == "workaround":
        if ent != a["entity"]: return "scoped to %s only; %s%s" % (a["entity"], ent, " explicitly excluded" if ent in a["excl"] else " not validated")
        if fw != a["rel"]: return "observed on %s only; not validated on %s" % (a["rel"], fw or "unknown firmware")
    if k == "hypothesis" and ent != a["entity"]: return "concerns %s only" % a["entity"]
    if k == "practice" and ent not in a["nodes"] and ENT.get(ent, ("",))[0] == "Transport node" and any(ENT.get(n, ("",))[0] == "Transport node" for n in a["nodes"]):
        return "concerns %s only" % ", ".join(a["nodes"])
    if k == "validation_gap":
        if ent != a["entity"]: return "concerns %s only" % a["entity"]
        if fw != a["rel"]: return "applies only while running " + a["rel"]
    return None

TTL = {"issue": 90, "decision": 180, "practice": 365, "workaround": 180, "access": 365, "hypothesis": 60, "validation_gap": 365, "observation": 120}  # days until an assertion needs reconfirming
def ttl(k): return int(os.environ.get("NCI_TTL_DAYS_" + k.upper(), TTL.get(k, 180)))
def last_conf(a): return a.get("lc") or a["learned"]
def freshness(a, asof):
    age = (dt.date.fromisoformat(asof) - dt.date.fromisoformat(last_conf(a))).days; t = ttl(a["kind"])
    return ("fresh" if age <= t / 2 else "ageing" if age <= t else "stale"), age
def fnote(a, asof):
    f, age = freshness(a, asof)
    if f == "fresh": return ""
    who = ", ".join(x["name"] for x in a["authors"][:2])
    return " Last confirmed %s (%d days ago)%s. Ask %s whether it still holds, then press Reconfirm." % (last_conf(a), age, "; possibly outdated" if f == "stale" else "", who)

def chat_of(c): return one("chat", c[5:]) if c and c.startswith("chat:") else None
def is_nci_chat(c): return bool(c) and c.startswith("chat:nci-")
def allowed(c): return c in ALLOW or (bool(c) and c.startswith("chat:") and not is_nci_chat(c) and (os.environ.get("NCI_CAPTURE_CHATS") or "1") != "0")
def members(c):
    if c and c.startswith("chat:"): x = chat_of(c); return set(x["members"]) if x else set()
    return set(MEMBERS.get(c, []))
def is_member(viewer, a): return viewer is None or any(viewer in members(c) for c in (a.get("ch") or ["Transport"]))
def can_see_channel(viewer, ch):
    if viewer is None: return True
    if ch and ch.startswith("chat:"): return viewer in members(ch)
    return ch not in MEMBERS or viewer in MEMBERS[ch]
def is_ops(viewer): return viewer is None or any(viewer in MEMBERS.get(c, []) for c in ALLOW if c != "General")
def vis(a, viewer):
    """Source-ACL at retrieval time. Channel members see everything. Others see the derived knowledge only, with roles instead of names and no raw quotes (or nothing, by policy)."""
    if is_member(viewer, a): return a
    if (os.environ.get("NCI_ORG_VISIBILITY") or "assertions") != "assertions": return None
    b = dict(a); b["authors"] = [{"name": x["role"], "role": x["role"]} for x in a["authors"]]; b["quotes"] = []; b["ev"] = []
    b["views"] = [dict(v, author=ROLES.get(v["author"], "a team member"), msg=None) for v in a["views"]]; b["restricted"] = True; return b

def view(a, T=None, K=None, ent=None, fw=None, asof=None):
    asof = asof or today(); T = T or asof; K = K or "9999"; ent = ent or "TRN-042"; fw = fw or fw_at(ent, T); f, age = freshness(a, asof)
    return dict(a, state=ST[a["st"]], applicable=ev(a, ent, fw, T, K) is None, why=ev(a, ent, fw, T, K), freshness=f, age_days=age, last_confirmed=last_conf(a))

def say(a, asof=None):
    w = [x["name"] for x in a["authors"]]; s = a["statement"]; k = a["kind"]
    if k == "workaround": t = "%s Reported by %s%s; advisory only. Do not suppress alarms or skip safety checks. [%s]" % (s, w[0], ", corroborated by " + w[1] if len(w) > 1 else "", a["id"])
    elif k == "access": t = "%s Effective %s (%s). Call the site desk before dispatch. [%s]" % (s, a["vf"], w[-1], a["id"])
    elif k == "hypothesis": t = "%s Raised by %s; unverified. Compare power telemetry before recommending physical intervention. [%s]" % (s, w[0], a["id"])
    elif k == "practice": t = "%s Shared from experience by %s; advisory, check on site before acting. [%s]" % (s, w[0], a["id"])
    elif k == "validation_gap": t = "%s Do not apply the workaround automatically on %s. [%s]" % (s, a["rel"], a["id"])
    elif k in ("observation", "issue", "decision"): t = "%s Reported by %s; extracted by an LLM from chat, not yet reviewed by a person. [%s]" % (s, w[0], a["id"])
    else: t = "%s (LLM-extracted, unreviewed) [%s]" % (s, a["id"])
    return t + (fnote(a, asof) if asof else "")

def orc_sync():
    if ORC.sync(rows("a"), {m["id"]: m for m in rows("m")}): ORC.calibrate(semantic=False)

def subtree(root):
    seen, q = [root], [root]
    while q:
        x = q.pop()
        for a, r, b in REL:
            if a == x and b not in seen: seen.append(b); q.append(b)
    return seen

def ask(qs, ent=None, fw=None, T=None, K=None, viewer=None, asof=None):
    low = qs.lower(); asof = asof or today(); T = T or asof; K = K or "9999"; ctx = one("ctx", "Transport") or {}
    d = re.search(r"\b(\d{1,2}) (%s)\w* (\d{4})" % "|".join(m[:3] for m in MONTHS), low)
    if d: T = "%s-%02d-%02d" % (d.group(3), [m[:3] for m in MONTHS].index(d.group(2)) + 1, int(d.group(1)))
    e = re.search(r"TRN-\d{3}", qs, re.I); f = re.search(r"(\d\.\d\.\d)", qs); explicit_ent = bool(ent or e); explicit_fw = bool(f or fw)
    ent = ent or (e.group(0).upper() if e else ctx.get("trn", "TRN-042")); fwp = ("FW-" + f.group(1)) if f else (fw if fw and fw.startswith("FW") else ("FW-" + fw if fw else None)); fw = fwp or fw_at(ent, T)
    who = next((p for p in ROLES if re.search(r"\b%s\b" % p.lower(), low) and p != viewer), None)  # "what does Erkki know"
    it = "b" if (who and re.search(r"know|learn|said|say|share|told|tell|from|expert", low)) or re.search(r"brief|overview|handover|hand over|onboard|new here|taking over|know about|everything about|catch me up|catch up|risk", low) else \
         "s" if re.search(r"learn|who said|certain|summar", low) else "d" if re.search(r"dispatch|barrier|gate|access|escort|site|key", low) else \
         "p" if re.search(r"power|feed|cab-", low) else "w" if re.search(r"workaround|90|reboot|reuse|valid|flap|previous|firmware", low) else "x"
    A = [a for a in rows("a")]
    if who and it == "b": A = [a for a in A if any(x["name"] == who for x in a["authors"])]
    A = [b for b in (vis(a, viewer) for a in A) if b is not None]
    orc_sync(); res = ORC.search(qs, k=5) if it in ("w", "p", "x") else None; assumed = None
    if res and res["ids"] and not explicit_ent:
        top = next((a for a in A if a["id"] == res["ids"][0][0]), None)
        if top and ENT.get(top["entity"], ("",))[0] == "Transport node": ent = top["entity"]; assumed = ent; fw = fwp or fw_at(ent, T)
    trace = []; used = []; extra = []; head = None
    if it == "b":
        rt = next((x for x in re.findall(IDRE, qs.upper()) if x in ENT), "HEL-DC-07"); tree = subtree(rt)
        mine = [a for a in A if a["entity"] in tree or any(n in tree for n in a["nodes"])]; now, openq, hist = [], [], []
        for a in mine:
            w = ev(a, a["entity"], fw_at(a["entity"], T), T, K); trace.append(dict(id=a["id"], decision="excluded" if w else "used", reason=w or "currently applies to %s" % a["entity"]))
            (hist if w else openq if a["kind"] in ("hypothesis", "validation_gap") else now).append((a, w))
        used = [a for a, _ in now + openq]; hidden = [x["id"] for x in A if x not in mine]
        trace += [dict(id=i, decision="skipped", reason="outside %s" % rt) for i in hidden]
        quiet = [] if who else [x for x in tree if ENT[x][0] in ("Transport node", "Cabinet", "Access barrier") and not any(x == a["entity"] or x in a["nodes"] for a in mine)]
        now = [x for x in now if x[0]["kind"] != "practice"] + [x for x in now if x[0]["kind"] == "practice"]
        extra = [("Expert know-how · " if a["kind"] == "practice" else "Currently applies · ") + say(a, asof) for a, _ in now] + ["Open question · " + say(a, asof) for a, _ in openq] + \
                ["Historical, not applicable now · [%s] %s (%s). Originally reported by %s." % (a["id"], a["statement"], w, ", ".join(x["name"] for x in a["authors"][:2])) for a, w in hist] + (["No captured context yet for: %s. Nobody has written anything down about them in the captured channels." % ", ".join(quiet)] if quiet else [])
        nstale = sum(freshness(a, asof)[0] == "stale" for a in used)
        head = ("What %s has shared about %s, as of %s%s" % (ROLES[who] if any(a.get("restricted") for a in mine) else who, rt, asof, "; %d item(s) need reconfirmation" % nstale if nstale else "") if who else
                "Handover brief for %s as of %s%s" % (rt, asof, "; %d item(s) need reconfirmation" % nstale if nstale else "")) if (used or hist) else                ("Nothing captured from %s about %s." % (who, rt) if who else "Nothing captured yet for %s." % rt)
    else:
        ids = [i for i, _ in res["ids"]] if (it == "x" and res and res["ok"]) else []
        rel = {"s": lambda a: True, "d": lambda a: a["kind"] in ("access", "hypothesis", "workaround", "practice"), "p": lambda a: a["kind"] in ("hypothesis", "workaround") or (a["kind"] == "practice" and any(n.startswith("CAB") for n in a["nodes"])),
               "w": lambda a: a["kind"] in ("workaround", "validation_gap", "hypothesis"), "x": lambda a: a["id"] in ids}[it]
        for a in (sorted(A, key=lambda a: ids.index(a["id"]) if a["id"] in ids else 99) if it == "x" else A):
            if not rel(a): trace.append(dict(id=a["id"], decision="skipped", reason="not relevant to this question")); continue
            w = ev(a, ent, fw, T, K); trace.append(dict(id=a["id"], decision="excluded" if w else "used", reason=w or "valid at %s, bound to %s / %s" % (T, ent, fw or "no firmware")))
            if not w: used.append(a)
    ok = lambda k: any(a["kind"] == k for a in used)
    if it == "x" and not used and any(t["decision"] == "excluded" for t in trace):
        used_ex = [a for a in A if any(t["id"] == a["id"] and t["decision"] == "excluded" for t in trace)]
        head = "Only earlier knowledge matches, and it does not apply now."; paras = ["Earlier knowledge, not applicable now · [%s] %s (%s). Originally reported by %s." % (a["id"], a["statement"], ev(a, ent, fw, T, K), ", ".join(x["name"] for x in a["authors"][:2])) for a in used_ex]
    elif not used and not (it == "b" and extra):
        head = head or "Nothing valid in the context store covers that."; paras = ["I won’t guess. " + ("No assertion is relevant to this question." if it == "x" else "Everything relevant is outside the entity, firmware or time scope of this incident.")]
        why = [t for t in trace if t["decision"] == "excluded"]
        if why: paras.append("Not used: " + "; ".join("%s (%s)" % (t["id"], t["reason"]) for t in why) + ".")
    elif it == "b": paras = extra
    else:
        head = {"w": "Yes, narrowly." if ok("workaround") else "Not safely." if ok("validation_gap") else "No applicable workaround on record.", "p": "Power feed is an open hypothesis.",
                "d": "Plan for an escort before dispatch.", "s": "What we know, who said it, and how sure we are.", "x": "Related context found."}[it]
        paras = [say(a, asof) + (" Valid from %s; %s." % (a["vf"], ST[a["st"]]) if it == "s" else "") for a in used]
        if it == "w" and not ok("workaround") and ok("validation_gap"):
            g = next((a for a in A if a["kind"] == "workaround"), None)
            if g: paras.insert(0, "A field-observed workaround exists (%s): %s It was observed on %s only; it is not validated for %s." % (g["id"], g["statement"], g["rel"], fw or "other firmware"))
        views = list({(v["author"], v["msg"]): v for a in used for v in a["views"]}.values())
        if it == "x":
            for a in A:
                w = ev(a, ent, fw, T, K) if a["id"] in ids and a not in used else None
                if w: paras.append("Earlier knowledge, not applicable now · [%s] %s (%s). Originally reported by %s." % (a["id"], a["statement"], w, ", ".join(x["name"] for x in a["authors"][:2])))
        if views and it in ("w", "p", "s", "x"): paras.append("Competing views kept: " + "; ".join("%s: %s" % (v["author"], v["view"]) for v in views) + ".")
    if assumed and (used or (head or "").startswith("Only earlier")): paras.insert(0, "Assuming you mean %s, the device the matching knowledge is about. Say if you meant another one." % assumed)
    if assumed and not used and (head or "").startswith("Only earlier"): paras.append("For everything captured about %s, ask for a handover brief on %s." % (assumed, assumed))
    if any(freshness(a, asof)[0] == "stale" for a in (used or locals().get("used_ex") or [])) and it != "b": paras.insert(0, "Heads-up: some of this was last confirmed longer ago than its review period. Treat it as a lead to verify, not a fact.")
    src = [dict(id=a["id"], state=ST[a["st"]], authors=[x["name"] for x in a["authors"]], valid_from=a["vf"], quotes=a["quotes"], freshness=freshness(a, asof)[0], last_confirmed=last_conf(a), restricted=bool(a.get("restricted"))) for a in (used or locals().get("used_ex") or [])]
    return dict(headline=head, paras=paras, trace=trace, sources=src, level="advisory", params=dict(entity=ent, firmware=fw, T=T, asof=asof, intent=it,
                retrieval=dict(arms=res["arms"], ranker=res["ranker"], coverage=res["best_cov"], semantic=res["best_sem"], passed=res["ok"]) if res else None, viewer_restricted=any(a.get("restricted") for a in used)))

def search(qs, T=None, viewer=None, asof=None):
    asof = asof or today(); orc_sync(); byid = {a["id"]: a for a in rows("a")}; res = ORC.search(qs, k=8); out = []
    for i, _ in res["ids"]:
        a = vis(byid[i], viewer)
        if a is not None: out.append(view(a, T, asof=asof))
    toks = [t for t in re.findall(r"[a-z0-9.-]{3,}", qs.lower())]; sc = lambda s: sum(t in s.lower() for t in toks)
    Mg = [(sc(m["text"] + " " + m["author"]), m) for m in rows("m") if m["author"] != "NCI" and not is_nci_chat(m.get("channel")) and can_see_channel(viewer, m.get("channel", "Transport"))]
    return dict(assertions=out, messages=[m for s, m in sorted(Mg, key=lambda x: -x[0]) if s][:8], retrieval=dict(arms=res["arms"], ranker=res["ranker"], passed=res["ok"]))

def dashboard(viewer=None, asof=None):
    asof = asof or today(); M = [m for m in rows("m") if m["author"] != "NCI" and not is_nci_chat(m.get("channel"))]; A = rows("a"); runs = rows("run"); sa = {}
    for r in runs:
        for k, v in r["stages"].items(): sa.setdefault(k, []).append(v)
    qs = [c for a in A for c in a["quotes"]]; amb = [x for x in rows("amb") if x["status"] == "pending"]
    cov = {e: sum(e in a["nodes"] for a in A) for e in ENT}
    att = [dict(sev="warn", text="%s is an unverified hypothesis (%s)" % (a["id"], a["scope"])) for a in A if a["kind"] == "hypothesis" and not a["dis"]] + \
          [dict(sev="warn", text="%s: %s" % (a["id"], a["statement"])) for a in A if a["kind"] == "validation_gap"] + \
          [dict(sev="bad", text="%s disputed by a person" % a["id"]) for a in A if a["dis"]] + \
          [dict(sev="warn", text="Ambiguous reference “%s” in %s; candidates: %s" % (x["phrase"], x["msg"], ", ".join(x["cands"])), amb=x["id"]) for x in amb] + \
          [dict(sev="info", text="%s refined by the GAIK curator, awaiting review" % a["id"]) for a in A if a["src"] == "gaik"]
    att += [dict(sev="warn" if freshness(a, asof)[0] == "stale" else "info", text="%s last confirmed %s (%d days ago), review period %d days" % (a["id"], last_conf(a), freshness(a, asof)[1], ttl(a["kind"]))) for a in A if freshness(a, asof)[0] != "fresh" and not a["dis"]]
    hold = {}
    for a in A:
        for x in a["authors"]:
            h = hold.setdefault(x["name"], dict(name=x["name"] if is_ops(viewer) else x["role"], role=x["role"], assertions=0, sole=0, kinds={}, messages=0))
            h["assertions"] += 1; h["sole"] += len(a["authors"]) == 1; h["kinds"][a["kind"]] = h["kinds"].get(a["kind"], 0) + 1
    for m in M:
        if m["author"] in hold: hold[m["author"]]["messages"] += 1
    holders = sorted(hold.values(), key=lambda h: (-h["assertions"], -h["sole"]))
    cur = one("cur", "last") or {}
    att += [dict(sev="warn", text="Conflict · %s: %s" % (c["topic"], c["description"])) for c in cur.get("conflicts", []) if c["status"] == "unresolved"] + \
           [dict(sev="info", text="No source covers: " + x) for x in cur.get("missing", [])] + ([dict(sev="bad", text="Curation error: " + cur["error"])] if cur.get("error") else [])
    return dict(kpi=dict(messages=len(M), assertions=len(A), bot=len(rows("m")) - len(M), runs=len(runs), avg_ms=round(sum(r["ms"] for r in runs) / len(runs), 2) if runs else 0,
                         quotes_ok=len(qs)), states=[sum(a["st"] == i for a in A) for i in range(4)], kinds={k: sum(a["kind"] == k for a in A) for k in sorted({a["kind"] for a in A})},
                questions=len(rows("qlog")), stage_avg={k: round(sum(v) / len(v), 3) for k, v in sa.items()}, coverage=cov, attention=att, runs=[dict(r, author=r["author"] if is_ops(viewer) else "(restricted)") for r in list(reversed(runs))[:12]], health=health(), holders=holders, curation=cur if is_ops(viewer) else {}, asof=asof)

def now_ts(): return dt.datetime.now().isoformat(timespec="seconds")

def post(m):
    allm = rows("m"); m["seq"] = len(allm) + 1; bot = m["author"] == "NCI"
    m["id"] = ("b%02d" % (sum(x["author"] == "NCI" for x in allm) + 1)) if bot else ("m%02d" % (sum(x["author"] != "NCI" for x in allm) + 1))
    m.setdefault("channel", "Transport"); m["role"] = m.get("role") or ROLES.get(m["author"], "Teams user"); m["ts"] = m.get("ts") or now_ts()
    m["seen"] = dict(m.get("seen") or {}, **{m["author"]: m["ts"]}); m = {k: v for k, v in m.items() if v is not None}; put("m", m["id"], m)
    if bot or not allowed(m["channel"]): return [m]
    r = pipeline(m); r["msg"] = m["id"]; put("run", m["id"], dict(r, author=m["author"])); schedule(m["channel"])
    m["ents"] = r["ents"]; m["nci"] = list(dict.fromkeys([i["id"] for i in r["items"]] + r["linked"])); put("m", m["id"], m)  # every message is tracked
    res = [m]; mention = re.search(r"@nci\b", m["text"], re.I); card = None
    if mention: card = dict(type="answer", **ask(re.sub(r"@nci", "", m["text"], flags=re.I).strip(), viewer=m["author"]))
    elif (r["items"] or r["ambiguous"] or r["notes"]) and not m["channel"].startswith("chat:"): card = dict(type="capture", items=r["items"], stages=r["stages"], ms=r["ms"], ambiguous=r["ambiguous"], notes=r["notes"])
    if card:
        b = dict(author="NCI", role="bot", text="", card=card, channel=m["channel"], ts=m["ts"], parent=m.get("parent") or m["id"], seen={"NCI": m["ts"]})
        b["seq"] = m["seq"] + 1; b["reply_to"] = m["id"]; b["id"] = "b%02d" % (sum(x["author"] == "NCI" for x in rows("m")) + 1); put("m", b["id"], b); res.append(b)
    if LLM.ready() and not r["items"] and not mention: spawn_track(m["id"])
    return res

def reset():
    with LK: db.execute("delete from d"); db.commit()
    ORC.sig = None; ORC.vecs = {}; TYP.clear()

def seed():
    reset()
    for x in json.load(open(os.path.join(R, "data", "teams_chat.json"), encoding="utf-8"))["messages"]:
        post(dict(author=x["from"], role=x["role"], text=x["text"], ts=x["time"][:19], channel=x.get("channel", "Transport"), parent=x.get("parent"), subject=x.get("subject")))
    for c in json.load(open(os.path.join(R, "data", "chats.json"), encoding="utf-8")):
        put("chat", c["id"], dict(id=c["id"], title=c.get("title"), members=c["members"], kind="group" if len(c["members"]) > 2 else "dm", created=c["messages"][0]["time"][:19]))
        for x in c["messages"]: post(dict(author=x["from"], text=x["text"], ts=x["time"][:19], channel="chat:" + c["id"]))
    for m in rows("m"):  # replayed history counts as already read by everyone who could see it
        mem = members(m.get("channel", "Transport")) or set(ROLES); m["seen"] = dict({n: m["ts"] for n in mem}, **m.get("seen", {})); put("m", m["id"], m)

def health():
    return dict(version=API_VERSION, openai=LLM.ready(), openai_model=LLM.model() if LLM.ready() else None, gaik_installed=GB.installed(), gaik_llm=GB.available(), provider=os.environ.get("NCI_LLM_PROVIDER") or "default(azure)", channels=sorted(ALLOW), db=DBP,
                teams_webhook=bool(os.environ.get("TEAMS_WEBHOOK_SECRET")), graph_notify=bool(os.environ.get("GRAPH_CLIENT_STATE") and os.environ.get("GRAPH_TOKEN")))

def pd(t, day):
    d = re.search(r"\b(\d{1,2}) (%s)" % "|".join(MONTHS), (t or "").lower())
    return "%s-%02d-%02d" % (day[:4], MONTHS.index(d.group(2)) + 1, int(d.group(1))) if d else None

def apply_units(units, msgs, ch):
    """Bind GAIK fact units to entities and time. Thread state is replayed per message so 'that node' resolves as when it was said."""
    snap, c = {}, {}
    for m in msgs:
        t = re.sub(r"\s+", " ", m["text"]); ids = [e for e in re.findall(IDRE, t) if e in ENT and e.startswith("TRN")]; f = ["FW-" + x for x in re.findall(r"\b(?:FW-)?(\d\.\d\.\d)\b", t) if "FW-" + x in ENT]
        if ids: c["trn"] = ids[0]
        if f: c["fw"] = f[-1]
        snap[m["id"]] = dict(c)
    M = {m["id"]: m for m in msgs}; A = {a["key"]: a for a in rows("a")}; out = []
    for u in units:
        m = M[u["id"]]; own = u["summary"] + " " + u["quote"]; full = own + " " + m["text"]; cx = snap[m["id"]]; k = u["kind"]
        ids = [e for e in dict.fromkeys(re.findall(IDRE, full)) if e in ENT] + [e for e, (_, al) in ENT.items() if any(re.search(r"\b" + a + r"\b", full.lower()) for a in al)]
        et = next((e for e in ids if e.startswith("TRN")), cx.get("trn")); cab = [e for e in ids if e.startswith("CAB")]; gate = [e for e in ids if e.startswith("GATE")]
        efw = ["FW-" + x for x in re.findall(r"\b(?:FW-)?(\d\.\d\.\d)\b", own) if "FW-" + x in ENT]; fw = efw[0] if efw else cx.get("fw")
        day = m["ts"][:10]; vf = pd(u["time"], day) or pd(m["text"], day)
        if k in ("workaround", "validation_gap") and et and fw: ent, rel, nodes, scope = et, fw, [et, fw], ("%s on %s only (exact node + firmware)" % (et, fw) if k == "workaround" else "%s + %s" % (et, fw))
        elif k == "hypothesis" and et: ent, rel, nodes, scope = et, (cab[0] if cab else "unspecified"), [et] + cab[:1], "%s / %s" % (et, cab[0] if cab else "unspecified")
        elif k == "access" and gate: ent, rel, nodes, scope = gate[0], "constraint", [gate[0], "HEL-DC-07"], gate[0] + " at HEL-DC-07"
        elif k == "practice" and (ids or et):
            nodes = list(dict.fromkeys([e for e in ids if ENT[e][0] != "Site"] or [et or ids[0]])); ent, rel, scope = nodes[0], m["id"], " + ".join(nodes)
        else: continue
        key = "%s|%s|%s" % (k, ent, rel); ex = A.get(key)
        if ex and m["id"] in ex["ev"]: continue
        if not ex and k == "workaround" and (not efw or re.search(r"\b(not|n[’']t|never|untested|without)\b", own, re.I)): continue  # never mint a workaround from vague or negative text
        act, a = upsert(dict(m, src="gaik"), k, ent, rel, nodes, u["summary"], scope, u["quote"], vf, bool(vf)); A[key] = a
        if k == "hypothesis": a["views"] = [v for v in a["views"] if v["msg"] != m["id"]] + [{"author": m["author"], "view": u["summary"][:80], "msg": m["id"]}]; put("a", a["id"], a)
        out.append(dict(id=a["id"], action=act, kind=k, statement=a["statement"], state=ST[a["st"]]))
    return out

def curate_now(ch="Transport", curator=None):
    """Incremental: only messages newer than the watermark (plus 6 for context) go to the LLM. Skips on unchanged hash, spent budget or an open breaker."""
    with LK:
        allm = [m for m in sorted(rows("m"), key=lambda x: x["seq"]) if m["author"] != "NCI" and m.get("channel", "Transport") == ch][-200:]
        wm = one("wm", ch) or {"seq": 0}; new = [m for m in allm if m["seq"] > wm["seq"]]
        if not new: return {"skipped": "nothing new since last curation"}
        window = [m for m in allm if m["seq"] <= wm["seq"]][-6:] + new
    if not (curator or GB.available()): return {"skipped": True}
    ok, why = ORC.allow()
    if not ok:
        with LK: put("cur", "last", dict(one("cur", "last") or {}, error="paused: " + why))
        return {"skipped": why}
    t0 = time.perf_counter()
    try: units, conf, missing, usage = GB.curate(window, curator)  # slow LLM call runs outside the lock
    except Exception as x:
        ORC.failure(x)
        with LK: put("cur", "last", dict(error=str(x)[:300]))
        return {"error": str(x)}
    ORC.success(); ORC.record("curator", usage, time.perf_counter() - t0)
    with LK:
        out = apply_units(units, allm, ch); put("wm", ch, {"seq": max(m["seq"] for m in new)})
        put("cur", "last", dict(conflicts=conf, missing=missing, usage=usage, ms=round((time.perf_counter() - t0) * 1000), units=len(units), window=len(window), applied=out, at=dt.datetime.now().isoformat(timespec="seconds")))
        if out: post(dict(author="NCI", role="bot", text="", channel=ch, card=dict(type="capture", items=out, stages={"gaik_curate": round(time.perf_counter() - t0, 2)}, ms=round((time.perf_counter() - t0) * 1000), ambiguous=[], notes=["GAIK curator pass over %d new message(s): quotes verified verbatim, bound to entities by NCI" % len(new)])))
    return {"units": len(units), "applied": out}

_T = {}
def schedule(ch):  # debounce: curate once the thread goes quiet
    if not GB.available(): return
    if _T.get(ch): _T[ch].cancel()
    _T[ch] = threading.Timer(float(os.environ.get("NCI_CURATE_DELAY", "3")), curate_now, [ch]); _T[ch].daemon = True; _T[ch].start()

def teams_outgoing(raw, auth):
    """Teams Outgoing Webhook (fires on an @mention of NCI). The HMAC signature is mandatory."""
    sec = os.environ.get("TEAMS_WEBHOOK_SECRET")
    if not sec: return 503, {"error": "TEAMS_WEBHOOK_SECRET not set"}
    sig = base64.b64encode(hmac.new(base64.b64decode(sec), raw, hashlib.sha256).digest()).decode()
    if not hmac.compare_digest("HMAC " + sig, auth or ""): return 401, {"error": "bad signature"}
    a = json.loads(raw); text = re.sub(r"<[^>]+>", " ", a.get("text", "")).strip()
    res = post(dict(author=(a.get("from") or {}).get("name", "Teams user"), text="@NCI " + text, channel=os.environ.get("NCI_DEFAULT_CHANNEL", "Transport")))
    c = res[-1].get("card") if len(res) > 1 else None
    return 200, {"type": "message", "text": ("**%s**\n\n%s" % (c["headline"], "\n\n".join(c["paras"]))) if c and c["type"] == "answer" else "Noted."}

def _graph_fetch(res):
    try:
        g = json.load(urllib.request.urlopen(urllib.request.Request("https://graph.microsoft.com/v1.0/" + res.lstrip("/"), headers={"Authorization": "Bearer " + os.environ["GRAPH_TOKEN"]}), timeout=20))
        with LK: post(dict(author=((g.get("from") or {}).get("user") or {}).get("displayName", "Teams user"), text=re.sub(r"<[^>]+>", " ", g["body"]["content"]).strip(),
                           channel=os.environ.get("NCI_DEFAULT_CHANNEL", "Transport"), ts=g["createdDateTime"][:19]))
    except Exception as x:
        with LK: put("cur", "last", dict(one("cur", "last") or {}, error="graph fetch: %s" % x))

def graph_notify(raw):
    """Microsoft Graph change notifications: passive channel capture. Needs tenant admin consent; clientState is verified."""
    st = os.environ.get("GRAPH_CLIENT_STATE")
    if not st or not os.environ.get("GRAPH_TOKEN"): return 503, {"error": "GRAPH_CLIENT_STATE / GRAPH_TOKEN not set"}
    ok = [v for v in json.loads(raw).get("value", []) if hmac.compare_digest(str(v.get("clientState", "")), st)]
    for v in ok: threading.Thread(target=_graph_fetch, args=(v["resource"],), daemon=True).start()
    return 202, {"accepted": len(ok)}

def assertion_action(aid, b):
    """confirm (trust +1), dispute, expire, reconfirm (someone checked it is still true: resets the review clock)."""
    a = one("a", aid); act = b.get("act")
    if not a: return {"error": "unknown"}, 404
    vw = b.get("viewer")
    if vw and vis(a, vw) is None: return {"error": "not visible"}, 403
    if act == "confirm": a["boost"] = a.get("boost", 0) + 1
    if act == "dispute": a["dis"] = not a["dis"]
    if act == "expire": a["vt"] = None if a["vt"] else today()
    if act == "reconfirm": a["lc"] = b.get("asof") or today(); a.setdefault("recon", []).append(dict(by=ROLES.get(vw, "a team member"), on=a["lc"]))
    a["st"] = score(a); put("a", a["id"], a); return view(a, asof=b.get("asof")), 200

def orchestrator_report(asof=None):
    asof = asof or today(); orc_sync()
    if not ORC.cal.get("lexical"): ORC.calibrate(semantic=False)
    A = rows("a")
    return dict(components=ORC.components(GB.available()), keys=OM.key_report(OM.chat_provider(), OM.embed_provider()), usage=ORC.usage_summary(), calibration=ORC.cal,
                floors=dict(lexical=ORC.floor_lex, semantic=ORC.floor_sem), index=dict(docs=len(ORC.docs), semantic=bool(ORC.store), finnish=OM._FI.get("name", "not loaded")),
                policy=dict(visibility=os.environ.get("NCI_ORG_VISIBILITY") or "assertions", ttl_days={k: ttl(k) for k in TTL}, channels=sorted(ALLOW), members={c: len(v) for c, v in MEMBERS.items()}),
                stale=[dict(id=a["id"], kind=a["kind"], age=freshness(a, asof)[1], freshness=freshness(a, asof)[0], owners=[x["role"] for x in a["authors"]]) for a in A if freshness(a, asof)[0] != "fresh"], asof=asof)

def export():
    return dict(exported=dt.datetime.now().isoformat(timespec="seconds"), assertions=rows("a"), pending_entity_links=[x for x in rows("amb") if x["status"] == "pending"], usage=rows("use"))

# ================================================================ workspace: chats, read receipts, reactions, typing, activity
TYP = {}  # conv -> {person: until-epoch}; in memory only
def unseen(m, viewer): return "seen" in m and viewer not in m["seen"]  # messages stored before read receipts existed count as read
def conv_label(c, viewer=None):
    if not c or not c.startswith("chat:"): return "#" + (c or "Transport")
    x = chat_of(c)
    if not x: return "a chat"
    if x["kind"] == "nci": return "NCI chat"
    if x.get("title"): return x["title"]
    return "chat with " + ", ".join(n for n in x["members"] if n != viewer)

def ensure_nci_chat(viewer):
    cid = "nci-" + viewer; x = one("chat", cid)
    if not x: x = dict(id=cid, title="NCI", members=[viewer, "NCI"], kind="nci", created=now_ts()); put("chat", cid, x)
    return x

def new_chat(viewer, people, title=None):
    mem = sorted(set([p for p in people if p in ROLES] + [viewer]))
    if len(mem) < 2: return None
    cid = "dm-" + "-".join(mem) if len(mem) == 2 else "g-" + hashlib.md5("|".join(mem).encode()).hexdigest()[:8]
    x = one("chat", cid)
    if not x: x = dict(id=cid, title=(title or "").strip()[:80] or None, members=mem, kind="dm" if len(mem) == 2 else "group", created=now_ts()); put("chat", cid, x)
    return x

def list_chats(viewer):
    if viewer not in ROLES: return []
    ensure_nci_chat(viewer); M = sorted(rows("m"), key=lambda m: m["seq"]); out = []
    for x in rows("chat"):
        if viewer not in x["members"]: continue
        c = "chat:" + x["id"]; L = [m for m in M if m.get("channel") == c]; last = L[-1] if L else None
        out.append(dict(x, conv=c, label=conv_label(c, viewer), others=[n for n in x["members"] if n != viewer],
                        last=dict(author=last["author"], text=(last["text"] or (last.get("card") or {}).get("headline") or "")[:90], ts=last["ts"]) if last else None,
                        unread=sum(1 for m in L if m["author"] != viewer and unseen(m, viewer))))
    pinned = [x for x in out if x["kind"] == "nci"]
    return pinned + sorted([x for x in out if x["kind"] != "nci"], key=lambda x: (x["last"] or {}).get("ts") or x["created"], reverse=True)

def mark_read(conv, viewer):
    n = 0; t = now_ts()
    for m in rows("m"):
        if m.get("channel") == conv and m["author"] != viewer and viewer not in m.get("seen", {}): m.setdefault("seen", {})[viewer] = t; put("m", m["id"], m); n += 1
    return n

def react(mid, emoji, viewer):
    m = one("m", mid)
    if not m or not can_see_channel(viewer, m.get("channel", "Transport")): return None
    d = m.setdefault("react", {}).setdefault(emoji, {})
    if viewer in d: d.pop(viewer)
    else: d[viewer] = now_ts()
    m["react"] = {k: v for k, v in m["react"].items() if v}; put("m", mid, m); return m

def typing(conv, viewer, secs=5): TYP.setdefault(conv, {})[viewer] = time.time() + secs

def activity(viewer):
    M = sorted(rows("m"), key=lambda m: m["seq"]); mine = {m["id"] for m in M if m["author"] == viewer}; out = []
    for m in M:
        ch = m.get("channel", "Transport")
        if m["author"] in (viewer, "NCI") or not can_see_channel(viewer, ch): continue
        u = unseen(m, viewer); base = dict(msg=m["id"], conv=ch, where=conv_label(ch, viewer), by=m["author"], ts=m["ts"], text=m["text"][:140], unread=u)
        if re.search(r"@%s\b" % re.escape(viewer), m["text"], re.I): out.append(dict(base, type="mention"))
        elif m.get("parent") in mine: out.append(dict(base, type="reply"))
    for m in M:
        if m["author"] != viewer: continue
        ch = m.get("channel", "Transport")
        for e, who in (m.get("react") or {}).items():
            for n, t in who.items():
                if n != viewer: out.append(dict(type="reaction", msg=m["id"], conv=ch, where=conv_label(ch, viewer), by=n, emoji=e, ts=t, text=m["text"][:140], unread=False))
        for i in m.get("nci") or []:
            a = one("a", i)
            if a: out.append(dict(type="tracked", msg=m["id"], conv=ch, where=conv_label(ch, viewer), by="NCI", id=i, ts=m["ts"], text=a["statement"][:140], unread=False))
    out.sort(key=lambda x: x["ts"], reverse=True)
    return dict(items=out[:60], unread=sum(x["unread"] for x in out))

def state(viewer):
    now = time.time(); typ = {}
    for c, d in TYP.items():
        n = [p for p, t in d.items() if p != viewer and t > now]
        if n and can_see_channel(viewer, c): typ[c] = n
    un = {}
    for m in rows("m"):
        c = m.get("channel", "Transport")
        if m["author"] != viewer and unseen(m, viewer) and can_see_channel(viewer, c): un[c] = un.get(c, 0) + 1
    return dict(typing=typ, unread=un, activity=activity(viewer)["unread"], llm=dict(ready=LLM.ready(), model=LLM.model() if LLM.ready() else None))

# ================================================================ tracking new messages with the LLM (quotes verified verbatim)
LLM_KINDS = ("observation", "practice", "issue", "decision")
_QN = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-"})
def qnorm(t): return re.sub(r"\s+", " ", (t or "").translate(_QN)).strip().lower()
IDS = r"\b(?:TRN-\d{3}|CAB-\d+|GATE-[A-Z]+-\d+|HEL-DC-\d+|FW-\d\.\d\.\d|\d\.\d\.\d)\b"

def spawn_track(mid):
    if (os.environ.get("NCI_TRACK_SYNC") or "") == "1": return track_llm(mid)
    threading.Thread(target=track_llm, args=(mid,), daemon=True).start()

def track_llm(mid):
    """One new message -> 0..3 knowledge items. The quote must be copied verbatim from the message; ids must exist in the message. Unreviewed, Observed only."""
    with LK: m = one("m", mid)
    if not m or not LLM.ready(): return []
    ok, why = ORC.allow()
    if not ok: return []
    sysm = ("You extract operational knowledge from ONE Microsoft Teams message for a network-operations knowledge base. Reply with JSON only: "
            '{"items":[{"kind":"observation|practice|issue|decision","statement":"one neutral sentence","quote":"exact text copied from the message","entities":["ID"]}]}. '
            "observation = a fact about the network, a site or a procedure; practice = know-how or a rule of thumb; issue = a problem or risk someone reports; decision = something agreed or planned. "
            "Known entity ids: " + ", ".join(ENT) + ". Use only ids that the message itself names. Never add devices, numbers, dates or names that are not in the message. "
            'Return {"items":[]} for greetings, thanks, questions without an answer and small talk.')
    t0 = time.perf_counter()
    try:
        out, usage = LLM.chat([{"role": "system", "content": sysm}, {"role": "user", "content": "Author role: %s\nConversation: %s\nMessage: %s" % (m["role"], conv_label(m.get("channel")), m["text"])}], max_tokens=500, json_mode=True)
        ORC.success()
    except Exception as x: ORC.failure(x); return []
    ORC.record("tracking", usage, time.perf_counter() - t0, provider="openai", model=LLM.model())
    try: items = (json.loads(out) or {}).get("items") or []
    except Exception: items = []
    added = []
    with LK:
        m = one("m", mid); txt = m["text"]; low = txt.lower(); have = set(re.findall(IDS, txt))
        for i, it in enumerate(items[:3]):
            if not isinstance(it, dict): continue
            k = it.get("kind"); qt = str(it.get("quote") or "").strip(); st = re.sub(r"\s+", " ", str(it.get("statement") or "")).strip()[:300]
            if k not in LLM_KINDS or not qt or qnorm(qt) not in qnorm(txt): continue          # no verbatim quote, no assertion
            if not st or set(re.findall(IDS, st)) - have: st = qt                            # statement invented an id: fall back to the quote
            ents = [e for e in dict.fromkeys(it.get("entities") or []) if e in ENT and (e.lower() in low or any(re.search(r"\b" + a + r"\b", low) for a in ENT[e][1]))]
            act, a = upsert(dict(m, src="openai"), k, ents[0] if ents else "unscoped", "%s:%d" % (mid, i), ents, st, " + ".join(ents) or "not tied to a device", qt)
            added.append(a["id"])
        if added:
            m["nci"] = list(dict.fromkeys((m.get("nci") or []) + added)); m["ents"] = sorted(set((m.get("ents") or []) + [n for i in added for n in one("a", i)["nodes"]])); put("m", mid, m)
    return added

# ================================================================ timeline of a topic, organisation-level risk register
def resolve_topic(t):
    t = (t or "").strip()
    if t.upper() in ENT: return t.upper()
    ids = [x for x in re.findall(IDRE, t.upper()) if x in ENT] + [e for e, (_, al) in ENT.items() if any(re.search(r"\b" + a + r"\b", t.lower()) for a in al)]
    if ids: return ids[0]
    if not t: return None
    orc_sync(); r = ORC.search(t, k=3)
    if r["ok"] and r["ids"]:
        a = one("a", r["ids"][0][0])
        if a and a["entity"] in ENT: return a["entity"]
    return None

def timeline(topic, viewer=None, asof=None):
    asof = asof or today()
    if not topic: return dict(topic=None, entities=[], events=[], asof=asof)
    tree = set(subtree(topic)); ev = []; A = rows("a")
    for e, d, fw in CHG:
        if e in tree: ev.append(dict(at=d, type="change", title="%s now runs %s" % (e, fw), text="Recorded in the inventory change log.", source="Inventory / change log"))
    rel = [a for a in A if a["entity"] in tree or set(a["nodes"]) & tree]; evid = {i for a in rel for i in a["ev"]}
    for m in sorted(rows("m"), key=lambda m: m["seq"]):
        c = m.get("channel", "Transport")
        if m["author"] == "NCI" or is_nci_chat(c) or not (set(m.get("ents") or []) & tree or m["id"] in evid): continue
        if can_see_channel(viewer, c): ev.append(dict(at=m["ts"], type="message", msg=m["id"], conv=c, title="%s in %s" % (m["author"], conv_label(c, viewer)), text=m["text"], role=m["role"]))
        elif not c.startswith("chat:"): ev.append(dict(at=m["ts"], type="message", restricted=True, title="%s in %s (you are not a member)" % (m["role"], conv_label(c)), text=None))  # other people's private chats stay invisible
    MT = {m["id"]: m["ts"] for m in rows("m")}
    for a0 in rel:
        a = vis(a0, viewer)
        if a is None: continue
        ev.append(dict(at=MT.get(a0["ev"][0], a["learned"]) if a0["ev"] else a["learned"], type="captured", id=a["id"], title="NCI captured %s · %s" % (a["id"], a["kind"].replace("_", " ")), text=a["statement"], state=ST[a["st"]]))
        if a["vf"] and a["vf"] != a["learned"]: ev.append(dict(at=a["vf"], type="effective", id=a["id"], title="%s %s" % (a["id"], "takes effect" if a["kind"] == "access" else "applies from"), text=a["scope"]))
        for x in a.get("recon") or []: ev.append(dict(at=x["on"], type="reconfirmed", id=a["id"], title="%s reconfirmed by %s" % (a["id"], x["by"]), text=None))
        if a["vt"]: ev.append(dict(at=a["vt"], type="expired", id=a["id"], title="%s marked expired" % a["id"], text=None))
        due = (dt.date.fromisoformat(last_conf(a)) + dt.timedelta(days=ttl(a["kind"]))).isoformat()
        ev.append(dict(at=due, type="review", id=a["id"], title="%s review due (%d-day period)" % (a["id"], ttl(a["kind"])), text=None))
    ev.sort(key=lambda e: e["at"])
    for e in ev: e["future"] = e["at"][:10] > asof
    return dict(topic=topic, kind=ENT.get(topic, ("",))[0], entities=sorted(tree), events=ev, asof=asof)

SEV = {"high": 0, "medium": 1, "low": 2}
def risks(viewer=None, asof=None):
    asof = asof or today(); A = [b for b in (vis(a, viewer) for a in rows("a")) if b is not None]; out = []
    def R(sev, kind, title, detail, ids=(), ents=()): out.append(dict(sev=sev, kind=kind, title=title, detail=detail, ids=list(ids), entities=[e for e in ents if e in ENT]))
    for a in A:
        E = list(dict.fromkeys([a["entity"]] + a["nodes"])); who = ", ".join(x["name"] for x in a["authors"])
        if a["dis"]: R("high", "disputed", "%s is disputed" % a["id"], "Someone disputed this; agents will not use it until it is resolved.", [a["id"]], E)
        if a["kind"] == "validation_gap": R("high", "validation", "Validation gap on %s" % a["scope"], a["statement"], [a["id"]], E)
        if a["kind"] == "hypothesis" and not a["dis"]: R("medium", "hypothesis", "Unverified cause: %s" % a["scope"], "%s Raised by %s." % (a["statement"], who), [a["id"]], E)
        if a["kind"] == "issue": R("medium", "issue", "Open issue: %s" % a["scope"], "%s Reported by %s." % (a["statement"], who), [a["id"]], E)
    st = [a for a in A if freshness(a, asof)[0] == "stale" and not a["dis"]]
    if st: R("medium", "stale", "%d item(s) past their review period" % len(st), "; ".join("%s last confirmed %s by %s" % (a["id"], last_conf(a), ", ".join(x["name"] for x in a["authors"])) for a in st) + ". Treat them as leads to verify, then press Reconfirm.",
             [a["id"] for a in st], sorted({e for a in st for e in [a["entity"]] + a["nodes"]}))
    hold = {}
    for a in A:
        if len(a["authors"]) == 1 and a["st"] == 0: hold.setdefault(a["authors"][0]["name"], []).append(a)
    for n, L in hold.items():
        if len(L) >= 3: R("high", "concentration", "Knowledge concentration: %s" % n, "%d items rest on %s alone and nobody else has confirmed them. If they leave or move, nobody can vouch for them. Ask a colleague to review and confirm." % (len(L), n),
                          [a["id"] for a in L], sorted({e for a in L for e in [a["entity"]] + a["nodes"]}))
    for e, (k, _) in ENT.items():
        if k in ("Transport node", "Cabinet", "Access barrier") and not any(e == a["entity"] or e in a["nodes"] for a in A):
            R("low", "coverage", "No captured knowledge about %s" % e, "Nobody has written anything about %s in the captured conversations." % e, (), [e])
    if viewer not in (None, "anonymous") and is_ops(viewer):
        seen = set()
        for x in sorted(rows("qlog"), key=lambda x: x["at"], reverse=True):
            if not x["answered"] and x["q"].lower() not in seen and len(seen) < 6:
                seen.add(x["q"].lower()); R("low", "gap", "Asked but not covered", "“%s” (asked %s by %s). Nobody has captured an answer yet." % (x["q"][:120], x["at"][:10], ROLES.get(x["viewer"], "a colleague")))
    out.sort(key=lambda r: SEV[r["sev"]]); return out

def tracked(conv, viewer, asof=None):
    if not can_see_channel(viewer, conv): return None
    M = [m for m in rows("m") if m.get("channel") == conv and m["author"] != "NCI"]; mids = {m["id"] for m in M}
    A = [view(b, asof=asof) for b in (vis(a, viewer) for a in rows("a")) if b is not None and set(b.get("ev") or []) & mids]
    ents = {}
    for m in M:
        for e in m.get("ents") or []: ents[e] = ents.get(e, 0) + 1
    ids = {a["id"] for a in A}
    return dict(conv=conv, label=conv_label(conv, viewer), messages=len(M), tracked=sum(1 for m in M if m.get("ents") or m.get("nci")), assertions=A,
                entities=sorted(ents.items(), key=lambda x: -x[1]), risks=[r for r in risks(viewer, asof) if set(r["ids"]) & ids or set(r["entities"]) & set(ents)][:8])

# ================================================================ NCI chat: retrieve (ACL'd) -> answer with OpenAI -> verify -> sources, related, risks, timeline
SELF = re.compile(r"\b(what did i|did i|have i|i said|i wrote|i asked|i told|i mentioned|i posted|i sent|my (?:message|messages|chat|chats|note|notes|text|texts|post|posts))\b", re.I)
QFILL = {"message", "messag", "chat", "said", "say", "wrote", "write", "text", "tell", "told", "mention", "mentioned", "post", "know", "anyone", "someone", "talk", "about"}
TALK = re.compile(r"\b(say|said|says|write|wrote|tell|told|mention|mentioned|message|messages|chat|chats|talk|talking|discuss|discussing|anyone|anybody|someone)\b", re.I)
def find_messages(q, viewer, focus=frozenset(), k=6):
    """Messages from staff chats and channels the viewer is in (never NCI chats). Ranked by matching words first,
    then the people named and shared devices; newest first among equals. 'What did I write ...' searches only the viewer's own messages.
    -> (messages, mine, talk) where talk = the question is about what people wrote."""
    low = q.lower(); mine = bool(SELF.search(q)); qt = set(OM.norm(q)) - QFILL
    people = [p for p in ROLES if p != viewer and re.search(r"\b%s\b" % p.lower(), low)]
    talk = mine or bool(TALK.search(q))
    cand = []
    for m in rows("m"):
        c = m.get("channel", "Transport")
        if m["author"] == "NCI" or is_nci_chat(c) or not can_see_channel(viewer, c) or (mine and m["author"] != viewer): continue
        if people and talk and m["author"] not in people: continue        # "what did Erkki say" = Erkki's messages
        words = len(qt & set(OM.norm(m["text"])))
        sc = 3 * words + (1 if set(m.get("ents") or []) & set(focus) else 0) + (2 if m["author"] in people else 0)
        if words or (not qt and (mine or m["author"] in people)): cand.append((sc, m))
    cand.sort(key=lambda x: x[1]["ts"], reverse=True)                     # newest first among equals
    return [m for sc, m in sorted(cand, key=lambda x: -x[0])][:k], mine, talk

def assist_context(q, viewer, asof=None):
    asof = asof or today(); r = ask(q, viewer=viewer, asof=asof); s = search(q, viewer=viewer, asof=asof)
    A = {b["id"]: b for b in (vis(a, viewer) for a in rows("a")) if b is not None}; M = {m["id"]: m for m in rows("m")}
    src = []
    for x in r["sources"]:
        a = A.get(x["id"])
        if not a: continue
        qs = [dict(u, conv=M[u["msg"]].get("channel", "Transport"), where=conv_label(M[u["msg"]].get("channel"), viewer), ts=M[u["msg"]]["ts"]) for u in x["quotes"] if u["msg"] in M]
        src.append(dict(x, kind=a["kind"], statement=a["statement"], scope=a["scope"], quotes=qs, where=[conv_label(c, viewer) for c in a.get("ch") or []],
                        applicable=ev(a, a["entity"], fw_at(a["entity"], asof), asof, "9999") is None))
    ids = [x["id"] for x in src]
    explicit = [x for x in re.findall(IDRE, q.upper()) if x in ENT] + [e for e, (_, al) in ENT.items() if any(re.search(r"\b" + a + r"\b", q.lower()) for a in al)]
    topic = explicit[0] if explicit else ("HEL-DC-07" if r["params"]["intent"] == "b" else (A[ids[0]]["entity"] if ids and A[ids[0]]["entity"] in ENT else None))
    ok = s["retrieval"]["passed"]
    if not topic and ok and s["assertions"] and s["assertions"][0]["entity"] in ENT: topic = s["assertions"][0]["entity"]
    rel = [dict(id=a["id"], kind=a["kind"], statement=a["statement"], applicable=a["applicable"], why=a["why"], state=a["state"]) for a in s["assertions"] if a["id"] not in ids][:5] if ok else []
    focus = set(subtree(topic)) if topic else set()
    for i in ids: focus |= set(A[i]["nodes"]) | {A[i]["entity"]}
    found, mine, talk = find_messages(q, viewer, focus)
    who = next((p for p in ROLES if p != viewer and re.search(r"\b%s\b" % p.lower(), q.lower())), None)
    def where(m):  # "Erkki in your 1:1 chat" rather than "Erkki in chat with Erkki"
        x = chat_of(m.get("channel"))
        if x and x["kind"] == "dm" and m["author"] != viewer: return "your 1:1 chat"
        return conv_label(m.get("channel"), viewer)
    msgs = [dict(id=m["id"], author=m["author"], role=m["role"], ts=m["ts"], conv=m.get("channel", "Transport"), where=where(m), text=m["text"], you=m["author"] == viewer, tracked=m.get("nci") or []) for m in found]
    head, paras = r["headline"], r["paras"]
    cite = lambda m: "[%s] %s in %s, %s: “%s”" % (m["id"], "You" if m["you"] else m["author"], m["where"], m["ts"][:16].replace("T", " "), m["text"][:220])
    if msgs and (talk or not (src or rel)):                            # the answer is in what people wrote, not (yet) in verified knowledge
        head = ("Here is what you wrote" if mine else ("What %s wrote" % who) if who and talk else "Found in your conversations") + " (%d message%s)" % (len(msgs), "" if len(msgs) == 1 else "s")
        paras = [cite(m) for m in msgs] + ["These are messages, not verified knowledge: treat them as what people said. %s" % ("NCI also has %d captured knowledge item(s) on this, listed under Sources." % len(src) if src else "Open a message to see what NCI tracked from it.")]
    elif mine and not msgs: head, paras = "I found no messages from you about that.", ["Only your own messages in chats and channels are searched for questions like this."] + (paras if src else [])
    elif msgs: paras = paras + ["Also mentioned in conversations: " + "; ".join("[%s] %s" % (m["id"], "you" if m["you"] else m["author"]) for m in msgs) + "."]
    rk = [x for x in risks(viewer, asof) if x["kind"] != "gap" and (set(x["entities"]) & focus or set(x["ids"]) & set(ids))][:6]
    tl = timeline(topic, viewer, asof) if topic else None
    return dict(headline=head, paras=paras, sources=src, related=rel, messages=msgs, risks=rk, timeline=tl, topic=topic, trace=r["trace"], params=r["params"], mine=mine)

NCI_SYS = ("You are NCI (Network Context Intelligence), an assistant inside Microsoft Teams for a network operations organisation. "
           "Answer ONLY from the CONTEXT, which NCI retrieved from the team's chats and its knowledge store with access control already applied. "
           "Cite every factual sentence with the id in square brackets, e.g. [C-001] or [m03]. Prefer currently applicable facts; mark historical or out-of-scope ones as such. "
           "Point out the relevant RISKS and say who (by the name or role given in the context) or which source to check. Keep every caveat (unverified, not validated, stale, scope). "
           "When the question is about what someone (or the person asking) wrote, answer from the CHAT MESSAGES and cite them. Address the person asking as 'you'. "
           "Never invent devices, firmware versions, numbers, dates, people or procedures. If the context does not answer the question, say so plainly and suggest where to look. "
           "Be concise: at most 170 words, short paragraphs or bullets. Advisory only: never claim you took an action.")

def _ctx_text(c):
    L = ["FACTS (cite as [C-xxx]):"]
    for x in c["sources"]: L.append("[%s] %s | kind: %s | trust: %s | %s | %s | reported by %s | freshness: %s, last confirmed %s" % (x["id"], x["statement"], x["kind"], x["state"], x["scope"], "applies now" if x["applicable"] else "NOT applicable now", ", ".join(x["authors"]), x["freshness"], x["last_confirmed"]))
    for x in c["related"]: L.append("[%s] (related) %s | %s" % (x["id"], x["statement"], "applies now" if x["applicable"] else "not applicable now: " + (x["why"] or "")))
    if c["messages"]:
        L.append("CHAT MESSAGES from Teams chats and channels (cite as [mNN]; 'YOU' = the person asking):"); L += ["[%s] %s %s (%s) in %s: %s" % (m["id"], m["ts"].replace("T", " ")[:16], "YOU" if m.get("you") else m["author"], m["role"], m["where"], m["text"]) for m in c["messages"]]
    if c["timeline"] and c["timeline"]["events"]:
        L.append("TIMELINE of %s:" % c["topic"]); L += ["%s · %s%s" % (e["at"][:10], e["title"], (" · " + e["text"][:140]) if e.get("text") else "") for e in c["timeline"]["events"][:18]]
    if c["risks"]: L.append("RISKS:"); L += ["%s · %s · %s" % (r["sev"], r["title"], r["detail"]) for r in c["risks"]]
    L.append("NCI RULE-BASED ANSWER: %s %s" % (c["headline"], " ".join(c["paras"])))
    return "\n".join(L)

def verify_answer(out, ctx, q):
    allowed_ids = set(re.findall(r"\b(C-\d{3}|m\d{2,3})\b", ctx)); cited = set(re.findall(r"\[(C-\d{3}|m\d{2,3})\]", out))
    if cited - allowed_ids: return "cited a source that was not retrieved (%s)" % ", ".join(sorted(cited - allowed_ids))
    bad = set(re.findall(IDS, out)) - set(re.findall(IDS, ctx + " " + q))
    if bad: return "named a device or version not in the sources (%s)" % ", ".join(sorted(bad))
    num = lambda t: set(re.findall(r"\b(\d+)\s*-?\s*(?:seconds?|minutes?|hours?|days?|%)", t, re.I))
    if num(out) - num(ctx + " " + q): return "used a number not in the sources"
    for n in ROLES:
        if re.search(r"\b%s\b" % n, out) and not re.search(r"\b%s\b" % n, ctx + " " + q): return "named a person not in the sources"
    return None

def llm_answer(q, c, hist):
    ok, why = ORC.allow()
    if not ok: return dict(used=False, note="LLM paused: " + why)
    ctx = _ctx_text(c); msgs = [{"role": "system", "content": NCI_SYS}]
    for m in hist[-4:]:
        if m["author"] == "NCI" and (m.get("card") or {}).get("answer"): msgs.append({"role": "assistant", "content": m["card"]["answer"]})
        elif m["author"] != "NCI": msgs.append({"role": "user", "content": m["text"]})
    msgs.append({"role": "user", "content": "CONTEXT\n" + ctx + "\n\nQUESTION: " + q})
    t0 = time.perf_counter()
    try: out, usage = LLM.chat(msgs, max_tokens=600); ORC.success()
    except Exception as x: ORC.failure(x); return dict(used=False, note=str(x)[:200])
    ms = round((time.perf_counter() - t0) * 1000); ORC.record("assistant", usage, ms / 1000, provider="openai", model=LLM.model())
    bad = verify_answer(out, ctx, q)
    if bad: return dict(used=False, note="LLM answer discarded: " + bad, model=LLM.model(), ms=ms)
    return dict(used=True, text=out, model=LLM.model(), ms=ms, tokens=usage.get("total_tokens"))

def nci_chat(q, viewer, asof=None):
    q = (q or "").strip()[:2000]
    with LK:
        x = ensure_nci_chat(viewer); conv = "chat:" + x["id"]
        hist = [m for m in sorted(rows("m"), key=lambda m: m["seq"]) if m.get("channel") == conv]
        um = post(dict(author=viewer, role=ROLES.get(viewer, "Teams user"), text=q, channel=conv))[0]; um["seen"]["NCI"] = now_ts(); put("m", um["id"], um)
        typing(conv, "NCI", 90); c = assist_context(q, viewer, asof)
    llm = llm_answer(q, c, hist) if LLM.ready() else dict(used=False, note="Set OPENAI_API_KEY in .env for conversational answers. Showing NCI's rule-based answer.")
    with LK:
        TYP.get(conv, {}).pop("NCI", None)
        card = dict(type="nci", q=q, answer=llm.get("text") if llm["used"] else None, llm=llm, **c)
        b = post(dict(author="NCI", role="bot", text="", card=card, channel=conv))[0]
        put("qlog", um["id"], dict(id=um["id"], q=q, viewer=viewer, at=um["ts"], answered=bool(c["sources"] or c["related"] or c["messages"]), topic=c["topic"], llm=llm["used"]))
        return [one("m", um["id"]), b]

class H(BaseHTTPRequestHandler):
    def log_message(s, *a): pass
    def out(s, o, c=200, ct="application/json"):
        b = o if isinstance(o, bytes) else json.dumps(o).encode(); s.send_response(c); s.send_header("Content-Type", ct); s.send_header("Content-Length", str(len(b))); s.end_headers(); s.wfile.write(b)
    def do_GET(s):
        try: s._get()
        except Exception as x: s.out({"error": str(x)}, 400)
    def do_POST(s):
        try: s._post()
        except Exception as x: s.out({"error": str(x)}, 400)
    def _get(s):
        u = urlparse(s.path); p = u.path; q = {k: v[0] for k, v in parse_qs(u.query).items()}
        with LK:
            if p in ("/", "/index.html"): return s.out(open(os.path.join(R, "static", "index.html"), "rb").read(), ct="text/html; charset=utf-8")
            vw = q.get("viewer") or "anonymous"; ao = q.get("asof") or None
            if p == "/api/messages": return s.out([m for m in sorted(rows("m"), key=lambda x: x["seq"]) if can_see_channel(vw, m.get("channel", "Transport"))])
            if p == "/api/assertions": return s.out(dict(assertions=[view(b, q.get("T"), asof=ao) for b in (vis(a, vw) for a in rows("a")) if b is not None], ambiguous=rows("amb") if is_ops(vw) else []))
            if p == "/api/search": return s.out(search(q.get("q", ""), q.get("T"), vw, ao))
            if p == "/api/dashboard": return s.out(dashboard(vw, ao))
            if p == "/api/orchestrator": return s.out(orchestrator_report(ao))
            if p == "/api/export": return s.out(export()) if is_ops(vw) and vw != "anonymous" else s.out({"error": "export is limited to channel members"}, 403)
            if p == "/api/health": return s.out(health())
            if p == "/api/graph": return s.out(dict(entities={k: v[0] for k, v in ENT.items()}, relations=REL, firmware=CHG))
            if p == "/api/chats": return s.out(list_chats(vw))
            if p == "/api/state": return s.out(state(vw))
            if p == "/api/activity": return s.out(activity(vw))
            if p == "/api/timeline": return s.out(timeline(resolve_topic(q.get("topic", "")), vw, ao))
            if p == "/api/risks": return s.out(risks(vw, ao))
            if p == "/api/people": return s.out(dict(roles=ROLES, members=MEMBERS))
            if p == "/api/tracked":
                t = tracked(q.get("conv", ""), vw, ao)
                return s.out(t) if t is not None else s.out({"error": "not a member"}, 403)
        s.out({"error": "not found"}, 404)
    def _post(s):
        u = urlparse(s.path); p = u.path; raw = s.rfile.read(int(s.headers.get("Content-Length", 0)))
        if p == "/api/graph/notify":
            vt = parse_qs(u.query).get("validationToken")
            if vt: return s.out(vt[0].encode(), ct="text/plain")  # Graph subscription handshake
            c, o = graph_notify(raw); return s.out(o, c)
        if p == "/api/teams/outgoing":
            with LK: c, o = teams_outgoing(raw, s.headers.get("Authorization"))
            return s.out(o, c)
        if p == "/api/curate": return s.out(curate_now(json.loads(raw or b"{}").get("channel", "Transport")))  # takes the lock itself
        b = json.loads(raw or b"{}")
        if p == "/api/nci/chat" or (p == "/api/messages" and is_nci_chat(b.get("channel"))):
            vw = b.get("viewer") or b.get("author")
            if vw not in ROLES: return s.out({"error": "pick a person first"}, 403)
            return s.out(nci_chat(b.get("q") or b.get("text") or "", vw, b.get("asof")))  # takes the lock itself; the LLM call runs outside it
        if p == "/api/ask":
            with LK: r = ask(b["q"], b.get("entity"), b.get("firmware"), b.get("T"), b.get("K"), b.get("viewer") or "anonymous", b.get("asof"))
            ctxt = "\n".join(re.sub(r"\[C-\d+\]", "", x) for x in r["paras"]) if r["sources"] else ""
            r["summary"] = ORC.summarize(b["q"], ctxt) if ctxt else None  # slow LLM call: deliberately outside the lock
            return s.out(r)
        with LK:
            if p == "/api/messages":
                ch = b.get("channel", "Transport")
                if not can_see_channel(b["author"], ch): return s.out({"error": "%s is not a member of #%s" % (b["author"], ch)}, 403)
                par = b.get("parent"); pm = one("m", par) if par else None
                par = (pm.get("parent") or pm["id"]) if pm and pm.get("channel") == ch else None
                return s.out(post(dict(author=b["author"], role=b.get("role") or ROLES.get(b["author"]), text=b["text"][:2000], channel=ch, parent=par,
                                       subject=None if par else ((b.get("subject") or "").strip()[:120] or None))))
            vw = b.get("viewer")
            if p == "/api/chats":
                if vw not in ROLES: return s.out({"error": "pick a person first"}, 403)
                x = new_chat(vw, b.get("members") or [], b.get("title")); return s.out(x) if x else s.out({"error": "a chat needs at least one other person"}, 400)
            if p == "/api/read":
                if not can_see_channel(vw, b.get("conv")): return s.out({"error": "not a member"}, 403)
                return s.out({"read": mark_read(b.get("conv"), vw)})
            if p == "/api/react":
                m = react(b.get("id"), (b.get("emoji") or "")[:8], vw) if vw in ROLES else None
                return s.out(m) if m else s.out({"error": "not allowed"}, 403)
            if p == "/api/typing":
                if vw in ROLES and can_see_channel(vw, b.get("conv")): typing(b.get("conv"), vw)
                return s.out({"ok": True})
            if p == "/api/calibrate": return s.out(ORC.calibrate(semantic=True))
            if p == "/api/seed": seed(); return s.out({"ok": True})
            if p == "/api/reset": reset(); return s.out({"ok": True})
            if p.startswith("/api/assertions/"):
                r = assertion_action(p.split("/")[3], b)
                return s.out(r[0], r[1])
            if p.startswith("/api/ambiguous/"):
                i = p[len("/api/ambiguous/"):]; x = one("amb", i)
                if x: x["status"] = "confirmed"; put("amb", i, x)
                return s.out({"ok": True})
        s.out({"error": "not found"}, 404)

if __name__ == "__main__":
    if not rows("m"): seed()
    port = int(os.environ.get("PORT", 8080))
    print("NCI on http://localhost:%d  (OpenAI chat: %s, GAIK curator: %s)" % (port, LLM.model() if LLM.ready() else "off - set OPENAI_API_KEY", "on" if GB.available() else "off - rules only"))
    class Srv(ThreadingHTTPServer):
        allow_reuse_address = os.name != "nt"  # on Windows reuse lets a second server silently share the port and split requests
    try: srv = Srv(("0.0.0.0", port), H)
    except OSError:
        raise SystemExit("Port %d is already in use: another NCI server is probably still running. Stop it first (Ctrl+C in its terminal), or set PORT=8081." % port)
    srv.serve_forever()
