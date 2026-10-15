"""
Authenova — Claim Intelligence Engine (Windows-friendly local backend)

Pipeline
  1. Query planning   Groq LLM turns the claim into 2-3 web search queries
  2. Live retrieval   Tavily Search API fetches pages for each query
  3. Evidence ranking SBERT (all-MiniLM-L6-v2) picks the sentences closest to the claim
  4. Stance           DeBERTa-v3 NLI cross-encoder: does each sentence support,
                      contradict, or not address the claim?
  5. Verdict          Source-level vote -> Likely Supported / Likely Contradicted /
                      Inconclusive, plus a qualitative evidence strength
  6. Fact-checkers    Google Fact Check Tools API: verdicts from professional
                      fact-checkers (ClaimReview) when this claim was checked before
  7. Write-up         Groq writes the reasoning and uncertainty from the evidence only

Keys (in .env next to server.py):
  TAVILY_API_KEY   required
  FACTCHECK_API_KEY  optional, Google API key with the Fact Check Tools API enabled
  GROQ_API_KEY     recommended (query planning + reasoning). Without it the claim
                   itself is used as the query and a template write-up is used.

Models (optional, installed by setup.bat):
  sentence-transformers  -> SBERT + DeBERTa NLI run locally on CPU
  If they're not installed, ranking falls back to keyword overlap and stance
  falls back to the Groq LLM, so the engine still works.

Response shape is identical to the original /api/claims/investigate contract.
"""

import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, urlencode

TAVILY_URL = "https://api.tavily.com/search"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
FACTCHECK_URL = "https://factchecktools.googleapis.com/v1alpha1/claims:search"

SBERT_MODEL = os.environ.get("SBERT_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
NLI_MODEL = os.environ.get("NLI_MODEL", "cross-encoder/nli-deberta-v3-small")

TOP_SENTENCES = 16          # sentences sent to NLI
SUPPORT_T = 0.60            # entailment prob to count as supporting
CONTRA_T = 0.60             # contradiction prob to count as contradicting
UA = "Authenova/1.0"


class ClaimError(Exception):
    """Error with a message that is safe to show to the user."""
    def __init__(self, message, status=502):
        super().__init__(message)
        self.message = message
        self.status = status


def log(msg):
    print(f"[claims] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def _key(name):
    v = os.environ.get(name, "").strip()
    return v if v and not v.lower().startswith("your_") else None


GROQ_FALLBACK_MODELS = ["openai/gpt-oss-120b", "qwen/qwen3-32b", "openai/gpt-oss-20b",
                        "llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
_groq_state = {"model": None}


def groq_models():
    """Preferred model first (GROQ_MODEL or the last one that worked), then fallbacks."""
    order = []
    for m in [_groq_state["model"], os.environ.get("GROQ_MODEL", "").strip()] + GROQ_FALLBACK_MODELS:
        if m and m not in order:
            order.append(m)
    return order


# ---------------------------------------------------------------------------
# Local models (lazy, thread-safe, optional)
# ---------------------------------------------------------------------------
_models = {"sbert": None, "nli": None, "labels": None, "state": "unloaded", "error": None}
_model_lock = threading.Lock()


def models_available():
    try:
        import sentence_transformers  # noqa: F401
        return True
    except Exception:
        return False


def load_models():
    """Load SBERT + NLI once. Returns True if local models are usable."""
    if _models["state"] == "ready":
        return True
    if _models["state"] == "failed":
        return False
    with _model_lock:
        if _models["state"] in ("ready", "failed"):
            return _models["state"] == "ready"
        try:
            from sentence_transformers import SentenceTransformer, CrossEncoder
            log(f"loading {SBERT_MODEL} (first run downloads it)...")
            sbert = SentenceTransformer(SBERT_MODEL, device="cpu")
            log(f"loading {NLI_MODEL} (first run downloads it)...")
            nli = CrossEncoder(NLI_MODEL, device="cpu")
            id2label = getattr(nli.model.config, "id2label", None) or {0: "contradiction", 1: "entailment", 2: "neutral"}
            labels = [str(id2label[i]).lower() for i in range(len(id2label))]
            _models.update(sbert=sbert, nli=nli, labels=labels, state="ready")
            log("local models ready")
            return True
        except Exception as e:
            _models.update(state="failed", error=repr(e))
            log(f"local models unavailable, using fallbacks: {e!r}")
            return False


def warm_up_async():
    if models_available():
        threading.Thread(target=load_models, daemon=True).start()


def status_line():
    parts = []
    parts.append("Tavily key: " + ("ok" if _key("TAVILY_API_KEY") else "MISSING"))
    parts.append("Groq key: " + ("ok" if _key("GROQ_API_KEY") else "missing (template write-up)"))
    parts.append("Fact Check: " + ("ok" if factcheck_key() else "off"))
    parts.append("SBERT/DeBERTa: " + ("installed" if models_available() else "not installed (LLM fallback)"))
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _post_json(url, payload, headers, timeout):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA, **headers},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def groq_json(system, user, max_tokens=700, timeout=30):
    """Call Groq chat completions and parse a JSON object reply. Returns dict or None.
    If a model is retired or unavailable, tries the next one and remembers what works."""
    key = _key("GROQ_API_KEY")
    if not key:
        return None
    for model in groq_models():
        payload = {
            "model": model,
            "temperature": 0.1,
            # reasoning models (gpt-oss, qwen3) spend tokens thinking before the JSON
            "max_tokens": max(max_tokens, 1500),
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        try:
            data = _post_json(GROQ_URL, payload, {"Authorization": f"Bearer {key}"}, timeout)
            text = data["choices"][0]["message"]["content"] or ""
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
            m = re.search(r"\{.*\}", text, flags=re.S)
            result = json.loads(m.group(0) if m else text)
            if _groq_state["model"] != model:
                log(f"using Groq model {model}")
                _groq_state["model"] = model
            return result
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            log(f"groq HTTP {e.code} ({model}): {body}")
            if e.code in (401, 403):
                return None       # bad key: no point retrying
            # retired/unknown model, rate limit, invalid JSON reply, overloaded -> try next model
            if e.code in (400, 404, 413, 429, 498, 500, 502, 503):
                continue
            return None
        except (ValueError, KeyError) as e:
            log(f"groq bad reply ({model}): {e!r}")
            continue
        except Exception as e:
            log(f"groq error ({model}): {e!r}")
            return None
    return None


# ---------------------------------------------------------------------------
# 1. Query planning
# ---------------------------------------------------------------------------
def plan_queries(claim):
    out = groq_json(
        "You plan web searches for fact-checking. Reply with JSON only.",
        "Write 3 short, distinct web search queries that would find evidence to verify or refute "
        "this claim. Include one neutral query for the core fact, one that looks for official or "
        "authoritative sources, and one that would surface contradicting evidence if the claim is false. "
        'Return {"queries": ["...", "...", "..."]}.\n\nClaim: ' + claim,
        max_tokens=200,
    )
    queries = []
    if isinstance(out, dict) and isinstance(out.get("queries"), list):
        queries = [str(q).strip() for q in out["queries"] if str(q).strip()][:3]
    if claim not in queries:
        queries.insert(0, claim)
    return queries[:3]


# ---------------------------------------------------------------------------
# 2. Retrieval
# ---------------------------------------------------------------------------
def tavily_search(query, max_results):
    key = _key("TAVILY_API_KEY")
    payload = {
        "api_key": key,
        "query": query[:380],
        "search_depth": "advanced",
        "max_results": max(1, min(int(max_results or 4), 8)),
        "include_answer": False,
        "include_raw_content": True,
    }
    try:
        data = _post_json(TAVILY_URL, payload, {"Authorization": f"Bearer {key}"}, timeout=30)
        return data.get("results", []) or []
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        log(f"tavily HTTP {e.code}: {body}")
        if e.code in (401, 403):
            raise ClaimError("The web search service rejected the Tavily key. Check the key in the settings file next to server.py.", 502)
        if e.code == 429 or e.code == 432 or e.code == 433:
            raise ClaimError("The web search service limit was reached. Wait a moment or check your Tavily plan.", 429)
        return []
    except Exception as e:
        log(f"tavily error: {e!r}")
        return []


def retrieve(queries, max_results):
    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        batches = list(pool.map(lambda q: tavily_search(q, max_results), queries))
    seen, docs = set(), []
    for batch in batches:
        for r in batch:
            url = (r.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            text = " ".join(filter(None, [r.get("content") or "", (r.get("raw_content") or "")[:8000]]))
            docs.append({
                "url": url,
                "title": (r.get("title") or "").strip(),
                "domain": urlparse(url).netloc.replace("www.", ""),
                "text": text,
            })
    return docs


# ---------------------------------------------------------------------------
# 3. Sentences + ranking
# ---------------------------------------------------------------------------
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
_JUNK = re.compile(r"(cookie|subscribe|sign in|log in|javascript|newsletter|all rights reserved|click here|advertisement)", re.I)


def split_sentences(text):
    text = re.sub(r"\s+", " ", re.sub(r"[#*|>_\[\]]+", " ", text or "")).strip()
    out = []
    for s in _SENT_SPLIT.split(text):
        s = s.strip()
        if 30 <= len(s) <= 400 and not _JUNK.search(s) and len(s.split()) >= 5:
            out.append(s)
    return out


def _tokens(s):
    return set(w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2)


def rank_sentences(claim, docs):
    """Return list of candidates: {doc, sentence, sim} sorted by relevance."""
    cands = []
    for d in docs:
        for s in split_sentences(d["text"])[:120]:
            cands.append({"doc": d, "sentence": s})
    if not cands:
        return []

    if load_models():
        from sentence_transformers import util
        sbert = _models["sbert"]
        emb_c = sbert.encode(claim, convert_to_tensor=True, normalize_embeddings=True)
        emb_s = sbert.encode([c["sentence"] for c in cands], convert_to_tensor=True,
                             normalize_embeddings=True, batch_size=64)
        sims = util.cos_sim(emb_c, emb_s)[0].tolist()
        for c, sim in zip(cands, sims):
            c["sim"] = float(sim)
    else:
        ct = _tokens(claim) or {""}
        for c in cands:
            st = _tokens(c["sentence"])
            c["sim"] = len(ct & st) / (len(ct) ** 0.5 * (len(st) or 1) ** 0.5)

    cands.sort(key=lambda c: c["sim"], reverse=True)
    # Keep diversity: at most 3 sentences per source
    picked, per_doc = [], {}
    for c in cands:
        u = c["doc"]["url"]
        if per_doc.get(u, 0) >= 3:
            continue
        per_doc[u] = per_doc.get(u, 0) + 1
        picked.append(c)
        if len(picked) >= TOP_SENTENCES:
            break
    return picked


# ---------------------------------------------------------------------------
# 4. Stance
# ---------------------------------------------------------------------------
def _softmax(row):
    import math
    m = max(row)
    e = [math.exp(x - m) for x in row]
    s = sum(e)
    return [x / s for x in e]


def stance_nli(claim, cands):
    nli, labels = _models["nli"], _models["labels"]
    logits = nli.predict([(c["sentence"], claim) for c in cands], batch_size=16)
    for c, row in zip(cands, logits):
        p = dict(zip(labels, _softmax(list(map(float, row)))))
        ent, con = p.get("entailment", 0.0), p.get("contradiction", 0.0)
        if ent >= SUPPORT_T and ent > con:
            c["stance"], c["conf"] = "support", ent
        elif con >= CONTRA_T and con > ent:
            c["stance"], c["conf"] = "contradict", con
        else:
            c["stance"], c["conf"] = "neutral", p.get("neutral", 0.0)


def stance_llm(claim, cands):
    listing = "\n".join(f"{i+1}. {c['sentence']}" for i, c in enumerate(cands))
    out = groq_json(
        "You are a careful natural language inference judge. Reply with JSON only.",
        "For each numbered passage, decide if it SUPPORTS the claim, CONTRADICTS it, or is NEUTRAL "
        "(does not settle it). Judge only from the passage text. Give a confidence from 0 to 1.\n"
        'Return {"results": [{"id": 1, "stance": "support|contradict|neutral", "confidence": 0.0}]}\n\n'
        f"Claim: {claim}\n\nPassages:\n{listing}",
        max_tokens=900,
    )
    results = {}
    if isinstance(out, dict):
        for r in out.get("results", []) or []:
            try:
                results[int(r.get("id"))] = r
            except Exception:
                pass
    for i, c in enumerate(cands, 1):
        r = results.get(i, {})
        st = str(r.get("stance", "neutral")).lower()
        st = "support" if st.startswith("support") else "contradict" if st.startswith("contra") else "neutral"
        try:
            conf = float(r.get("confidence", 0.5))
        except Exception:
            conf = 0.5
        if st != "neutral" and conf < 0.6:
            st = "neutral"
        c["stance"], c["conf"] = st, conf


# ---------------------------------------------------------------------------
# Professional fact-checks (Google Fact Check Tools API)
# ---------------------------------------------------------------------------
def factcheck_key():
    return _key("FACTCHECK_API_KEY") or _key("GOOGLE_FACTCHECK_API_KEY") or _key("GOOGLE_API_KEY")


_FALSE_WORDS = ("false", "fake", "incorrect", "not true", "untrue", "wrong", "pants on fire", "hoax",
                "baseless", "fabricated", "misleading", "misattributed", "doctored", "altered", "scam",
                "debunked", "no evidence", "inaccurate", "distorted", "satire", "four pinocchios",
                "three pinocchios", "mostly false", "rumour", "rumor", "manipulated")
_TRUE_WORDS = ("true", "correct", "accurate", "verified", "confirmed", "legit", "real")
_MIXED_WORDS = ("half true", "half-true", "partly", "partially", "mixture", "mixed", "missing context",
                "needs context", "unproven", "unverified", "lacks context", "exaggerat", "outdated",
                "two pinocchios", "one pinocchio", "explainer", "cherry")


def rating_polarity(rating):
    r = (rating or "").lower()
    if any(w in r for w in _MIXED_WORDS):
        return "mixed"
    if any(w in r for w in _FALSE_WORDS):
        return "false"
    if any(w in r for w in _TRUE_WORDS):
        return "true"
    return "unknown"


def fetch_factchecks(claim):
    key = factcheck_key()
    if not key:
        return []
    qs = urlencode({"query": claim[:300], "languageCode": "en", "pageSize": 10, "key": key})
    req = urllib.request.Request(f"{FACTCHECK_URL}?{qs}", headers={"Accept": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log(f"factcheck HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
        return []
    except Exception as e:
        log(f"factcheck error: {e!r}")
        return []

    out = []
    for c in data.get("claims", []) or []:
        for r in (c.get("claimReview") or [])[:2]:
            url = (r.get("url") or "").strip()
            if not url:
                continue
            pub = r.get("publisher") or {}
            out.append({
                "checked_claim": (c.get("text") or "").strip(),
                "claimant": (c.get("claimant") or "").strip(),
                "claim_date": (c.get("claimDate") or "")[:10],
                "publisher": (pub.get("name") or pub.get("site") or urlparse(url).netloc).strip(),
                "site": (pub.get("site") or urlparse(url).netloc.replace("www.", "")).strip(),
                "url": url,
                "title": (r.get("title") or "").strip(),
                "review_date": (r.get("reviewDate") or "")[:10],
                "rating": (r.get("textualRating") or "").strip(),
            })
    return out[:10]


def _similarity(a, b):
    if _models["state"] == "ready":
        from sentence_transformers import util
        e = _models["sbert"].encode([a, b], convert_to_tensor=True, normalize_embeddings=True)
        return float(util.cos_sim(e[0], e[1]))
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / (len(ta) ** 0.5 * len(tb) ** 0.5)


def match_factchecks(claim, fcs):
    """Decide how each fact-checked claim relates to the user's claim, then map
    its rating to support / contradict / neutral for the user's claim."""
    if not fcs:
        return []
    relations, verdicts = {}, {}
    listing = "\n".join(f'{i+1}. "{f["checked_claim"]}" (rated: {f["rating"]})' for i, f in enumerate(fcs))
    out = groq_json(
        "You match fact-checked claims to a user's claim. Reply with JSON only.",
        "For each numbered fact-checked claim, say how it relates to the user's claim:\n"
        "- same: asserts the same thing (wording may differ)\n"
        "- opposite: asserts the negation / opposite of the user's claim\n"
        "- unrelated: a different claim (different subject, event, number, date or place)\n"
        "Also read the fact-checker's rating text (it may be a label like 'False' or a full sentence) and say "
        "what it concludes about THE FACT-CHECKED CLAIM: true, false, or mixed.\n"
        'Return {"results": [{"id": 1, "relation": "same|opposite|unrelated", "rating_verdict": "true|false|mixed"}]}\n\n'
        f"User's claim: {claim}\n\nFact-checked claims:\n{listing}",
        max_tokens=400,
    )
    if isinstance(out, dict):
        for r in out.get("results", []) or []:
            try:
                relations[int(r.get("id"))] = str(r.get("relation", "")).lower()
                verdicts[int(r.get("id"))] = str(r.get("rating_verdict", "")).lower()
            except Exception:
                pass

    matched = []
    for i, f in enumerate(fcs, 1):
        rel = relations.get(i)
        if rel is None:  # no LLM available: only trust near-identical wording
            rel = "same" if _similarity(claim, f["checked_claim"]) >= 0.75 else "unrelated"
        if rel not in ("same", "opposite"):
            continue
        pol = rating_polarity(f["rating"])
        if pol == "unknown" and verdicts.get(i) in ("true", "false", "mixed"):
            pol = verdicts[i]          # free-text rating interpreted by the LLM
        if pol in ("mixed", "unknown"):
            stance = "neutral"
        elif (pol == "true") == (rel == "same"):
            stance = "support"
        else:
            stance = "contradict"
        matched.append({**f, "relation": rel, "stance": stance})
    return matched


def fc_to_item(f):
    quote = f'{f["publisher"]} rated the claim "{f["checked_claim"]}" as: {f["rating"]}.'
    if f["relation"] == "opposite":
        quote += " (That claim states the opposite of yours.)"
    return {
        "source_title": f["title"] or f'{f["publisher"]} fact-check',
        "domain": f["site"],
        "url": f["url"],
        "evidence_sentence": quote,
        "strength": "Fact-checker verdict",
    }


# ---------------------------------------------------------------------------
# 5. Aggregate
# ---------------------------------------------------------------------------
def aggregate(cands):
    """One stance per source: its strongest decisive sentence, else neutral."""
    by_src = {}
    for c in cands:
        u = c["doc"]["url"]
        best = by_src.get(u)
        score = (c["conf"] if c["stance"] != "neutral" else 0.0, c["sim"])
        if best is None or score > best[0]:
            by_src[u] = (score, c)
    sources = [v[1] for v in by_src.values()]
    sup = sorted([c for c in sources if c["stance"] == "support"], key=lambda c: -c["conf"])
    con = sorted([c for c in sources if c["stance"] == "contradict"], key=lambda c: -c["conf"])
    neu = [c for c in sources if c["stance"] == "neutral"]
    return sup, con, neu


def decide(sup, con, fc_sup=0, fc_con=0):
    # A professional fact-check counts as two independent web sources
    s, c = len(sup) + 2 * fc_sup, len(con) + 2 * fc_con
    if s == 0 and c == 0:
        verdict = "Inconclusive"
    elif s >= 1 and s >= 2 * c and s > c:
        verdict = "Likely Supported"
    elif c >= 1 and c >= 2 * s and c > s:
        verdict = "Likely Contradicted"
    else:
        verdict = "Inconclusive"

    decisive = sup + con
    avg = sum(x["conf"] for x in decisive) / len(decisive) if decisive else 0.0
    if fc_sup or fc_con:
        avg = max(avg, 0.85)
    margin = abs(s - c)
    if margin >= 3 and avg >= 0.8:
        strength = "Strong evidence"
    elif margin >= 2 or (margin >= 1 and avg >= 0.75):
        strength = "Moderate evidence"
    else:
        strength = "Limited evidence"
    if verdict == "Inconclusive" and decisive and margin <= 1 and s and c:
        strength = "Mixed evidence"
    return verdict, strength


def to_item(c):
    d = c["doc"]
    return {
        "source_title": d["title"] or d["domain"],
        "domain": d["domain"],
        "url": d["url"],
        "evidence_sentence": c["sentence"],
        "strength": "High relevance" if c["sim"] >= 0.6 else "Moderate relevance" if c["sim"] >= 0.4 else "Related",
    }


# ---------------------------------------------------------------------------
# 6. Write-up
# ---------------------------------------------------------------------------
def write_up(claim, verdict, strength, sup, con, neu, fcs=None):
    fcs = fcs or []
    def fmt(items, tag):
        return "\n".join(f"[{tag}] {c['doc']['domain']}: {c['sentence']}" for c in items[:5])
    evidence = "\n".join(filter(None, [fmt(sup, "SUPPORTS"), fmt(con, "CONTRADICTS"), fmt(neu[:3], "NEUTRAL")]))
    if fcs:
        evidence += "\n" + "\n".join(
            f'[FACT-CHECK -> {f["stance"].upper()}] {f["publisher"]} rated "{f["checked_claim"]}" as "{f["rating"]}"'
            + (" (opposite wording of the user's claim)" if f["relation"] == "opposite" else "")
            for f in fcs[:5])

    out = groq_json(
        "You explain fact-check verdicts. Use ONLY the evidence given; never add outside facts. "
        "Plain, neutral language. Reply with JSON only.",
        f"Claim: {claim}\nVerdict (already decided): {verdict} ({strength})\n"
        f"Counts: {len(sup)} supporting, {len(con)} contradicting, {len(neu)} neutral web sources; "
        f"{len(fcs)} matching professional fact-check(s).\n\n"
        f"Evidence:\n{evidence or '(no usable evidence found)'}\n\n"
        "Write:\n- reasoning: 3-5 sentences explaining why the evidence leads to this verdict, naming sources by domain. "
        "If professional fact-checks are listed, mention them first.\n"
        "- uncertainty: 1-2 sentences on the limits (e.g. few sources, dates, ambiguity, partial matches).\n"
        'Return {"reasoning": "...", "uncertainty": "..."}',
        max_tokens=500,
    )
    if isinstance(out, dict) and out.get("reasoning"):
        return str(out["reasoning"]).strip(), str(out.get("uncertainty") or "").strip()

    # Template fallback
    s, c, n = len(sup), len(con), len(neu)
    if verdict == "Likely Supported":
        r = f"{s} source{'s' if s != 1 else ''} directly support the claim"
        r += f", while {c} contradict it." if c else " and none contradict it."
    elif verdict == "Likely Contradicted":
        r = f"{c} source{'s' if c != 1 else ''} contradict the claim"
        r += f", while {s} support it." if s else " and none support it."
    elif s or c:
        r = f"The evidence is split: {s} supporting and {c} contradicting source{'s' if (s + c) != 1 else ''}."
    else:
        r = "None of the retrieved passages clearly support or contradict the claim."
    fsup = sum(1 for f in fcs if f["stance"] == "support")
    fcon = sum(1 for f in fcs if f["stance"] == "contradict")
    if fsup or fcon:
        r = (f"Professional fact-checkers: {fsup} rating{'s' if fsup != 1 else ''} back the claim and "
             f"{fcon} reject it. Web evidence: ") + r[0].lower() + r[1:]
    if n:
        r += f" {n} other source{'s' if n != 1 else ''} discussed the topic without settling it."
    u = "This verdict is based on a limited set of live web results and automated stance detection."
    if s + c < 3:
        u = "Only a few sources addressed the claim directly, so the verdict could change with more evidence."
    return r, u


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def investigate(claim, max_results=4):
    claim = (claim or "").strip()
    if len(claim) < 5:
        raise ClaimError("Enter a claim of at least 5 characters to investigate.", 400)
    if len(claim) > 1000:
        raise ClaimError("Shorten the claim to 1000 characters or fewer.", 400)
    if not _key("TAVILY_API_KEY"):
        raise ClaimError("Claim investigation isn't set up yet. Add a Tavily key to the settings file next to server.py.", 500)

    with ThreadPoolExecutor(max_workers=2) as pool:
        fc_future = pool.submit(fetch_factchecks, claim)
        queries = plan_queries(claim)
        log(f"queries: {queries}")
        docs = retrieve(queries, max_results)
        raw_fcs = fc_future.result()
    log(f"sources retrieved: {len(docs)}, fact-checks found: {len(raw_fcs)}")
    fcs = match_factchecks(claim, raw_fcs)
    log(f"fact-checks matching this claim: {len(fcs)}")
    if not docs and not fcs:
        raise ClaimError("No web results came back for this claim. Try rephrasing it.", 502)

    cands = rank_sentences(claim, docs) if docs else []
    if not cands:
        sup = con = []
        neu = []
    else:
        if _models["state"] == "ready":
            stance_nli(claim, cands)
        elif _key("GROQ_API_KEY"):
            stance_llm(claim, cands)
        else:
            for c in cands:
                c["stance"], c["conf"] = "neutral", 0.0
        sup, con, neu = aggregate(cands)

    fc_sup = [f for f in fcs if f["stance"] == "support"]
    fc_con = [f for f in fcs if f["stance"] == "contradict"]
    verdict, strength = decide(sup, con, len(fc_sup), len(fc_con))
    reasoning, uncertainty = write_up(claim, verdict, strength, sup, con, neu, fcs)

    used = {c["doc"]["url"] for c in sup + con + neu}
    sources = [{"title": d["title"] or d["domain"], "domain": d["domain"], "url": d["url"]}
               for d in docs if d["url"] in used] or \
              [{"title": d["title"] or d["domain"], "domain": d["domain"], "url": d["url"]} for d in docs[:8]]

    return {
        "claim": claim,
        "verdict": verdict,
        "evidence_strength": strength,
        "counts": {"supporting": len(sup) + len(fc_sup), "contradicting": len(con) + len(fc_con), "neutral": len(neu)},
        "reasoning": reasoning,
        "uncertainty": uncertainty,
        "supporting_evidence": [fc_to_item(f) for f in fc_sup] + [to_item(c) for c in sup],
        "contradicting_evidence": [fc_to_item(f) for f in fc_con] + [to_item(c) for c in con],
        "sources": sources,
        "fact_checks": [{
            "publisher": f["publisher"], "site": f["site"], "url": f["url"], "title": f["title"],
            "rating": f["rating"], "checked_claim": f["checked_claim"], "claimant": f["claimant"],
            "review_date": f["review_date"], "stance": f["stance"], "relation": f["relation"],
        } for f in fcs],
    }
