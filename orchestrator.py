"""NCI orchestrator: decides which GAIK component does what, how much it may spend, and what happens when it can't.
Everything degrades gracefully (no gaik / no key / over budget -> built-in rules) and the dashboard says which mode is live."""
import os, re, json, time, math, hashlib, threading, datetime as dt

# ---------------------------------------------------------------- keys (names and set/unset only; values are never exposed)
PROVIDERS = {  # mirrors gaik.software_components.llm.config; "any" = one of these is enough
    "anthropic": dict(chat=True, embed=False, need=[["ANTHROPIC_API_KEY"]], opt=["ANTHROPIC_MODEL", "ANTHROPIC_MAX_TOKENS"]),
    "openai": dict(chat=True, embed=True, need=[["OPENAI_API_KEY"]], opt=["OPENAI_MODEL", "OPENAI_BASE_URL", "EMBEDDING_MODEL"]),
    "azure": dict(chat=True, embed=True, need=[["AZURE_API_KEY"], ["AZURE_ENDPOINT"]], opt=["AZURE_DEPLOYMENT", "AZURE_API_VERSION", "EMBEDDING_MODEL"]),
    "google": dict(chat=True, embed=True, need=[["GOOGLE_API_KEY", "GEMINI_API_KEY"]], opt=["GOOGLE_MODEL", "EMBEDDING_MODEL"]),
    "vertex": dict(chat=True, embed=True, need=[["GOOGLE_PROJECT_ID", "GOOGLE_CLOUD_PROJECT"]], opt=["GOOGLE_CLOUD_LOCATION", "GOOGLE_MODEL"]),
    "aitta": dict(chat=True, embed=True, need=[["AITTA_API_KEY", "AITTA_API_TOKEN", "AITTA_TOKEN"]], opt=["AITTA_MODEL", "AITTA_EMBEDDING_MODEL"]),
    "anthropic_foundry": dict(chat=True, embed=False, need=[["ANTHROPIC_FOUNDRY_API_KEY", "AZURE_API_KEY"], ["ANTHROPIC_FOUNDRY_RESOURCE"]], opt=[]),
    "openai_compatible": dict(chat=True, embed=True, need=[["OPENAI_API_KEY"], ["OPENAI_BASE_URL"], ["OPENAI_MODEL"]], opt=["EMBEDDING_MODEL"]),
    "litellm": dict(chat=True, embed=True, need=[["LITELLM_MODEL"]], opt=["LITELLM_API_KEY", "LITELLM_BASE_URL", "LITELLM_EMBEDDING_MODEL"])}
NCI_KEYS = [("OPENAI_API_KEY", "NCI chat answers and tracking of new messages (direct OpenAI call)"), ("OPENAI_MODEL", "Model for the NCI chat (default gpt-4o-mini)"),
            ("NCI_LLM_PROVIDER", "Provider that reads threads and phrases summaries"), ("NCI_EMBED_PROVIDER", "Provider for semantic search (Anthropic has no embeddings API)"),
            ("NCI_DAILY_TOKEN_BUDGET", "Hard stop for LLM tokens per day (default 200000)"), ("NCI_CAPTURE_CHANNELS", "Channels that may be analysed"),
            ("NCI_ORG_VISIBILITY", "assertions (default) | members | off: what people outside the channel may see"),
            ("TEAMS_WEBHOOK_SECRET", "Teams outgoing webhook signature"), ("GRAPH_CLIENT_STATE", "Graph notification check"), ("GRAPH_TOKEN", "Graph read token"),
            ("DATABASE_URL", "Only for GAIK PgVectorStore (not used by default)")]
def isset(n): return bool((os.environ.get(n) or "").strip())

def key_report(chat, embed):
    out = [dict(env=n, set=isset(n), why=w, group="NCI") for n, w in NCI_KEYS]
    for role, p in (("chat", chat), ("embeddings", embed)):
        if p in PROVIDERS:
            for grp in PROVIDERS[p]["need"]: out.append(dict(env=" / ".join(grp), set=any(isset(n) for n in grp), why="required for %s via %s" % (role, p), group=role))
            for n in PROVIDERS[p]["opt"]: out.append(dict(env=n, set=isset(n), why="optional for %s via %s" % (role, p), group=role))
    return out

def chat_provider(): return (os.environ.get("NCI_LLM_PROVIDER") or "").strip() or None
def provider_ready(p): return bool(p in PROVIDERS and all(any(isset(n) for n in g) for g in PROVIDERS[p]["need"]))
def embed_provider():
    e = (os.environ.get("NCI_EMBED_PROVIDER") or "").strip().lower()
    if e in ("none", "off"): return None
    if e: return e
    c = chat_provider()
    if c in PROVIDERS and PROVIDERS[c]["embed"]: return c
    return next((p for p in ("openai", "google", "azure", "aitta") if provider_ready(p)), None)  # auto-detect another provider that has keys

# ---------------------------------------------------------------- text normalisation (domain synonyms, light stemming, optional Finnish lemmas)
STOP = set("what when where which would could should does have this that with from about there their they them then than into over under after before while still being your onto "
           "for and the who how why can was did you are its has but any all our out too now one two get got use used using "
           "kuka mitä missä milloin miten joka jotka että oli ovat sekä tämä tuo".split())
SYN = {"restart": "reboot", "restarted": "reboot", "restarts": "reboot", "rebooted": "reboot", "reboots": "reboot", "booting": "reboot", "offline": "unreachable",
       "reachability": "unreachable", "unreachable": "unreachable", "lost": "unreachable", "disconnect": "unreachable", "disconnects": "unreachable", "drop": "unreachable", "drops": "unreachable", "dropped": "unreachable", "door": "barrier",
       "gate": "barrier", "entry": "barrier", "entrance": "barrier", "voltage": "power", "feed": "power", "supply": "power", "release": "firmware", "version": "firmware", "software": "firmware",
       "fix": "workaround", "trick": "workaround", "hack": "workaround", "delay": "wait", "waiting": "wait", "waited": "wait", "minute": "wait", "seconds": "wait", "second": "wait"}
KIND_WORDS = {"workaround": "workaround fix reboot wait", "access": "access entry barrier lock key escort visit dispatch", "hypothesis": "cause possible hypothesis power",
              "validation_gap": "untested not validated gap firmware", "observation": "observation",
              "practice": "expert know-how practice rule thumb tip experience always never check"}
FI_HINT = re.compile(r"[äö]|\b(ja|on|ei|että|pitää|odota|sekuntia|kaapeli|laite|vika|avain|portti)\b", re.I)
_FI = {"p": None, "tried": False}
def fin():
    if not _FI["tried"]:
        _FI["tried"] = True
        try:
            from gaik.software_components.RAG.finnish_text_processor import FinnishTextProcessor
            p = FinnishTextProcessor("auto", decompound=False); _FI["p"] = p if p.backend_name != "simple" else None  # "simple" is not a lemmatizer
            _FI["name"] = p.backend_name
        except Exception as x: _FI["name"] = "unavailable: %s" % type(x).__name__
    return _FI["p"]
def stem(w):
    for suf in ("ing", "es", "ed", "s"):
        if len(w) > 4 + len(suf) and w.endswith(suf): return w[:-len(suf)]
    return w
def norm(text):
    base = [w for w in re.findall(r"[a-zåäö0-9][a-zåäö0-9.\-]{2,}", (text or "").lower()) if w not in STOP]
    if FI_HINT.search(text or "") and fin():
        try: base += [w.lower() for w in fin().lemmatize(text)]
        except Exception: pass
    return [SYN.get(w, stem(w)) for w in base]

DEFAULT_PROBES = dict(
    answerable=["how long should we wait after a reboot", "who can open the north barrier", "is the power feed a possible cause", "can we reuse the workaround on new firmware",
                "what changed with the site access key", "was the reboot fix tested on TRN-042", "why does management drop after restart"],
    unanswerable=["weather in Oslo tomorrow", "what is for lunch", "who won the football match", "how do I bake bread", "stock price of a company", "translate this poem into French"])

# ---------------------------------------------------------------- the orchestrator
class Orch:
    def __init__(self, put, rows, ents, embedder=None, generator_factory=None):
        self.put, self.rows, self.ents = put, rows, ents  # ents: name -> list of aliases
        self.embedder, self.gen_factory = embedder, generator_factory
        self.docs, self.idf, self.sig, self.store, self.vecs = [], {}, None, None, {}
        self.cal = {"lex": None, "sem": None}; self.floor_lex, self.floor_sem = 0.34, None
        self.fails, self.paused_until, self.last_error = 0, 0.0, None; self._sum = {}; self.lock = threading.RLock()

    # ---- providers
    def _cfg(self, p):
        from gaik.software_components.llm import get_llm_config
        return get_llm_config(p)
    def chat_ready(self):
        try: return bool(chat_provider()) and bool(self._cfg(chat_provider()))
        except Exception: return False
    def get_embedder(self):
        if self.embedder is not None: return self.embedder
        p = embed_provider()
        if not p or not PROVIDERS.get(p, {}).get("embed") or not provider_ready(p): return None
        try:
            from gaik.software_components.RAG.embedder import Embedder
            self.embedder = Embedder(self._cfg(p))
        except Exception as x: self.last_error = "embedder: %s" % x
        return self.embedder

    # ---- budget, breaker, usage
    def today(self): return dt.date.today().isoformat()
    def used_today(self, day=None): return sum(r.get("total", 0) for r in self.rows("use") if r.get("day") == (day or self.today()))
    def budget(self): return int(os.environ.get("NCI_DAILY_TOKEN_BUDGET") or 200000)
    def allow(self):
        """-> (ok, reason). Checked before every LLM call."""
        if time.time() < self.paused_until: return False, "circuit breaker open until %s" % time.strftime("%H:%M:%S", time.localtime(self.paused_until))
        if self.used_today() >= self.budget(): return False, "daily token budget reached (%d)" % self.budget()
        return True, "ok"
    def success(self): self.fails = 0
    def failure(self, err):
        self.fails += 1; self.last_error = str(err)[:300]
        if self.fails >= 3: self.paused_until = time.time() + float(os.environ.get("NCI_BREAKER_SECONDS") or 300); self.fails = 0
    def record(self, kind, usage, dur, est=False, provider=None, model=None):
        u = dict(usage or {}); inp = int(u.get("input_tokens") or u.get("prompt_tokens") or 0); out = int(u.get("output_tokens") or u.get("completion_tokens") or 0)
        total = int(u.get("total_tokens") or inp + out); cost = 0.0; prov = provider or chat_provider() or "unknown"
        if model is None: model = (self._cfg(prov).get("model") if self.chat_ready() else "") or ""
        try:
            from gaik.observability import build_usage_record
            cost = build_usage_record(prov, model, {"input_tokens": inp, "output_tokens": out}, dur).cost_usd
        except Exception: pass
        rid = "%d" % (time.time() * 1000); self.put("use", rid, dict(id=rid, kind=kind, day=self.today(), at=dt.datetime.now().isoformat(timespec="seconds"), input=inp, output=out, total=total, cost=cost, dur=round(dur, 2), est=est, provider=prov, model=model))

    # ---- index
    def _doc_text(self, a, M):
        ev = " ".join(M[i]["text"] for i in a["ev"] if i in M)
        al = " ".join(" ".join(self.ents.get(n, [])) for n in a["nodes"] + [a["entity"]])
        return " ".join([a["statement"], a["scope"], a["entity"], a["rel"], al, KIND_WORDS.get(a["kind"], ""), ev])
    def sync(self, A, M):
        sig = hashlib.md5(json.dumps([(a["id"], a["statement"], a["scope"], a["ev"]) for a in A], sort_keys=True).encode()).hexdigest()
        if sig == self.sig: return False
        with self.lock:
            self.docs = []
            for a in A:
                tf = {}
                for t in norm(self._doc_text(a, M)): tf[t] = tf.get(t, 0) + 1
                self.docs.append(dict(id=a["id"], tf=tf, text=self._doc_text(a, M), a=a))
            n = max(1, len(self.docs)); df = {}
            for d in self.docs:
                for t in d["tf"]: df[t] = df.get(t, 0) + 1
            self.idf = {t: math.log(1 + (n - c + .5) / (c + .5)) for t, c in df.items()}
            self._build_semantic(); self.sig = sig; self.cal = {"lex": None, "sem": None}
        return True
    def _build_semantic(self):
        self.store = None; emb = self.get_embedder()
        if not emb or not self.docs: return
        try:
            from langchain_core.documents import Document
            from gaik.software_components.RAG.vector_store import VectorStore
            for r in self.rows("emb"): self.vecs.setdefault(r["id"], r["v"])  # persisted cache: unchanged text is never re-embedded
            need = [d for d in self.docs if hashlib.md5(d["text"].encode()).hexdigest() not in self.vecs]
            if need:
                vs, _ = emb.embed([d["text"] for d in need])
                for d, v in zip(need, vs): h = hashlib.md5(d["text"].encode()).hexdigest(); self.vecs[h] = v; self.put("emb", h, dict(id=h, v=v))
                self.record("embedding", {"input_tokens": sum(len(d["text"]) for d in need) // 4}, 0, est=True)
            st = VectorStore(persist=False)
            st.add([Document(page_content=d["text"], metadata={"id": d["id"]}) for d in self.docs], [self.vecs[hashlib.md5(d["text"].encode()).hexdigest()] for d in self.docs])
            self.store = st
        except Exception as x: self.last_error = "semantic index: %s" % x; self.store = None

    # ---- retrieval
    def _lex(self, q):
        qt = list(dict.fromkeys(norm(q))); out = []
        for d in self.docs:
            hit = [t for t in qt if t in d["tf"]]
            out.append((d, sum(self.idf.get(t, 0) * d["tf"][t] / (d["tf"][t] + 1.2) for t in hit), len(hit) / max(1, len(qt))))
        return sorted([o for o in out if o[1] > 0], key=lambda o: -o[1])
    def _ent_arm(self, q):
        low = q.lower(); hit = [n for n, al in self.ents.items() if n.lower() in low or any(re.search(r"\b" + re.escape(a.lower()) + r"\b", low) for a in al)]
        return [d for d in self.docs if hit and (d["a"]["entity"] in hit or any(n in hit for n in d["a"]["nodes"]))]
    def search(self, q, k=5):
        """-> dict(ids=[(assertion_id, fused_score)], best_cov, best_sem, arms, ok). Lexical + entity (+ semantic) fused with GAIK Ranker (RRF)."""
        with self.lock:
            lex = self._lex(q); ent = self._ent_arm(q); best_cov = lex[0][2] if lex else 0.0; sem, best_sem = [], None
            if self.store and self.get_embedder():
                try:
                    sem = self.store.search(self.get_embedder().embed_query(q), top_k=k * 2); best_sem = sem[0][1] if sem else None
                except Exception as x: self.last_error = "semantic query: %s" % x
            arms = [("lexical", [(d["id"], s) for d, s, _ in lex[:k * 2]]), ("entity", [(d["id"], 1.0) for d in ent]), ("semantic", [(doc.metadata["id"], s) for doc, s in sem])]
            arms = [a for a in arms if a[1]]; ranker = "gaik.Ranker"
            try:
                from langchain_core.documents import Document
                from gaik.software_components.RAG.ranker import Ranker
                mk = lambda lst: [(Document(page_content=i, metadata={"id": i}), s) for i, s in lst]
                w = {"lexical": 0.4, "entity": 0.2, "semantic": 0.6}
                fused = Ranker(rrf_k=60).fuse(*[mk(l) for _, l in arms], weights=[w[n] for n, _ in arms], names=[n for n, _ in arms], top_k=k) if arms else []
                ids = [(d.metadata["id"], round(s, 5)) for d, s in fused]
            except Exception:  # same maths, no gaik
                ranker = "builtin RRF"; sc = {}
                for (n, l), wt in zip(arms, [{"lexical": .4, "entity": .2, "semantic": .6}[n] for n, _ in arms]):
                    for r, (i, _) in enumerate(l, 1): sc[i] = sc.get(i, 0) + wt / (60 + r)
                ids = sorted(sc.items(), key=lambda x: -x[1])[:k]
            return dict(ids=ids, best_cov=round(best_cov, 3), best_sem=best_sem, arms=[n for n, _ in arms], ranker=ranker, ok=self.gate(best_cov, best_sem))

    # ---- calibrated relevance gate
    def gate(self, cov, sem):
        try:
            from gaik.software_components.RAG.relevance_gate import RelevanceGate
            lex_ok = RelevanceGate(floor=self.floor_lex, lower_is_better=False).passes(cov)
            sem_ok = bool(self.floor_sem is not None and sem is not None and RelevanceGate(floor=self.floor_sem, lower_is_better=False).passes(sem))
        except Exception:
            lex_ok = cov >= self.floor_lex; sem_ok = bool(self.floor_sem is not None and sem is not None and sem >= self.floor_sem)
        return lex_ok or sem_ok
    def calibrate(self, probes=None, semantic=True):
        """Measure where answerable and unanswerable questions land on THIS corpus and set the floors from it (GAIK RelevanceGate.calibrate)."""
        probes = probes or DEFAULT_PROBES
        with self.lock:
            best = lambda qs: [(self._lex(q)[0][2] if self._lex(q) else 0.0) for q in qs]
            res = dict(lexical=self._cal(best(probes["answerable"]), best(probes["unanswerable"]), "lex"))
            if semantic and self.store and self.get_embedder():
                try:
                    e = self.get_embedder(); top = lambda qs: [(self.store.search(e.embed_query(q), top_k=1) or [(None, 0.0)])[0][1] for q in qs]
                    res["semantic"] = self._cal(top(probes["answerable"]), top(probes["unanswerable"]), "sem")
                except Exception as x: res["semantic"] = dict(error=str(x)[:120])
            self.cal = dict(self.cal, **res); return self.cal
    def _cal(self, ans, un, which):
        try:
            from gaik.software_components.RAG.relevance_gate import RelevanceGate
            r = RelevanceGate.calibrate(answerable=ans, unanswerable=un); sep, floor, txt = bool(r.separated), float(r.floor), str(r)
        except Exception:
            lo, hi = min(ans), max(un); sep, floor, txt = lo > hi, (lo + hi) / 2, "builtin: answerable>=%.3f unanswerable<=%.3f" % (lo, hi)
        if sep:
            if which == "lex": self.floor_lex = floor
            else: self.floor_sem = floor
        return dict(separated=sep, floor=round(floor, 3), reading=txt, applied=sep)

    # ---- grounded plain-language summary (GAIK AnswerGenerator), guarded
    def summarize(self, question, context):
        if (os.environ.get("NCI_LLM_SUMMARY") or "1") == "0" or not context: return None
        ok, why = self.allow()
        if not (self.gen_factory or self.chat_ready()) or not ok: return None
        h = hashlib.md5((question + context).encode()).hexdigest()
        if h in self._sum: return self._sum[h]
        PROMPT = ("You write for a network operations engineer who was NOT in the original chat. Use ONLY the facts below. Keep every caveat (unverified, not validated, stale, scope). "
                  "Do not add names, devices, versions, numbers or advice that are not in the facts. At most 4 sentences. If the facts do not answer the question, say so.\n\nFacts:\n{context}\n\nQuestion: {query}")
        t0 = time.perf_counter()
        try:
            if self.gen_factory: g = self.gen_factory(PROMPT)
            else:
                from gaik.software_components.RAG.answer_generator import AnswerGenerator
                g = AnswerGenerator(config=self._cfg(chat_provider()), citations=False, stream=False, prompt=PROMPT)
            out = (g.generate(question, context, stream=False) or "").strip(); self.success()
        except Exception as x: self.failure(x); return None
        self.record("summary", {"input_tokens": (len(context) + len(question)) // 4 + 120, "output_tokens": len(out) // 4}, time.perf_counter() - t0, est=True)
        ids = lambda t: set(re.findall(r"\b(?:TRN-\d{3}|CAB-\d+|GATE-[A-Z]+-\d+|HEL-DC-\d+|FW-\d\.\d\.\d|\d\.\d\.\d|C-\d{3})\b", t)) | set(re.findall(r"\b\d+(?=[- ]second)", t))
        res = out if out and ids(out) <= ids(context + " " + question) else None  # drop a summary that invents an id, version or number
        self._sum[h] = res; return res

    # ---- dashboard
    def components(self, curator_ok):
        emb = self.get_embedder(); f = fin() or None; ok, why = self.allow(); cp, ep = chat_provider(), embed_provider()
        S = lambda stage, comp, state, note: dict(stage=stage, component=comp, state=state, note=note)
        try: import gaik; gaik_ok = True
        except Exception: gaik_ok = False
        return [S("Capture", "Teams UI / outgoing webhook / Graph", "active", "channel allowlist enforced"),
                S("Normalise", "gaik SourceNormalizer (NormalizedSources)", "active" if gaik_ok else "off", "keeps the message id on every line"),
                S("Extract (fast)", "NCI rules", "active", "milliseconds, no cost"),
                S("Extract (slow)", "gaik KnowledgeCurator", "active" if curator_ok and ok else "paused" if curator_ok else "off", why if curator_ok else "set NCI_LLM_PROVIDER and its key"),
                S("Verify", "gaik quote_in_text", "active" if gaik_ok else "fallback", "a fact needs a verbatim quote inside one message"),
                S("Index", "gaik Embedder + VectorStore", "active" if self.store else "fallback", "semantic search" if self.store else "lexical only: %s" % (self.last_error or "no embedding provider (%s)" % (ep or "none"))),
                S("Fuse", "gaik Ranker (weighted RRF)", "active" if gaik_ok else "fallback", "lexical + entity" + (" + semantic" if self.store else "")),
                S("Gate", "gaik RelevanceGate", "active" if gaik_ok else "fallback", "lexical floor %.2f%s" % (self.floor_lex, "" if self.floor_sem is None else ", semantic floor %.2f" % self.floor_sem)),
                S("Finnish", "gaik FinnishTextProcessor", "active" if f else "off", _FI.get("name", "not loaded") if not f else "backend " + _FI["name"]),
                S("Chat assistant", "OpenAI Chat Completions (llm.py)", "active" if __import__("llm").ready() and ok else "structured only", "grounded in retrieved facts; answers citing unknown sources, devices, numbers or people are discarded"),
                S("Answer", "NCI structured answer + gaik AnswerGenerator summary", "active" if (self.gen_factory or self.chat_ready()) and ok else "structured only", "summary is checked against the facts"),
                S("Observe", "gaik observability UsageRecord", "active" if gaik_ok else "fallback", "tokens and cost per call"),
                S("Not wired", "PgVectorStore, LLMJudge, RAGWorkflow, Transcriber", "off", "Postgres not needed at this size; Judge is page-image based; RAGWorkflow is document RAG; Transcriber = future meeting recordings")]
    def usage_summary(self, day=None):
        R = self.rows("use"); d = day or self.today()
        return dict(budget=self.budget(), used_today=self.used_today(d), cost_total=round(sum(r.get("cost", 0) for r in R), 4), calls=len(R), paused=time.time() < self.paused_until, last_error=self.last_error,
                    recent=list(reversed(R))[:8], providers=dict(chat=chat_provider(), embed=embed_provider()))
