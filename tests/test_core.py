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


def test_location_dependent_answers(profile, job):
    profile["answers"] = {"authori[sz]ed to work": {"india|mumbai|bengaluru": "Yes", "default": "No"},
                          "sponsor": {"india|mumbai|bengaluru": "No", "default": "Yes"}}
    q = "Are you authorized to work in the country for which you applied?"
    assert rule_answer(q, profile, job) == "No"                       # job is in the United States
    assert rule_answer("Will you require sponsorship?", profile, job) == "Yes"
    job.location = "Bengaluru, Karnataka"
    assert rule_answer(q, profile, job) == "Yes"
    assert rule_answer("Will you require sponsorship?", profile, job) == "No"
    assert rule_answer("Please provide the name of your current (or most recent) company", profile) == "Acme Payments"
    assert rule_answer("What is your current country of residence?", profile) == "United States"


def test_pick_option_ranges_consent_synonyms():
    years = [{"text": "0-2 years"}, {"text": "3-5 years"}, {"text": "6-10 years"}, {"text": "10+ years"}]
    assert [pick_option(str(n), years) for n in (1, 2, 4, 6, 15)] == [0, 0, 1, 2, 3]
    assert pick_option("3", [{"text": "Less than 1 year"}, {"text": "1 to 4 years"}, {"text": "More than 4 years"}]) == 1
    assert pick_option("consent", [{"text": "Select..."}, {"text": "I agree"}]) == 1
    assert pick_option("consent", [{"text": "No"}, {"text": "Maybe"}]) is None
    assert pick_option("Male", [{"text": "Woman"}, {"text": "Man"}, {"text": "Non-binary"}]) == 1
    heard = [{"text": "Careers Website"}, {"text": "LinkedIn"}, {"text": "Glassdoor"}, {"text": "Twilio Blog"}]
    assert pick_option("Company careers page", heard) == 0
    assert pick_option("A friend told me", heard) is None


def test_remote_regions(profile):
    profile["search"].update(locations=["india"], remote_ok=True, remote_regions=["india", "apac", "asia"])
    mk = lambda loc: Job("x", "1", "A", "Software Engineer", loc, "", "python", remote=True)  # noqa: E731
    assert score_job(mk("Remote - US"), profile) == 0
    assert score_job(mk("Remote, Canada"), profile) == 0
    assert score_job(mk("Remote"), profile) > 0
    assert score_job(mk("Remote (APAC)"), profile) > 0
    assert score_job(mk("Pune, India"), profile) > 0
    del profile["search"]["remote_regions"]
    assert score_job(mk("Remote - US"), profile) > 0


def test_llm_json_tolerance():
    from jobbot.llm import _json_part
    assert json.loads(_json_part('```json\n{"a": 1}\n```')) == {"a": 1}
    assert json.loads(_json_part('Here you go: {"a": {"b": 2}} hope it helps')) == {"a": {"b": 2}}


def test_telegram_and_ntfy(monkeypatch, profile, job, tmp_path):
    from jobbot import notify
    from jobbot.models import Outcome
    calls = []

    class Resp:
        status_code = 200

        def json(self):
            return {"result": [{"message": {"chat": {"id": 42, "type": "private"}}}]}

    monkeypatch.setattr(notify.requests, "get", lambda url, **kw: calls.append(("get", url, kw)) or Resp())
    monkeypatch.setattr(notify.requests, "post", lambda url, **kw: calls.append(("post", url, kw)) or Resp())
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-")
    state = State(str(tmp_path / "s.enc"), "k")
    profile["telegram"] = {"token": "T"}
    profile["ntfy"] = {"topic": "secret-topic"}
    assert notify.alert(profile, job, Outcome("captcha"), [str(pdf)], state)
    urls = [c[1] for c in calls]
    assert urls == ["https://api.telegram.org/botT/getUpdates", "https://api.telegram.org/botT/sendMessage",
                    "https://api.telegram.org/botT/sendDocument", "https://ntfy.sh/secret-topic"]
    assert calls[1][2]["json"]["chat_id"] == 42 and state.meta["telegram_chat_id"] == 42
    calls.clear()
    notify.send(profile, "again", state=state)
    assert "getUpdates" not in calls[0][1]  # chat id remembered


def test_whatsapp_log_and_alert(monkeypatch, profile, job):
    from jobbot import notify
    from jobbot.models import Outcome
    sent = []

    class Resp:
        status_code = 200

    monkeypatch.setattr(notify.requests, "get", lambda url, timeout, params: sent.append(params) or Resp())
    profile.pop("smtp", None)
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


def test_no_duplicate_applications(tmp_path, job):
    path = str(tmp_path / "s.enc")
    s = State(path, "k")
    s.record(job, "attempting")   # written before submit
    s.save()
    assert State(path, "k").seen(job)          # a crash mid-submit still blocks a second application
    s.record(job, "submitted")
    s.save()
    again = State(path, "k")
    assert again.jobs[job.key]["status"] == "submitted" and again.jobs[job.key]["tries"] == 1
    # same role seen through another source, city or company spelling
    for twin in (Job("remotive", "9", "Initech Inc.", "Senior Backend Engineer (Remote)", "Anywhere", "u"),
                 Job("ashby", "z", "initech", "Senior Backend Engineer - Berlin", "Berlin", "u"),
                 Job("greenhouse", "555", "Initech", "Senior Backend Engineer", "Pune", "u", board="initech")):
        assert again.seen(twin), twin
    assert not again.seen(Job("greenhouse", "7", "Initech", "Data Engineer", "Pune", "u", board="initech"))


def test_sealed_profile_roundtrip(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    from jobbot import config, seal
    key = Fernet.generate_key().decode()
    text = open(os.path.join(config.ROOT, "profile.example.yml")).read().replace("change-me-to-a-long-random-passphrase", "real-key")
    sealed = tmp_path / "profile.enc"
    sealed.write_bytes(Fernet(key.encode()).encrypt(text.encode()))
    assert b"Jane" not in sealed.read_bytes()
    assert seal.unseal(key, str(sealed)) == text
    with pytest.raises(SystemExit):
        seal.unseal(Fernet.generate_key().decode(), str(sealed))
    monkeypatch.setattr(config, "ROOT", str(tmp_path))
    monkeypatch.setattr(seal, "SEALED", str(sealed))
    monkeypatch.delenv("PROFILE_YAML", raising=False)
    monkeypatch.setenv("JOBBOT_KEY", key)
    monkeypatch.setattr(seal, "unseal", lambda k, path=str(sealed), _u=seal.unseal: _u(k, path))
    assert config.load_profile()["name"] == "Jane Doe"


def test_llm_tries_endpoints_until_one_answers(monkeypatch):
    from jobbot import llm as llm_mod
    seen = []

    class Resp:
        def __init__(self, status, text, payload=None):
            self.status_code, self.text, self.payload, self.headers = status, text, payload, {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise llm_mod.requests.HTTPError("x")

        def json(self):
            if self.payload is None:
                raise json.JSONDecodeError("x", "", 0)
            return self.payload

    def post(url, json, timeout, headers, allow_redirects):
        seen.append((url, json["model"]))
        assert allow_redirects is False
        if "/orgs/" in url:
            return Resp(200, "", {"choices": [{"message": {"content": '```json\n{"ok": true}\n```'}}]})
        return Resp(200, "OK\r\n")

    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "acme")
    monkeypatch.setattr(llm_mod.requests, "post", post)
    bot = llm_mod.LLM({"enabled": True, "model": "openai/gpt-4o-mini"})
    assert bot.ask("s", "u", as_json=True) == {"ok": True}
    assert [u.split("/")[3] for u, _ in seen] == ["inference", "orgs"]
    seen.clear()
    assert bot.ask("s", "u", as_json=True) == {"ok": True} and len(seen) == 1  # remembers the working endpoint


def test_workflow_files_are_valid():
    """A mis-indented workflow fails silently on GitHub; catch it here."""
    import glob

    import yaml
    from jobbot.config import ROOT
    files = glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml"))
    assert len(files) >= 3
    for path in files:
        wf = yaml.safe_load(open(path))
        assert wf.get("jobs"), path
        for name, job in wf["jobs"].items():
            assert job.get("runs-on") and job.get("steps"), f"{path}:{name}"
            for step in job["steps"]:
                assert ("run" in step) != ("uses" in step), f"{path}:{name}: {step}"
                assert all(isinstance(v, str) for v in (step.get("env") or {}).values()), f"{path}:{name}"
    # the bot must never be reachable from fork pull requests, which could read its secrets
    for wf_name in ("jobbot.yml", "notify.yml"):
        on = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", wf_name)))[True]
        assert set(on) <= {"schedule", "workflow_dispatch"}, wf_name


def test_github_inbox_keeps_details_private(monkeypatch, profile, job, tmp_path):
    from jobbot import notify
    from jobbot.models import Outcome
    posts = []

    class Resp:
        def __init__(self, status, payload):
            self.status_code, self.payload = status, payload

        def json(self):
            return self.payload

    monkeypatch.setattr(notify.requests, "get",
                        lambda url, **kw: Resp(200, {"state": "open"} if url.endswith("/issues/7") else []))

    def post(url, **kw):
        posts.append((url, kw["json"]))
        return Resp(201, {"number": 7})

    monkeypatch.setattr(notify.requests, "post", post)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "me/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "99")
    profile.pop("smtp", None)
    state = State(str(tmp_path / "s.enc"), "k")
    results = [(job, Outcome("submitted"), None, True), (job, Outcome("captcha"), None, False)]
    assert notify.daily_log(profile, results, False, 1, state)
    assert posts[0][0].endswith("/repos/me/repo/issues") and posts[0][1]["title"] == "jobbot inbox"
    comment = posts[1][1]["body"]
    assert posts[1][0].endswith("/issues/7/comments") and state.meta["inbox_issue"] == 7
    assert comment.startswith("@janedoe-example ") and "submitted 1, need you 1 (1 captcha)" in comment
    assert "/actions/runs/99" in comment
    assert "Initech" not in comment and job.url not in comment and job.title not in comment
    assert notify.alert(profile, job, Outcome("captcha"), state=state)
    assert "needs you now" in posts[-1][1]["body"] and "Initech" not in posts[-1][1]["body"]
    profile["github_inbox"] = {"details": True}
    notify.alert(profile, job, Outcome("captcha"), state=state)
    assert "Initech" in posts[-1][1]["body"]
