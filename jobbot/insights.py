"""What the market asks for versus what the profile says: keyword coverage and skills gap."""
import re
from collections import Counter

from .util import has_term, norm

# Terms employers list as requirements. Lower-case; matched as whole terms.
# Single letters and everyday words ("c", "r", "go") are left out: they match ordinary prose.
TERMS = sorted(set("""
python|java|javascript|typescript|golang|rust|c++|c#|kotlin|swift|scala|ruby|php|elixir|dart|matlab|sql|nosql|bash
react|angular|vue|next.js|node.js|express|django|flask|fastapi|spring|spring boot|laravel|rails|.net|asp.net|graphql|rest|grpc
aws|azure|gcp|kubernetes|docker|terraform|ansible|helm|jenkins|github actions|gitlab ci|ci/cd|linux|unix|nginx
postgresql|mysql|mongodb|redis|elasticsearch|cassandra|dynamodb|snowflake|bigquery|redshift|clickhouse|oracle|sqlite
kafka|rabbitmq|spark|hadoop|airflow|flink|dbt|etl|databricks|tableau|power bi|looker
machine learning|deep learning|pytorch|tensorflow|scikit-learn|nlp|llm|generative ai|computer vision|mlops|langchain|rag
microservices|distributed systems|system design|data structures|algorithms|multithreading|concurrency|networking|tcp/ip
prometheus|grafana|datadog|splunk|opentelemetry|observability
selenium|playwright|cypress|junit|pytest|jest|test automation
oauth|jwt|sso|cryptography|penetration testing|siem
android|ios|flutter|react native|agile|scrum|jira|git
""".replace("\n", "|").strip("|").split("|")))


def asked_terms(job):
    text = norm(job.title + " " + job.description)
    return [t for t in TERMS if has_term(t, text)]


def coverage(job, profile_norm):
    """(terms the posting asks for that the profile has, terms it lacks)."""
    asked = asked_terms(job)
    have = [t for t in asked if has_term(t, profile_norm)]
    return have, [t for t in asked if t not in have]


def skills_gap(jobs, profile, top=15):
    """Across matching jobs: which requested terms the profile never mentions, most requested first."""
    import json
    profile_norm = norm(json.dumps({k: profile.get(k) for k in ("summary", "skills", "experience", "projects", "education")}, default=str))
    missing, wanted = Counter(), Counter()
    for job in jobs:
        have, lack = coverage(job, profile_norm)
        wanted.update(have + lack)
        missing.update(lack)
    return {"jobs": len(jobs), "missing": missing.most_common(top), "wanted": wanted.most_common(top), "profile_norm": profile_norm}


YEARS = re.compile(r"(\d{1,2})\s*(?:\+|plus)?\s*(?:-|to|–)?\s*(?:\d{1,2}\s*)?\+?\s*(?:years|yrs)(?:'|’)?\s*(?:of\s+)?"
                   r"(?:[a-z-]+\s+){0,4}?(?:experience|exp\b)", re.I)


def required_years(description):
    """Smallest 'N years of experience' figure in the posting, or None."""
    found = [int(m.group(1)) for m in YEARS.finditer(description or "") if int(m.group(1)) < 30]
    return min(found) if found else None
