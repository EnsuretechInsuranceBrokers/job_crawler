import builtins

from jobbot.careers import login_pages, pending, walk, workday_home
from jobbot.apply import with_learned
from jobbot.models import Job
from jobbot.state import State

WIZARD = """<html><body><form onsubmit="return false">
<div id=s1><label for=fn>First Name *</label><input id=fn name=first_name required>
<label for=em>Email *</label><input id=em type=email name=email required>
<button type=button onclick="if(fn.value&&em.value){s1.hidden=true;s2.hidden=false}">Save and Continue</button></div>
<div id=s2 hidden><label for=q>What is your favourite data structure? *</label><input id=q name=q required>
<button type=button onclick="if(q.value){document.body.innerHTML='<h1>Thank you for applying</h1>'; window.sent=q.value}">Submit</button></div>
</form></body></html>"""


def test_walk_fills_pages_asks_once_and_submits(profile, browser, tmp_path, monkeypatch):
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    asked = []
    monkeypatch.setattr(builtins, "input", lambda prompt="": asked.append(prompt) or "Trie")
    job = Job("workday", "1", "Acme", "Backend Engineer", "Bengaluru", "u")
    learned = {}
    page = browser.new_page()
    page.set_content(WIZARD)
    acfg = {"llm_answer_questions": False, "memory": {}}
    assert walk(page, job, profile, learned, str(pdf), None, "", acfg, None) == "submitted"
    assert page.evaluate("window.sent") == "Trie" and len(asked) == 1
    assert learned == {"What is your favourite data structure?": "Trie"}
    # the next form - and the scheduled bot - answer it without asking
    from jobbot.apply import rule_answer
    assert rule_answer("What is your favourite data structure? *", with_learned(profile, learned)) == "Trie"
    page.close()


def test_pending_counts_manual_packets_but_not_recent_applications(tmp_path):
    s = State(str(tmp_path / "s.enc"), "k")
    job = Job("workday", "9", "Nvidia", "Software Engineer", "Pune", "u", board="nvidia")
    s.record(job, "manual")
    assert pending(s, job, 7)
    s.record(job, "submitted")
    assert not pending(s, job, 7)


def test_login_pages():
    pages = login_pages({"workday": ["nvidia/wd5/NVIDIAExternalCareerSite"], "amazon": True, "google": True})
    assert pages[0] == workday_home("nvidia/wd5/NVIDIAExternalCareerSite") == "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
    assert any("amazon.jobs" in p for p in pages) and any("google.com" in p for p in pages)


def test_one_window_is_reused_and_extra_tabs_closed(browser):
    from jobbot.browser import work_page
    ctx = browser.new_context()
    first = ctx.new_page()
    ctx.new_page()                      # e.g. a site's Apply button opened a new tab
    assert work_page(ctx) is first and len(ctx.pages) == 1
    assert work_page(ctx) is first      # the next job reuses it rather than opening a window
    ctx.close()


def test_saved_profile_keeps_sign_in_cookies_between_runs(tmp_path, browser):
    import glob
    import os
    exe = os.environ.get("JOBBOT_CHROMIUM") or (glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome") or [None])[0]
    cookie = {"name": "sid", "value": "x", "domain": "example.com", "path": "/", "expires": 4102444800}
    chromium = browser.browser_type
    ctx = chromium.launch_persistent_context(str(tmp_path), executable_path=exe)
    ctx.add_cookies([cookie])
    ctx.close()
    ctx = chromium.launch_persistent_context(str(tmp_path), executable_path=exe)   # a later run
    assert any(c["name"] == "sid" for c in ctx.cookies("https://example.com"))
    ctx.close()
