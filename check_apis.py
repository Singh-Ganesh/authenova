#!/usr/bin/env python3
"""
Authenova API check: tests each key with one small live request.
Never prints key values. Run with check.bat (Windows) or: python check_apis.py
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server          # noqa: E402  (for load_env)
import claim_engine    # noqa: E402

UA = {"User-Agent": "Authenova-check/1.0", "Accept": "application/json"}


def mask_errors(text, keys):
    for k in keys:
        if k:
            text = text.replace(k, "<key>")
    return text


def http_json(req, timeout=25):
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def explain(e, keys):
    if isinstance(e, urllib.error.HTTPError):
        body = e.read().decode("utf-8", "replace")
        try:
            j = json.loads(body)
            msg = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else (j.get("detail") or j.get("error") or body)
        except Exception:
            msg = body
        return f"HTTP {e.code}: {mask_errors(str(msg), keys)[:220]}"
    if isinstance(e, urllib.error.URLError):
        return f"cannot connect ({e.reason})"
    return repr(e)[:220]


def check_tavily(key):
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=json.dumps({"api_key": key, "query": "Taj Mahal location", "max_results": 1, "search_depth": "basic"}).encode(),
        headers={**UA, "Content-Type": "application/json", "Authorization": f"Bearer {key}"}, method="POST")
    _, d = http_json(req)
    n = len(d.get("results", []))
    return n > 0, f"{n} result(s) returned"


def check_groq(key):
    out = claim_engine.groq_json("Reply with JSON only.", 'Return {"ok": true}', max_tokens=300)
    if isinstance(out, dict):
        return True, f"model {claim_engine._groq_state['model']} answered"
    return False, "no Groq model answered (see [claims] lines above for the reason)"


def check_factcheck(key):
    qs = urllib.parse.urlencode({"query": "The Earth is flat", "languageCode": "en", "pageSize": 5, "key": key})
    req = urllib.request.Request(f"{claim_engine.FACTCHECK_URL}?{qs}", headers=UA)
    _, d = http_json(req)
    claims = d.get("claims", [])
    if not claims:
        return True, "API works, but returned 0 fact-checks for the test claim"
    rv = (claims[0].get("claimReview") or [{}])[0]
    pub = (rv.get("publisher") or {}).get("name", "?")
    return True, f"{len(claims)} fact-check(s) found, e.g. {pub}: \"{rv.get('textualRating', '')}\""


def main():
    server.load_env()
    keys = {
        "Tavily (web search)": ("TAVILY_API_KEY", check_tavily),
        "Groq (LLM)": ("GROQ_API_KEY", check_groq),
        "Google Fact Check": ("FACTCHECK_API_KEY", check_factcheck),
    }
    secrets = [os.environ.get(v[0], "") for v in keys.values()] + [os.environ.get("HIVE_API_KEY", "")]
    print("=" * 60)
    print(" Authenova API check")
    print("=" * 60)
    print(f" Hive (media):        {'key present' if os.environ.get('HIVE_API_KEY') else 'MISSING'} (not called, to save credits)")
    all_ok = True
    for label, (env, fn) in keys.items():
        key = os.environ.get(env, "").strip()
        if not key:
            print(f" {label:<20} MISSING  - add {env}=... to .env")
            all_ok = False
            continue
        try:
            ok, info = fn(key)
        except Exception as e:
            ok, info = False, explain(e, secrets)
        all_ok &= ok
        print(f" {label:<20} {'OK  ' if ok else 'FAIL'}     - {info}")
    print(f" Local SBERT/DeBERTa: {'installed' if claim_engine.models_available() else 'not installed (optional, run setup.bat)'}")
    print("=" * 60)
    print(" All good." if all_ok else " Fix the items marked FAIL or MISSING, then run this again.")
    print("=" * 60)


if __name__ == "__main__":
    main()
