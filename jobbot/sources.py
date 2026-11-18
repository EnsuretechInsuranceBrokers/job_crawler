"""Job sources. Only public, documented JSON endpoints - no HTML scraping."""
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

from .models import Job
from .util import detail, log, strip_html

UA = {"User-Agent": "jobbot/1.0 (personal job-search automation)"}
TIMEOUT = 40


def _get(url):
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _ts(value):
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _pretty(board):
    return board.replace("-", " ").replace("_", " ").title()


def _is_remote(*texts):
    return any("remote" in (t or "").lower() for t in texts)


def greenhouse(board):
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true")
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        yield Job("greenhouse", str(j["id"]), j.get("company_name") or _pretty(board), j["title"].strip(), loc,
                  j.get("absolute_url", ""), strip_html(j.get("content", "")), _is_remote(loc, j["title"]),
                  _ts(j.get("first_published") or j.get("updated_at")), board, named=bool(j.get("company_name")))


def lever(board):
    for j in _get(f"https://api.lever.co/v0/postings/{board}?mode=json"):
        cats = j.get("categories") or {}
        loc = cats.get("location", "") or ""
        parts = [j.get("descriptionPlain", "")]
        parts += [f"{x.get('text', '')}\n{strip_html(x.get('content', ''))}" for x in j.get("lists", [])]
        parts.append(j.get("additionalPlain", ""))
        yield Job("lever", j["id"], _pretty(board), j["text"].strip(), loc, j.get("hostedUrl", ""), "\n".join(parts),
                  _is_remote(loc, j.get("workplaceType")), _ts(j.get("createdAt")), board, named=False)


def ashby(board):
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{board}")
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        loc = j.get("location", "") or ""
        yield Job("ashby", j["id"], _pretty(board), j["title"].strip(), loc, j.get("jobUrl", ""),
                  j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")),
                  bool(j.get("isRemote")) or _is_remote(loc), _ts(j.get("publishedAt")), board, named=False)


def remotive(_=None):
    for j in _get("https://remotive.com/api/remote-jobs").get("jobs", []):
        yield Job("remotive", str(j["id"]), j.get("company_name", ""), j["title"].strip(),
                  j.get("candidate_required_location", "") or "Remote", j["url"],
                  strip_html(j.get("description", "")), True, _ts(j.get("publication_date")))


def remoteok(_=None):
    for j in _get("https://remoteok.com/api"):
        if not j.get("position"):
            continue  # first element is a legal notice
        yield Job("remoteok", str(j["id"]), j.get("company", ""), j["position"].strip(),
                  j.get("location", "") or "Remote", j.get("url", ""), strip_html(j.get("description", "")),
                  True, _ts(j.get("epoch") or j.get("date")))


def arbeitnow(_=None):
    for j in _get("https://www.arbeitnow.com/api/job-board-api").get("data", []):
        yield Job("arbeitnow", j["slug"], j.get("company_name", ""), j["title"].strip(), j.get("location", ""),
                  j["url"], strip_html(j.get("description", "")), bool(j.get("remote")), _ts(j.get("created_at")))


INDIA_OR_REMOTE = ("in",)


def smartrecruiters(company):
    """SmartRecruiters company board (ServiceNow, Bosch, Swiggy, Freshworks ...). The postings API is public;
    the application pages are behind bot protection (DataDome), so these become "apply yourself" packets.
    Details are fetched only for postings in India or marked remote, to keep the number of calls small."""
    offset, seen = 0, 0
    while offset < 1000:
        data = _get(f"https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100&offset={offset}")
        rows = data.get("content", [])
        for j in rows:
            loc = j.get("location") or {}
            if loc.get("country") not in INDIA_OR_REMOTE and not (loc.get("remote") and not loc.get("country")):
                continue
            seen += 1
            if seen > 80:
                return
            try:
                det = _get(j["ref"])
            except Exception:  # noqa: BLE001
                continue
            sections = ((det.get("jobAd") or {}).get("sections") or {})
            text = "\n".join(t for t in (strip_html((sections.get(k) or {}).get("text", ""))
                                          for k in ("jobDescription", "qualifications", "additionalInformation")) if t)
            yield Job("smartrecruiters", str(j["id"]), (det.get("company") or {}).get("name") or _pretty(company), j["name"].strip(),
                      loc.get("fullLocation") or loc.get("city") or "", det.get("applyUrl") or det.get("postingUrl", ""), text,
                      bool(loc.get("remote")), _ts(j.get("releasedDate")), company)
        offset += 100
        if offset >= int(data.get("totalFound", 0)):
            return


def workday(spec):
    """Workday career site, as "tenant/wdN/site" (e.g. "salesforce/wd12/External_Career_Site").
    Applying on Workday needs an account, so these jobs become "apply yourself" packets."""
    tenant, wd, site = spec.split("/")
    base = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    headers = {**UA, "Accept": "application/json", "Content-Type": "application/json"}
    seen = set()
    for query in BIG_QUERIES:
        for offset in range(0, 100, 20):
            r = requests.post(f"{base}/jobs", json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": f"{query} India"},
                              headers=headers, timeout=TIMEOUT)
            r.raise_for_status()
            rows = r.json().get("jobPostings", [])
            for j in rows:
                path = j.get("externalPath", "")
                if path in seen or "india" not in (j.get("locationsText", "") + path).lower() and "remote" not in j.get("locationsText", "").lower():
                    continue
                seen.add(path)
                try:
                    info = requests.get(base + path, headers=headers, timeout=TIMEOUT).json().get("jobPostingInfo", {})
                except Exception:  # noqa: BLE001
                    info = {}
                yield Job("workday", path.rsplit("_", 1)[-1] or path, _pretty(tenant), j.get("title", "").strip(),
                          info.get("location") or j.get("locationsText", ""), info.get("externalUrl") or f"https://{tenant}.{wd}.myworkdayjobs.com/{site}{path}",
                          strip_html(info.get("jobDescription", "")), "remote" in j.get("locationsText", "").lower(),
                          _ts(info.get("startDate")), tenant)
            if len(rows) < 20:
                break


def amazon(_=None):
    """amazon.jobs, India. Applying needs an Amazon login, so these become "apply yourself" packets."""
    seen = set()
    for query in ("software development engineer",) + BIG_QUERIES:
        for offset in (0, 100):
            data = _get(f"https://www.amazon.jobs/en/search.json?base_query={requests.utils.quote(query)}"
                        f"&normalized_country_code%5B%5D=IND&result_limit=100&offset={offset}")
            for j in data.get("jobs", []):
                if j["id_icims"] in seen:
                    continue
                seen.add(j["id_icims"])
                text = "\n".join(strip_html(j.get(k, "")) for k in ("description", "basic_qualifications", "preferred_qualifications"))
                posted = 0.0
                try:
                    posted = datetime.strptime(j.get("posted_date", ""), "%B %d, %Y").timestamp()
                except ValueError:
                    pass
                yield Job("amazon", str(j["id_icims"]), j.get("company_name") or "Amazon", j["title"].strip(), j.get("normalized_location", ""),
                          "https://www.amazon.jobs" + j.get("job_path", ""), text, False, posted, "amazon")
            if len(data.get("jobs", [])) < 100:
                break


BIG_QUERIES = ("software engineer", "software developer", "data engineer", "data scientist", "machine learning",
               "site reliability", "devops", "cloud engineer", "security engineer", "network engineer", "qa engineer",
               "support engineer", "solutions engineer", "data analyst", "systems engineer", "firmware", "frontend", "backend")
BROWSER = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) jobbot/1.0", "Accept": "application/json, text/html"}


def microsoft(_=None):
    """careers.microsoft.com (India). Applying needs a Microsoft account: apply-yourself packets."""
    base = "https://apply.careers.microsoft.com/api/pcsx"
    seen = set()
    for query in BIG_QUERIES:
        if len(seen) >= 150:
            return
        for start in range(0, 60, 20):
            time.sleep(0.5)  # the API rate-limits bursts
            r = requests.get(f"{base}/search", params={"domain": "microsoft.com", "query": query, "location": "India",
                                                       "start": start, "num": 20}, headers=BROWSER, timeout=TIMEOUT)
            r.raise_for_status()
            rows = (r.json().get("data") or {}).get("positions", [])
            for p in rows:
                if p["id"] in seen:
                    continue
                seen.add(p["id"])
                desc = ""
                try:
                    time.sleep(0.3)
                    d = requests.get(f"{base}/position_details", params={"position_id": p["id"], "domain": "microsoft.com", "hl": "en"},
                                     headers=BROWSER, timeout=TIMEOUT).json().get("data", {})
                    desc = strip_html(d.get("jobDescription", ""))
                except Exception:  # noqa: BLE001
                    pass
                yield Job("microsoft", str(p["id"]), "Microsoft", p["name"].strip(), "; ".join(p.get("locations") or []),
                          "https://apply.careers.microsoft.com" + p.get("positionUrl", ""), desc,
                          "remote" in str(p.get("workLocationOption", "")).lower(), float(p.get("postedTs") or 0), "microsoft")
            if len(rows) < 20:
                break


def google(_=None):
    """google.com/about/careers (India). Applying needs a Google account: apply-yourself packets.
    There is no public API, so the search results page is read; each card carries title, place and the
    minimum qualifications, which serve as the description."""
    base = "https://www.google.com/about/careers/applications/jobs/results"
    seen = set()
    for query in BIG_QUERIES[:10]:
        for page in (1, 2, 3):
            r = requests.get(base, params={"location": "India", "q": query, "page": page}, headers=BROWSER, timeout=TIMEOUT)
            r.raise_for_status()
            text = r.text
            marks = list(re.finditer(r'jobs/results/(\d{6,})-([a-z0-9-]+)', text))
            new = 0
            for i, m in enumerate(marks):
                jid = m.group(1)
                if jid in seen:
                    continue
                seen.add(jid)
                new += 1
                chunk = text[m.start(): marks[i + 1].start() if i + 1 < len(marks) else m.start() + 6000]
                plain = strip_html(chunk)
                title = re.search(r"aria-label=\"([^\"]{3,120})\"", chunk)
                head = plain.split("corporate_fare")[0].strip().split("\n")[-1].strip()
                place = re.search(r"place\s+(.+?)(?:\s{2,}|\n|$)", plain)
                yield Job("google", jid, "Google", (head or (title.group(1) if title else m.group(2).replace("-", " "))).strip()[:150],
                          place.group(1).strip() if place else "India", f"{base}/{jid}-{m.group(2)}", plain[:4000], False, 0.0, "google")
            if not new:
                break


def apple(_=None):
    """jobs.apple.com (India). Applying needs an Apple ID: apply-yourself packets."""
    seen = set()
    for query in ("software", "engineer", "machine learning"):
        for page in (1, 2):
            r = requests.get("https://jobs.apple.com/en-in/search", params={"location": "india-INDC", "search": query, "page": page},
                             headers=BROWSER, timeout=20)
            r.raise_for_status()
            found = re.findall(r'aria-label="([^"]+?) (\d{6,})" href="(/en-in/details/\d+/[^"?]+)[^"]*"', r.text)
            new = 0
            for title, jid, path in found:
                if jid in seen:
                    continue
                seen.add(jid)
                new += 1
                desc = title
                try:  # the details page carries the description and minimum qualifications
                    desc = strip_html(requests.get("https://jobs.apple.com" + path, headers=BROWSER, timeout=20).text)[:6000]
                except Exception:  # noqa: BLE001
                    pass
                yield Job("apple", jid, "Apple", title.strip(), "India", "https://jobs.apple.com" + path, desc, False, 0.0, "apple")
            if not new:
                break


ATS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters, "workday": workday}
AGGREGATORS = {"remotive": remotive, "remoteok": remoteok, "arbeitnow": arbeitnow, "amazon": amazon,
               "microsoft": microsoft, "google": google, "apple": apple}


def crawl(sources_cfg):
    """Fetch every configured source concurrently. A failing source is skipped."""
    tasks = []
    for name, fn in ATS.items():
        tasks += [(name, fn, board) for board in sources_cfg.get(name) or []]
    tasks += [(name, fn, None) for name, fn in AGGREGATORS.items() if sources_cfg.get(name)]

    def run(task):
        name, fn, arg = task
        for attempt in (1, 2):
            try:
                return list(fn(arg))
            except Exception as e:  # noqa: BLE001 - one bad board must not kill the run
                err = f"{name}/{arg or ''}: {type(e).__name__} {getattr(getattr(e, 'response', None), 'status_code', '')}"
                if attempt == 1 and not isinstance(e, requests.HTTPError):
                    time.sleep(3)
        log(f"source skipped - {err}")
        return []

    jobs = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for task, result in zip(tasks, pool.map(run, tasks)):
            detail(f"{task[0]}/{task[2] or ''}: {len(result)} jobs")
            jobs += result
    log(f"crawled {len(tasks)} sources, {len(jobs)} postings")
    return jobs
