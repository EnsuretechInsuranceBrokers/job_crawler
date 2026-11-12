import re
import time

from .util import has_term, norm


def _phrase_in(phrase, title_norm):
    return all(has_term(w, title_norm) for w in norm(phrase).split())


PLACES = (r"(?:the )?(?:u\.?s\.?a?\.?|united states|america|canada|u\.?k\.?|united kingdom|europe|e\.?u\.?|emea|latam|latin america|"
          r"north america|australia|germany|france|brazil|mexico|ireland|netherlands|poland|spain|portugal|israel|singapore|japan)")
RESTRICTED = re.compile(
    r"(?:must|need to|required to|have to|should)\s+(?:be\s+)?(?:currently\s+)?(?:based|located|resid\w+|live|living)\s+in\s+(" + PLACES + r")\b"
    r"|(?:only|exclusively)\s+(?:open\s+to|for|hiring|considering)\s+(?:candidates|applicants|people|residents)\s+(?:based\s+|located\s+|residing\s+)?in\s+(" + PLACES + r")\b"
    r"|\b(" + PLACES + r")[- ](?:based|only)\s+(?:candidates|applicants|role|position)"
    r"|(?:candidates|applicants)\s+(?:must\s+be\s+)?(?:based|located|residing)\s+in\s+(" + PLACES + r")\s+only"
    r"|(?:authori[sz]ed|eligible|right)\s+to\s+work\s+in\s+(" + PLACES + r")\s+without\s+(?:visa\s+)?sponsorship", re.I)


def restricted_to(description):
    """Place a posting says candidates must be in ('United States'), or None."""
    for m in RESTRICTED.finditer(description or ""):
        place = next((g for g in m.groups() if g), None)
        # "us", "eu" and "uk" only count when written as abbreviations, not as the word "us"
        if place and (len(place.replace(".", "").replace("the ", "").strip()) > 3 or place.replace("the ", "").strip().isupper()
                      or "." in place):
            return place
    return None


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
            if ok and regions:
                # The location line may say just "Remote" while the text says "must be based in the US".
                place = restricted_to(job.description)
                if place and not any(norm(r) in norm(place) or norm(place) in norm(r) for r in regions):
                    ok = False
        if not ok:
            return 0.0
    limit = search.get("max_required_years")
    if limit is not None:
        from .insights import required_years
        need = required_years(job.description)
        if need is not None and need > float(limit):
            return 0.0  # asks for clearly more experience than the profile has
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
    # Best match first; among equals prefer jobs the bot can submit itself, then the newest.
    from .apply import apply_url
    out.sort(key=lambda j: (-j.score, apply_url(j) is None, -j.posted))
    unique = []
    for job in out:
        if role_key(job) not in roles:
            roles.add(role_key(job))
            unique.append(job)
    return unique
