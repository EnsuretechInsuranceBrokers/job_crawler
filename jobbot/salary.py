"""Expected salary, worked out per job instead of one fixed figure.

1. If the posting states a pay range, the market figure is its midpoint.
2. Otherwise the AI is asked for the typical (median) base salary for this role, company and
   location at the candidate's experience level. It answers from what it learned in training;
   it cannot browse, so it is an estimate.
The ask is that market figure plus a small premium (profile: salary.premium, default 8%), never
below the candidate's current pay plus 20%. If neither source is available the question is left
for the user rather than guessed.
"""
import json
import re

from .util import detail

EXPECTED = re.compile(r"expect|desired|salary requirement|compensation requirement|(ctc|salary|pay|compensation).{0,30}(looking|seeking|ask)"
                      r"|what (are|is) your (salary|compensation|pay)|target (base|salary|compensation|pay|ctc)", re.I)
CURRENT = re.compile(r"current|present|last drawn|existing|previous", re.I)
LAKH, CRORE = 100_000, 10_000_000
SYMBOL = {"$": "USD", "£": "GBP", "€": "EUR", "₹": "INR", "C$": "CAD", "A$": "AUD", "S$": "SGD"}

SYSTEM = """You estimate salaries. Give the typical (median) annual BASE salary that this company, or companies of
its size and kind, pay in this location for this role to someone with the stated years of experience.
Use the local currency of the job's location. Reply with JSON only:
{"currency": "ISO code", "median": number, "low": number, "high": number, "basis": "one short sentence"}"""


def is_expected_salary(label):
    return bool(EXPECTED.search(label or "")) and not CURRENT.search(label or "") and \
        re.search(r"salary|ctc|compensation|pay\b|package|remuneration|expectation", label or "", re.I) is not None


def _amount(num, suffix):
    v = float(num.replace(",", ""))
    s = (suffix or "").lower()
    if s in ("k",):
        v *= 1000
    elif s in ("l", "lac", "lakh", "lakhs", "lpa"):
        v *= LAKH
    elif s in ("cr", "crore", "crores"):
        v *= CRORE
    elif s in ("m", "mn", "million"):
        v *= 1_000_000
    return v


RANGE = re.compile(
    r"(?P<cur>C\$|A\$|S\$|\$|£|€|₹|USD|GBP|EUR|INR|CAD|AUD|SGD|Rs\.?)?\s?(?P<a>\d[\d,]*(?:\.\d+)?)\s?(?P<sa>k|K|L|lac|lakhs?|LPA|lpa|cr|crores?|M|million)?"
    r"\s?(?:-|–|—|to)\s?"
    r"(?P<cur2>C\$|A\$|S\$|\$|£|€|₹|USD|GBP|EUR|INR|CAD|AUD|SGD|Rs\.?)?\s?(?P<b>\d[\d,]*(?:\.\d+)?)\s?(?P<sb>k|K|L|lac|lakhs?|LPA|lpa|cr|crores?|M|million)?"
    r"(?P<tail>[^.\n]{0,40})")


def posting_range(text):
    """(low, high, currency) of the first plausible annual pay range in a posting, or None."""
    for m in RANGE.finditer(text or ""):
        cur = m.group("cur") or m.group("cur2")
        sa, sb = m.group("sa") or m.group("sb"), m.group("sb") or m.group("sa")
        if not cur and not (sa or sb):
            continue
        code = SYMBOL.get(cur, (cur or "").upper().rstrip(".").replace("RS", "INR")) or ("INR" if (sa or "").lower() in ("l", "lpa", "lac", "lakh", "lakhs") else "")
        low, high = _amount(m.group("a"), sa), _amount(m.group("b"), sb)
        tail = m.group("tail").lower()
        if "hour" in tail or "/hr" in tail:
            low, high = low * 2080, high * 2080
        elif "month" in tail:
            low, high = low * 12, high * 12
        if code and 1000 < low <= high < 1e9 and high <= low * 4:
            return low, high, code
    return None


def _nice(v, currency):
    step = 50_000 if currency == "INR" else 1_000
    return int(round(v / step) * step)


def estimate(job, profile, llm=None, cache=None):
    """{"currency", "amount" (annual), "basis"} for this job, or None."""
    cache = cache if cache is not None else {}
    key = f"{job.company}|{job.title}|{job.location}"
    if key in cache:
        return cache[key]
    cfg = profile.get("salary") or {}
    premium = float(cfg.get("premium", 0.08))
    found = posting_range(job.description)
    result = None
    if found:
        low, high, cur = found
        result = {"currency": cur, "market": (low + high) / 2, "basis": "midpoint of the range in the posting"}
    elif llm is not None and getattr(llm, "enabled", False):
        years = (profile.get("answers") or {})
        exp = next((v for k, v in years.items() if "years of" in k), "")
        reply = llm.ask(SYSTEM, f"ROLE: {job.title}\nCOMPANY: {job.company}\nLOCATION: {job.location}\n"
                                f"CANDIDATE EXPERIENCE: {exp} years\nPOSTING (excerpt):\n{job.description[:2500]}", as_json=True)
        try:
            med, cur = float(reply["median"]), str(reply["currency"]).upper()[:3]
            if med > 1000 and cur.isalpha():
                result = {"currency": cur, "market": med, "basis": str(reply.get("basis", ""))[:160]}
        except (TypeError, KeyError, ValueError):
            pass
    if result:
        amount = result["market"] * (1 + premium)
        cur_lpa = cfg.get("current_lpa")
        if result["currency"] == "INR" and cur_lpa:
            amount = max(amount, float(cur_lpa) * LAKH * 1.2)
        result["amount"] = _nice(amount, result["currency"])
        detail(f"expected salary for {job.company}: {result['currency']} {result['amount']:,} ({result['basis']})")
    cache[key] = result
    return result


def answers_for(est, label, kind):
    """Candidate answers in the unit the question asks for, most specific first."""
    if not est:
        return []
    amt, cur = est["amount"], est["currency"]
    l = (label or "").lower()
    monthly = "month" in l
    value = amt / 12 if monthly else amt
    lpa = value / LAKH
    if cur == "INR":
        lpa_s = f"{lpa:.1f}".rstrip("0").rstrip(".")
        if re.search(r"lpa|lakh|lac", l):
            return [lpa_s] if kind == "number" else [f"{lpa_s} LPA", lpa_s]
        if kind == "number" or re.search(r"\binr\b|₹|rupees|per annum|annual", l):
            return [str(int(value)), lpa_s]
        return [f"{lpa_s} LPA", f"INR {int(value):,}", lpa_s, str(int(value))]
    sym = {"USD": "$", "GBP": "£", "EUR": "€"}.get(cur, cur + " ")
    if kind == "number":
        return [str(int(value / 1000)) if re.search(r"\(k\)|in thousands|\bk\b", l) else str(int(value))]
    return [f"{sym}{int(value):,}" + (" per month" if monthly else " per year"), str(int(value))]
