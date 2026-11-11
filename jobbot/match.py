import re
import time

from .util import has_term, norm


def _phrase_in(phrase, title_norm):
    return all(has_term(w, title_norm) for w in norm(phrase).split())


def score_job(job, profile, max_age_days=0):
    """0 = reject. Otherwise 0.5 for a title match plus up to 0.5 for skill overlap."""
    search = profile["search"]
    title = norm(job.title)
    if not any(_phrase_in(p, title) for p in search["titles"]):
        return 0.0
    if any(_phrase_in(p, title) for p in search.get("exclude_titles") or []):
        return 0.0
    if max_age_days and job.posted and time.time() - job.posted > max_age_days * 86400:
        return 0.0
    locations = search.get("locations") or []
    if locations:
        loc = norm(job.location)
        ok = any(norm(l) in loc for l in locations)
        if not ok and job.remote and search.get("remote_ok", True):
            # "Remote - US" is not open to someone elsewhere: with remote_regions set, a remote job
            # must name one of those regions (or name no place at all).
            regions = search.get("remote_regions") or []
            bare = not re.sub(r"\b(remote|anywhere|worldwide|global|fully|work from home|wfh|only)\b", "", loc).strip()
            ok = not regions or bare or any(norm(r) in loc for r in regions)
        if not ok:
            return 0.0
    text = norm(job.title + " " + job.description)
    terms = list(dict.fromkeys(profile["skills"] + (search.get("keywords") or [])))
    hits = sum(1 for t in terms if has_term(t, text))
    return round(0.5 + 0.5 * min(1.0, hits / 6), 3)


def role_key(job):
    """Same role posted in several cities should only be applied to once."""
    company = re.sub(r"\b(inc|llc|ltd|pvt|gmbh|corp|co)\b", "", norm(job.company))
    company = re.sub(r"[^a-z0-9]", "", company)  # "Scale AI" (aggregator) == "Scaleai" (board token)
    title = re.sub(r"\(.*?\)", "", job.title).split(" - ")[0]  # drop "(Remote)" / " - Berlin" suffixes
    return company + "|" + norm(title)


def select(jobs, profile, cfg, seen):
    """Score, drop already-handled jobs, sort best first, de-duplicate roles."""
    out, roles = [], set()
    for job in jobs:
        if seen(job):
            continue
        job.score = score_job(job, profile, cfg["match"].get("max_age_days", 0))
        if job.score >= cfg["match"]["min_score"]:
            out.append(job)
    out.sort(key=lambda j: (-j.score, -j.posted))
    unique = []
    for job in out:
        if role_key(job) not in roles:
            roles.add(role_key(job))
            unique.append(job)
    return unique
