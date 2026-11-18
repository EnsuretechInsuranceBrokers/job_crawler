from jobbot.apply import rule_answer
from jobbot.models import Job
from jobbot.portals import Answerer, choose, internshala_search_url, job_id, naukri_chatbot, naukri_search_url


def test_portal_urls_and_ids():
    u = naukri_search_url("Backend Engineer", "Mumbai", 3)
    assert u.startswith("https://www.naukri.com/backend-engineer-jobs-in-mumbai?") and "experience=3" in u
    assert internshala_search_url("Data Science", "internships") == "https://internshala.com/internships/data-science-internships/"
    assert job_id("naukri", "https://www.naukri.com/job-listings-sde-acme-mumbai-3-to-5-years-181124001234?src=x") == "181124001234"


def test_answerer_uses_profile_then_memory_then_person(profile):
    job = Job("naukri", "1", "Acme", "Backend Engineer", "Mumbai", "u")
    asked = []
    memory = {}
    ans = Answerer(profile, job, None, memory, {}, ask=lambda prompt: asked.append(prompt) or "Kubernetes and Go")
    assert ans.candidates("What is the meaning of life?") == []
    assert ans.human("Which tools do you use daily?") == "Kubernetes and Go"
    assert ans.candidates("Which tools do you use daily?") == ["Kubernetes and Go"]   # remembered
    assert choose(["Yes"], ["No", "Yes"]) == 1
    assert choose(["Indian", "Asian"], ["American Indian or Alaska Native", "Asian", "White"]) == 1


CHAT = """<html><body><div class=chatbot_DrawerContentWrapper id=d>
<div class=botMsg id=q>Are you willing to relocate?</div>
<div id=opts><div class=ssrc__radio-btn-container onclick="pick(this)">Yes</div>
<div class=ssrc__radio-btn-container onclick="pick(this)">No</div></div>
<div class=sendMsg onclick="send()">Save</div></div>
<script>
let step = 0, chosen = null; window.log = [];
function pick(e){ chosen = e.textContent; }
function send(){
  const box = document.querySelector('[contenteditable]');
  window.log.push(chosen || (box && box.textContent));
  chosen = null; step++;
  if (step == 1) { document.getElementById('q').textContent = 'What is your notice period?';
    document.getElementById('opts').innerHTML = '<div class=textArea contenteditable=true></div>'; }
  else document.getElementById('d').remove();
}
</script></body></html>"""


def test_naukri_chatbot_answers_from_profile(profile, browser):
    profile = dict(profile, answers=dict(profile.get("answers") or {}, **{"relocat": "Yes"}))
    job = Job("naukri", "1", "Acme", "Backend Engineer", "Mumbai", "u")
    page = browser.new_page()
    page.set_content(CHAT)
    assert naukri_chatbot(page, Answerer(profile, job, None, {}, {}, ask=lambda p: ""))
    assert page.evaluate("window.log") == ["Yes", rule_answer("What is your notice period?", profile)]
    page.close()
