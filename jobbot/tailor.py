"""Tailor the resume to one job WITHOUT inventing anything.

The model (or the keyword fallback) may only choose and reorder skills and
bullets that already exist in the profile. The single piece of free text is the
summary, and it is rejected if it mentions a job keyword the profile lacks.
"""
import copy
import json
import re

from .util import has_term, norm

MAX_BULLETS = 5

SYSTEM = """You tailor a resume to a job posting. You must never invent or alter facts.
Return JSON with exactly these keys:
 "skills": the candidate's skills reordered most relevant first (only skills from the given list, same spelling; drop clearly irrelevant ones, keep at least 8 if available)
 "bullets": object mapping each experience index (string) to a list of that experience's bullet indices, most relevant first, at most 5
 "summary": 2 sentences, first person implied, built ONLY from facts in the candidate summary, skills and bullets, emphasising what matters for this job. Do not name the hiring company. Do not claim skills, titles, or years that are not in the profile."""


def _overlap(text, job_norm, terms):
    return sum(1 for t in terms if has_term(t, job_norm) and has_term(t, norm(text)))


def _keyword_plan(profile, job_norm):
    terms = profile["skills"] + (profile["search"].get("keywords") or [])
    skills = sorted(profile["skills"], key=lambda s: not has_term(s, job_norm))  # stable: matches first
    bullets = {}
    for i, exp in enumerate(profile["experience"]):
        bl = exp.get("bullets") or []
        ranked = sorted(range(len(bl)), key=lambda k: (-_overlap(bl[k], job_norm, terms), k))
        bullets[str(i)] = ranked[:MAX_BULLETS]
    return {"skills": skills, "bullets": bullets, "summary": profile.get("summary", "")}


NEUTRAL = {"i", "dear", "sincerely", "hiring", "team", "manager", "regards", "best", "thank", "yours"}


def grounded(text, profile, job, max_len=600, letter=False):
    """True if `text` introduces no job-posting term, company or number that the profile lacks.

    Flags technical-looking words (mid-sentence capitals, digits, + # .) that occur in the
    posting but nowhere in the profile - the signature of a claimed skill the person lacks.
    """
    if not text or len(text) > max_len:
        return False
    profile_norm = norm(json.dumps(profile, default=str))
    allowed = set(norm(job.title).split()) | NEUTRAL
    if letter and job.named:
        allowed |= set(norm(job.company).split())
    elif has_term(job.company, norm(text)) and not has_term(job.company, profile_norm):
        return False
    job_norm = norm(job.title + " " + job.description)
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9+#.]*[A-Za-z0-9+#]", text):
        word = m.group()
        before = text[:m.start()].rstrip()
        starts_sentence = not before or before[-1] in ".!?:\n,"
        techy = (word[0].isupper() and not starts_sentence) or any(c in word for c in "+#.") or any(c.isdigit() for c in word)
        if techy and norm(word) not in allowed and not has_term(word, profile_norm) and has_term(word, job_norm):
            return False
    return all(number in profile_norm for number in re.findall(r"\d+", text))


def tailor(profile, job, llm=None):
    """Returns (tailored_profile, used_llm)."""
    job_norm = norm(job.title + " " + job.description)
    plan = _keyword_plan(profile, job_norm)
    used_llm = False
    if llm is not None and llm.enabled:
        slim = {"summary": profile.get("summary", ""), "skills": profile["skills"],
                "experience": [{"index": i, "title": e.get("title"),
                                "bullets": dict(enumerate(e.get("bullets") or []))}
                               for i, e in enumerate(profile["experience"])]}
        reply = llm.ask(SYSTEM, f"JOB TITLE: {job.title}\nJOB POSTING:\n{job.description[:6000]}\n\n"
                                f"CANDIDATE:\n{json.dumps(slim)}", as_json=True)
        if isinstance(reply, dict):
            used_llm = _merge(plan, reply, profile, job)

    out = copy.deepcopy(profile)
    out["skills"] = plan["skills"]
    out["summary"] = plan["summary"]
    for i, exp in enumerate(out["experience"]):
        bl = exp.get("bullets") or []
        exp["bullets"] = [bl[k] for k in plan["bullets"][str(i)]]
    return out, used_llm


def _merge(plan, reply, profile, job):
    """Copy only the valid parts of the model's reply into the plan."""
    used = False
    known = {s.lower(): s for s in profile["skills"]}
    skills = list(dict.fromkeys(known[s.lower()] for s in reply.get("skills") or []
                                if isinstance(s, str) and s.lower() in known))
    if len(skills) >= min(5, len(known)):
        plan["skills"], used = skills, True
    for i, exp in enumerate(profile["experience"]):
        n = len(exp.get("bullets") or [])
        try:
            ids = list(dict.fromkeys(int(k) for k in (reply.get("bullets") or {}).get(str(i), [])))
        except (TypeError, ValueError):
            continue
        ids = [k for k in ids if 0 <= k < n][:MAX_BULLETS]
        if len(ids) >= min(2, n):
            plan["bullets"][str(i)], used = ids, True
    summary = reply.get("summary")
    if isinstance(summary, str) and grounded(summary.strip(), profile, job):
        plan["summary"], used = summary.strip(), True
    return used
