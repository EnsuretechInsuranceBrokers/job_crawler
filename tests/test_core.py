import json
import os

import pytest

from jobbot import sources
from jobbot.apply import apply_url, pick_option, rule_answer
from jobbot.match import score_job, select
from jobbot.models import Job
from jobbot.state import State
from jobbot.tailor import tailor


class FakeLLM:
    enabled = True

    def __init__(self, reply):
        self.reply = reply

    def ask(self, system, user, as_json=False):
        return self.reply


def test_score_requires_title_and_location(profile, job):
    assert score_job(job, profile) > 0.8
    assert score_job(Job("x", "1", "A", "Engineering Manager, Backend", "Remote", ""), profile) == 0
    assert score_job(Job("x", "1", "A", "Account Executive", "Remote", ""), profile) == 0
    berlin = Job("x", "1", "A", "Software Engineer", "Berlin, Germany", "", "python")
    assert score_job(berlin, profile) == 0
    berlin.remote = True
    assert score_job(berlin, profile) > 0


def test_select_dedupes_same_role_and_skips_seen(profile, job):
    cfg = {"match": {"min_score": 0.55, "max_age_days": 0}}
    twin = Job("greenhouse", "124", "Initech", "Senior Backend Engineer", "Austin", "", job.description, board="initech")
    assert len(select([job, twin], profile, cfg, lambda j: False)) == 1
    assert select([job, twin], profile, cfg, lambda j: True) == []


def test_keyword_tailoring_only_reorders(profile, job):
    out, used = tailor(profile, job, None)
    assert not used
    assert out["skills"][:3] == ["Python", "PostgreSQL", "Kubernetes"]
    assert set(out["skills"]) == set(profile["skills"])
    original = {b for e in profile["experience"] for b in e["bullets"]}
    assert all(b in original for e in out["experience"] for b in e["bullets"])
    assert "Kafka" in out["experience"][0]["bullets"][0]
    assert len(out["experience"][0]["bullets"]) == 5


def test_llm_cannot_fabricate(profile, job):
    reply = {"skills": ["Rust", "Elixir", "Python", "Kafka", "Go", "AWS", "Docker", "Redis"],
             "bullets": {"0": [2, 0, 99], "1": ["x"]},
             "summary": "Backend engineer with 12 years of Rust and Elixir experience."}
    out, used = tailor(profile, job, FakeLLM(reply))
    assert used
    assert "Rust" not in out["skills"] and out["skills"][0] == "Python"
    assert out["experience"][0]["bullets"] == [profile["experience"][0]["bullets"][2], profile["experience"][0]["bullets"][0]]
    assert out["summary"] == profile["summary"]  # fabricated summary rejected
    good = dict(reply, summary="Backend engineer with 6 years of experience building Python and Kafka services on AWS.")
    out, _ = tailor(profile, job, FakeLLM(good))
    assert out["summary"] == good["summary"]


def test_state_roundtrip_is_encrypted(tmp_path, job):
    path = str(tmp_path / "s.enc")
    s = State(path, "secret")
    assert not s.seen(job)
    s.record(job, "submitted")
    s.save()
    assert b"Initech" not in open(path, "rb").read()
    assert State(path, "secret").seen(job)
    with pytest.raises(SystemExit):
        State(path, "other")


def test_state_retries_errors_once(tmp_path, job):
    s = State(str(tmp_path / "s.enc"), "k")
    s.record(job, "error")
    assert not s.seen(job)
    s.record(job, "error")
    assert s.seen(job)


def test_rule_answers(profile):
    assert rule_answer("First Name", profile) == "Jane"
    assert rule_answer("Last Name", profile) == "Doe"
    assert rule_answer("Full name", profile) == "Jane Doe"
    assert rule_answer("Current company", profile) == "Acme Payments"
    assert rule_answer("Are you legally authorized to work in the country where this job is located?", profile) == "Yes"
    assert rule_answer("Will you require visa sponsorship?", profile) == "No"
    assert rule_answer("What is your favourite colour?", profile) is None


def test_pick_option():
    yn = [{"text": "Yes, I am authorized"}, {"text": "No, I am not"}]
    assert pick_option("Yes", yn) == 0 and pick_option("No", yn) == 1
    eeo = [{"text": "Male"}, {"text": "Female"}, {"text": "I don't wish to answer"}]
    assert pick_option("decline", eeo) == 2
    assert pick_option("Maybe", yn) is None
    assert pick_option("engineer", [{"text": "Software engineer"}, {"text": "Data engineer"}]) is None


def test_apply_url():
    assert "for=acme&token=42" in apply_url(Job("greenhouse", "42", "Acme", "t", "", "https://acme.com/careers?gh_jid=42", board="acme"))
    lever = Job("lever", "x", "a", "t", "", "https://jobs.lever.co/acme/0a1b2c3d-0000-4000-8000-000000000000")
    assert apply_url(lever).endswith("/apply")
    agg = Job("remotive", "1", "a", "t", "", "https://job-boards.greenhouse.io/acme/jobs/777?src=x")
    assert "for=acme&token=777" in apply_url(agg)
    assert apply_url(Job("remotive", "1", "a", "t", "", "https://remotive.com/remote-jobs/x")) is None


def test_source_parsers(monkeypatch):
    payloads = {
        "greenhouse": {"jobs": [{"id": 1, "title": "Dev ", "location": {"name": "Remote"}, "absolute_url": "u",
                                 "updated_at": "2026-01-02T03:04:05-05:00", "content": "&lt;p&gt;Use &lt;b&gt;Go&lt;/b&gt;&lt;/p&gt;"}]},
        "lever": [{"id": "a", "text": "Dev", "categories": {"location": "NYC"}, "hostedUrl": "u", "createdAt": 1767000000000,
                   "descriptionPlain": "d", "lists": [{"text": "Reqs", "content": "<li>Python</li>"}]}],
        "ashby": {"jobs": [{"id": "b", "title": "Dev", "location": "SF", "isRemote": True, "jobUrl": "u",
                            "publishedAt": "2026-01-02T03:04:05.000+00:00", "descriptionPlain": "d"},
                           {"id": "c", "title": "Hidden", "isListed": False}]},
        "remoteok": [{"legal": "x"}, {"id": 5, "position": "Dev", "company": "C", "url": "u", "epoch": 1767000000}],
    }
    for name, payload in payloads.items():
        monkeypatch.setattr(sources, "_get", lambda url, p=payload: p)
        fn = {**sources.ATS, **sources.AGGREGATORS}[name]
        jobs = list(fn("board"))
        assert len(jobs) == 1 and jobs[0].title == "Dev" and jobs[0].posted > 1.7e9, name
    monkeypatch.setattr(sources, "_get", lambda url: payloads["greenhouse"])
    assert list(sources.greenhouse("b"))[0].description == "Use Go"


def test_crawl_survives_bad_source(monkeypatch):
    def boom(url):
        raise ValueError("bad json")
    monkeypatch.setattr(sources, "_get", boom)
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    assert sources.crawl({"greenhouse": ["x"], "remotive": True}) == []


def test_cover_letter_template_and_llm_guard(profile, job):
    from jobbot.cover import cover_letter
    tailored, _ = tailor(profile, job, None)
    text, by_llm = cover_letter(profile, tailored, job, None)
    assert not by_llm and text.startswith("Dear Initech hiring team,") and text.endswith("Sincerely,\nJane Doe")
    assert "Senior Backend Engineer" in text and "Kafka" in text
    job.named = False
    assert cover_letter(profile, tailored, job, None)[0].startswith("Dear hiring team,")
    job.named = True
    body = ("I have spent six years building Python services on AWS, including a payment reconciliation pipeline "
            "with Kafka that cut settlement delays dramatically. " * 3)
    honest = f"Dear Initech hiring team,\n\n{body}\n\nThe Senior Backend Engineer role fits that work.\n\nSincerely,\nJane Doe"
    assert cover_letter(profile, tailored, job, FakeLLM(honest)) == (honest, True)
    lying = honest.replace("Python services", "Rust and Elixir services")
    assert cover_letter(profile, tailored, job, FakeLLM(lying))[1] is False
    assert cover_letter(profile, tailored, job, FakeLLM(honest.replace("six years", "15 years")))[1] is False


def test_whatsapp_log_and_alert(monkeypatch, profile, job):
    from jobbot import notify
    from jobbot.models import Outcome
    sent = []

    class Resp:
        status_code = 200

    monkeypatch.setattr(notify.requests, "get", lambda url, timeout, params: sent.append(params) or Resp())
    assert notify.daily_log(profile, [], False, 0) is False  # not configured -> silent no-op
    profile["whatsapp"] = {"phone": "+1 555 010 0199", "apikey": "k"}
    assert notify.alert(profile, job, Outcome("captcha"))
    assert sent[0]["phone"] == "+15550100199" and "CAPTCHA" in sent[0]["text"] and job.url in sent[0]["text"]
    assert not notify.alert(profile, job, Outcome("submitted"))
    results = [(job, Outcome("submitted"), None, True), (job, Outcome("needs_answers", unanswered=["Shoe size?"]), None, False)]
    sent.clear()
    assert notify.daily_log(profile, results, False, 1)
    text = "\n".join(p["text"] for p in sent)
    assert "Submitted: 1 | Need you: 1" in text and "Shoe size?" in text and job.url in text
    sent.clear()
    many = [(job, Outcome("manual"), None, False)] * 80
    notify.daily_log(profile, many, True, 0)
    assert len(sent) == 4 and all(len(p["text"]) < 1000 for p in sent) and "DRY RUN" in sent[0]["text"]
