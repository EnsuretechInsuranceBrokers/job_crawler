import json
import os

import pytest

from jobbot.apply import apply_to
from jobbot.render import render_pdf
from jobbot.tailor import tailor

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
CFG = {"accept_consent": True, "llm_answer_questions": True}


class FakeLLM:
    enabled = True

    def __init__(self, answer):
        self.answer = answer

    def ask(self, system, user, as_json=False):
        questions = json.loads(user.split("QUESTIONS:\n")[1])
        return {q["id"]: self.answer for q in questions}


@pytest.fixture
def pdf(browser, profile, job, tmp_path):
    tailored, _ = tailor(profile, job, None)
    path = render_pdf(browser, tailored, str(tmp_path / "Jane_Doe_Resume.pdf"))
    assert open(path, "rb").read(5) == b"%PDF-" and os.path.getsize(path) > 5000
    return path


def run(browser, fixture, profile, job, pdf, llm=None, dry=False, html=None):
    ctx = browser.new_context()
    url = "file://" + os.path.join(FIX, fixture or "")
    if html is not None:
        ctx.route("**/custom.html", lambda r: r.fulfill(body=html, content_type="text/html; charset=utf-8"))
        url = "http://jobbot.test/custom.html"
    pages = []
    orig = ctx.new_page

    def new_page():  # keep the page open so the test can read what was submitted
        p = orig()
        p.close = lambda: None
        pages.append(p)
        return p

    ctx.new_page = new_page
    outcome = apply_to(ctx, job, profile, pdf, CFG, llm, dry, url=url)
    data = None
    if pages and pages[0].locator("#data").count():
        data = json.loads(pages[0].locator("#data").inner_text())
    ctx.close()
    return outcome, data


def test_greenhouse_like_needs_answer_without_llm(browser, profile, job, pdf):
    outcome, data = run(browser, "greenhouse_like.html", profile, job, pdf)
    assert outcome.status == "needs_answers"
    assert outcome.unanswered == ["Why do you want to work here?"]
    assert data is None  # nothing was submitted


def test_greenhouse_like_submits_with_llm(browser, profile, job, pdf):
    outcome, data = run(browser, "greenhouse_like.html", profile, job, pdf, FakeLLM("I enjoy payment systems."))
    assert outcome.status == "submitted", outcome
    assert data == {"first": "Jane", "last": "Doe", "email": "jane.doe@example.com", "phone": "+1 555 010 0199",
                    "linkedin": "https://www.linkedin.com/in/janedoe-example", "auth": "1",
                    "sponsor": "No, I will not require sponsorship", "why": "I enjoy payment systems.",
                    "consent": True, "gender": "d", "resume": "Jane_Doe_Resume.pdf", "cover": 0}
    assert outcome.llm_answers == [("Why do you want to work here?", "I enjoy payment systems.")]


def test_dry_run_never_submits(browser, profile, job, pdf):
    outcome, data = run(browser, "greenhouse_like.html", profile, job, pdf, FakeLLM("x"), dry=True)
    assert outcome.status == "dry_run_ok" and data is None


def test_lever_like_radio_and_unknown_question(browser, profile, job, pdf):
    outcome, data = run(browser, "lever_like.html", profile, job, pdf)
    assert outcome.status == "needs_answers" and outcome.unanswered == ["Favourite colour of the week?"]
    profile["answers"]["favourite colour"] = "Blue"
    outcome, data = run(browser, "lever_like.html", profile, job, pdf)
    assert outcome.status == "submitted", outcome
    assert data["name"] == "Jane Doe" and data["org"] == "Acme Payments" and data["resume"] == "Jane_Doe_Resume.pdf"
    assert data["cards[a][field0]"] == "No" and data["cards[a][field1]"] == "Blue"
    assert data["urls[GitHub]"] == "https://github.com/janedoe-example"


def test_llm_option_must_match(browser, profile, job, pdf):
    outcome, _ = run(browser, "lever_like.html", profile, job, pdf, FakeLLM("Green"))
    assert outcome.status == "needs_answers"


def test_captcha_is_handed_to_human(browser, profile, job, pdf):
    profile["answers"]["favourite colour"] = "Red"
    html = open(os.path.join(FIX, "lever_like.html")).read().replace(
        "<!--CAPTCHA_SLOT-->", '<iframe src="https://newassets.hcaptcha.com/captcha/v1/x/static/hcaptcha.html" width="300" height="80"></iframe>')
    outcome, data = run(browser, None, profile, job, pdf, html=html)
    assert outcome.status == "captcha" and data is None, outcome


def test_missing_form_is_error(browser, profile, job, pdf):
    outcome, _ = run(browser, None, profile, job, pdf, html="<html><body><h1>This job is closed</h1></body></html>")
    assert outcome.status == "error"
