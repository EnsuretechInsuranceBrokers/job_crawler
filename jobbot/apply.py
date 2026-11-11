"""Fill and submit application forms on Greenhouse, Lever and Ashby.

The filler is label-driven rather than tied to one site's markup: it lists the
form's questions, answers them from the profile, and submits only when every
required question has an answer. CAPTCHAs and email-verification codes are
never worked around - those applications are handed back to the human.
"""
import json
import re
import time

from .models import Outcome
from .util import detail, norm

CONFIRM = re.compile(r"thank you|thanks for applying|application (has been|was) (received|submitted)|"
                     r"(we('ve| have)|i've) received your application|successfully (submitted|applied)|"
                     r"application submitted|application received", re.I)
VERIFY = re.compile(r"(verification|security) code|verify your email|enter the code", re.I)
DECLINE = re.compile(r"decline|prefer not|rather not|don'?t wish|do not wish|not (wish|want) to", re.I)
CONSENT = re.compile(r"agree|consent|acknowledge|privacy|confirm|certify|understand|terms", re.I)
CAPTCHA_SEL = ('iframe[src*="recaptcha/api2/bframe"], iframe[src*="recaptcha/enterprise/bframe"], '
               'iframe[src*="recaptcha/api2/anchor"]:not([src*="size=invisible"]), '
               'iframe[src*="hcaptcha.com"], iframe[src*="challenges.cloudflare.com"], iframe[title*="challenge" i]')


def apply_url(job):
    """URL of the bare application form, or None if we cannot auto-apply there."""
    if job.source == "greenhouse":
        return f"https://boards.greenhouse.io/embed/job_app?for={job.board}&token={job.id}"
    if job.source == "lever" and job.url:
        return job.url.rstrip("/") + "/apply"
    if job.source == "ashby" and job.url:
        return job.url.rstrip("/") + "/application"
    url = job.url or ""
    m = re.search(r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/([\w-]+)/jobs/(\d+)", url)
    if m:
        return f"https://boards.greenhouse.io/embed/job_app?for={m.group(1)}&token={m.group(2)}"
    m = re.search(r"(https://jobs\.(?:eu\.)?lever\.co/[\w.-]+/[0-9a-f-]{36})", url)
    if m:
        return m.group(1) + "/apply"
    m = re.search(r"(https://jobs\.ashbyhq\.com/[\w.%-]+/[0-9a-f-]{36})", url)
    if m:
        return m.group(1) + "/application"
    return None


# Lists every question on the page. Each element gets a data-jb id so Python
# can act on it afterwards.
SCAN_JS = r"""
() => {
  window.__jb = window.__jb || 0;
  const tag = el => el.dataset.jb || (el.dataset.jb = 'f' + (++window.__jb));
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const shown = el => {
    if (!el) return false;
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  };
  const labelEl = el => {
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) return l; }
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const l = document.getElementById(lb.split(' ')[0]); if (l) return l; }
    if (el.closest('label')) return el.closest('label');
    let p = el.parentElement;
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
      const l = [...p.querySelectorAll('label, legend, [class*="label" i]')].find(x => !x.contains(el) && clean(x.innerText));
      if (l) return l;
    }
    return null;
  };
  const describe = (el, lab) => {
    const text = clean(lab ? lab.innerText : '') || clean(el.getAttribute('aria-label')) || clean(el.placeholder) || clean(el.name);
    const box = el.closest('[class*="field" i], [class*="question" i], li, fieldset') || el.parentElement;
    const required = el.required || el.getAttribute('aria-required') === 'true' || /[*✱]|\(required\)/i.test(text)
      || (lab && /required/i.test(lab.className + ' ' + [...lab.querySelectorAll('*')].map(x => x.className).join(' ')))
      || (box && /\brequired\b/i.test(String(box.className)) && !/optional/i.test(text));
    return { label: clean(text.replace(/[*✱]|\(required\)/gi, '')), required: !!required && !/\(optional\)/i.test(text) };
  };
  const groupLabel = inputs => {
    const fs = inputs[0].closest('fieldset');
    if (fs && fs.querySelector('legend') && clean(fs.querySelector('legend').innerText)) return fs.querySelector('legend');
    const rg = inputs[0].closest('[role="radiogroup"], [role="group"]');
    if (rg && rg.getAttribute('aria-labelledby')) { const l = document.getElementById(rg.getAttribute('aria-labelledby').split(' ')[0]); if (l) return l; }
    const ids = new Set(inputs.map(i => i.id).filter(Boolean));
    let p = inputs[0].parentElement;
    while (p && !inputs.every(i => p.contains(i))) p = p.parentElement;
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
      const c = [...p.querySelectorAll('legend, label, [class*="label" i], [class*="question" i], [class*="title" i], h3, h4, p')].find(x =>
        clean(x.innerText) && !inputs.some(inp => x.contains(inp)) && !(x.htmlFor && ids.has(x.htmlFor))
        && !(x.closest('label') && inputs.some(inp => x.closest('label').contains(inp))));
      if (c) return c;
    }
    return null;
  };
  const out = [], groups = new Map();
  for (const el of document.querySelectorAll('input, textarea, select')) {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(type) || el.disabled) continue;
    if (type === 'radio' || type === 'checkbox') {
      const lab = labelEl(el);
      if (!shown(el) && !shown(lab)) continue;
      const key = type + ':' + (el.name || tag(el));
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(el);
      continue;
    }
    if (type === 'file') {
      const lab = labelEl(el), d = describe(el, lab);
      out.push({ jb: tag(el), kind: 'file', label: clean(d.label + ' ' + (el.name || '') + ' ' + (el.id || '')), required: d.required, filled: el.files.length > 0 });
      continue;
    }
    if (!shown(el)) continue;
    const d = describe(el, labelEl(el));
    const f = { jb: tag(el), label: d.label, required: d.required, filled: !!el.value };
    if (el.tagName === 'SELECT') {
      f.kind = 'select';
      f.options = [...el.options].filter(o => o.value !== '' && !o.disabled).map(o => ({ text: clean(o.text), value: o.value }));
      f.filled = el.value !== '';
    } else if (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete') === 'list' || el.getAttribute('aria-haspopup') === 'listbox') {
      f.kind = 'combobox';
      const box = el.closest('[class*="select" i]');
      f.filled = !!el.value || !!(box && box.querySelector('[class*="single-value" i], [class*="multi-value" i]'));
    } else {
      f.kind = el.tagName === 'TEXTAREA' ? 'textarea' : 'text';
    }
    out.push(f);
  }
  for (const [key, inputs] of groups) {
    const options = inputs.map(i => ({ text: clean((labelEl(i) || {}).innerText) || i.value, jb: tag(i) }));
    const any = inputs.some(i => i.checked);
    if (inputs.length === 1 && key.startsWith('checkbox')) {
      const d = describe(inputs[0], labelEl(inputs[0]));
      const g = groupLabel(inputs);
      const q = g ? clean(g.innerText.replace(/[*✱]/g, '')) : '';
      out.push({ jb: options[0].jb, kind: 'checkbox', label: q && q !== d.label ? q + ' - ' + d.label : d.label, required: d.required || (g ? /[*✱]/.test(g.innerText) : false), filled: any });
      continue;
    }
    const g = groupLabel(inputs);
    const d = describe(inputs[0], g);
    out.push({ jb: options[0].jb, kind: key.startsWith('radio') ? 'radio' : 'checkboxes', label: g ? d.label : '', required: g ? d.required : inputs.some(i => i.required), options, filled: any });
  }
  return out;
}
"""


def _last(p):
    return p["experience"][0] if p.get("experience") else {}


BUILTIN = [  # (regex on short label, profile -> answer); first match wins
    (r"first name|given name|forename", lambda p: p["name"].split()[0]),
    (r"last name|family name|surname", lambda p: " ".join(p["name"].split()[1:]) or p["name"]),
    (r"preferred name", lambda p: p["name"].split()[0]),
    (r"^(full |legal |your )*name\b", lambda p: p["name"]),
    (r"e-?mail", lambda p: p["email"]),
    (r"phone|mobile|telephone", lambda p: p.get("phone")),
    (r"linkedin", lambda p: p["links"].get("linkedin")),
    (r"github", lambda p: p["links"].get("github")),
    (r"twitter|\bx\.com", lambda p: p["links"].get("twitter")),
    (r"portfolio|website|personal site|blog|other (link|url)", lambda p: p["links"].get("website")),
    (r"current (company|employer)|^org|^company( name)?$|most recent (company|employer)", lambda p: _last(p).get("company")),
    (r"current (job )?(title|role|position)|^title$|^job title", lambda p: _last(p).get("title")),
    (r"^country", lambda p: p.get("country") or (p.get("location") or "").split(",")[-1].strip()),
    (r"^(current |your )?(location|city)|where are you (currently )?(based|located|living)|city.*(live|reside)",
     lambda p: p.get("location")),
]


def rule_answer(label, profile):
    """Answer from the user's `answers` map, then from built-in identity rules."""
    for pattern, answer in (profile.get("answers") or {}).items():
        try:
            if re.search(pattern, label, re.I):
                return str(answer)
        except re.error:
            continue
    if len(label) <= 70:
        for pattern, fn in BUILTIN:
            if re.search(pattern, label, re.I):
                value = fn(profile)
                return str(value) if value else None
    return None


def pick_option(answer, options):
    """Map a free-form answer onto one of the offered option texts. Returns index or None."""
    a = norm(answer)
    texts = [norm(o["text"]) for o in options]
    if a == "decline":
        return next((i for i, o in enumerate(options) if DECLINE.search(o["text"])), None)
    tests = (lambda t: t == a,
             lambda t: t.startswith(a + " ") or a.startswith(t + " "),
             lambda t: re.search(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])", t) is not None)
    for n, test in enumerate(tests):
        hits = [i for i, t in enumerate(texts) if t and test(t)]
        if len(hits) == 1 or (hits and n == 0):
            return hits[0]
        if hits:
            return None  # ambiguous: do not guess
    return None


LLM_SYSTEM = """You fill in a job application for the candidate below. Answer each question ONLY from the candidate's facts.
Return JSON mapping each question id to an answer string, or null when the facts do not determine the answer
(never guess about legal status, visas, salary, demographics, dates, or anything not stated).
For questions with options the answer must be exactly one of the option texts.
For open questions about motivation or fit, write 2-4 honest sentences grounded in the candidate's real experience and the job posting; invent nothing."""


def llm_answers(llm, questions, profile, job):
    if not (llm and llm.enabled and questions):
        return {}
    facts = {k: v for k, v in profile.items() if k not in ("state_key", "smtp", "search")}
    listing = [{"id": q["jb"], "question": q["label"], **({"options": [o["text"] for o in q["options"]]} if q.get("options") else {})}
               for q in questions]
    reply = llm.ask(LLM_SYSTEM, f"CANDIDATE:\n{json.dumps(facts, default=str)}\n\nJOB: {job.title} at {job.company}\n"
                                f"{job.description[:3000]}\n\nQUESTIONS:\n{json.dumps(listing)}", as_json=True)
    if not isinstance(reply, dict):
        return {}
    return {k: str(v).strip() for k, v in reply.items() if isinstance(v, (str, int, float)) and str(v).strip()}


class FormFiller:
    def __init__(self, page, profile, job, resume_pdf, cfg, llm=None):
        self.page, self.profile, self.job, self.pdf = page, profile, job, resume_pdf
        self.cfg, self.llm = cfg, llm
        self.done, self.filled, self.llm_used = set(), 0, []

    def loc(self, jb):
        return self.page.locator(f'[data-jb="{jb}"]')

    def _tick(self, jb):
        try:
            self.loc(jb).check(force=True, timeout=3000)
        except Exception:  # noqa: BLE001 - visually hidden input: click it through the DOM
            self.page.evaluate("jb => document.querySelector('[data-jb=\"' + jb + '\"]').click()", jb)

    def _combobox(self, f, answer):
        box = self.loc(f["jb"])
        box.click(timeout=5000)
        self.page.wait_for_timeout(400)
        opts = self.page.locator('[role="option"]:visible')

        def choose():
            texts = [{"text": t} for t in opts.all_inner_texts()]
            i = pick_option(answer, texts)
            if i is not None:
                opts.nth(i).click()
                return True
            return False

        if choose():
            return True
        if norm(answer) == "decline":
            self.page.keyboard.press("Escape")
            return False
        box.fill(answer[:60])
        self.page.wait_for_timeout(1200)
        if choose() or (opts.count() and (opts.first.click() or True)):
            return True
        self.page.keyboard.press("Escape")
        box.fill("")
        return False

    def put(self, f, answer):
        """Enter one answer. Returns True when the field was actually filled."""
        kind = f["kind"]
        if kind in ("text", "textarea"):
            if norm(answer) == "decline":
                return False
            self.loc(f["jb"]).fill(answer, timeout=5000)
            return True
        if kind == "combobox":
            return self._combobox(f, answer)
        if kind == "checkbox":
            if norm(answer) in ("yes", "true", "agree", "i agree"):
                self._tick(f["jb"])
                return True
            return False
        i = pick_option(answer, f["options"])
        if i is None:
            return False
        if kind == "select":
            self.loc(f["jb"]).select_option(value=f["options"][i]["value"], timeout=5000)
        else:
            self._tick(f["options"][i]["jb"])
        return True

    def answer(self, f):
        if f["kind"] == "file":
            if re.search(r"resume|\bcv\b|curriculum", f["label"], re.I) and not re.search(r"cover", f["label"], re.I):
                self.loc(f["jb"]).set_input_files(self.pdf, timeout=10000)
                return True
            return False
        if f["kind"] == "checkbox" and f["required"] and self.cfg.get("accept_consent", True) and CONSENT.search(f["label"]):
            self._tick(f["jb"])
            return True
        a = rule_answer(f["label"], self.profile) if f["label"] else None
        return self.put(f, a) if a else False

    def fill(self):
        """Fill what the profile can answer. Returns the required questions left open."""
        open_required = {}
        for _ in range(3):  # answering can reveal follow-up questions
            fields = [f for f in self.page.evaluate(SCAN_JS) if f["jb"] not in self.done]
            if not fields:
                break
            for f in fields:
                self.done.add(f["jb"])
                if f.get("filled"):
                    continue
                try:
                    ok = self.answer(f)
                except Exception as e:  # noqa: BLE001
                    detail(f"could not fill '{f['label'][:50]}': {type(e).__name__}")
                    ok = False
                if ok:
                    self.filled += 1
                elif f["required"]:
                    open_required[f["jb"]] = f
            self.page.wait_for_timeout(300)
        if open_required and self.cfg.get("llm_answer_questions", True):
            askable = [f for f in open_required.values() if f["kind"] != "file" and f["label"]]
            for jb, a in llm_answers(self.llm, askable, self.profile, self.job).items():
                f = open_required.get(jb)
                try:
                    if f and self.put(f, a):
                        self.filled += 1
                        self.llm_used.append((f["label"], a))
                        del open_required[jb]
                except Exception:  # noqa: BLE001
                    pass
        return [f["label"] or f"(unlabelled {f['kind']})" for f in open_required.values()]


def _captcha(page):
    for frame in page.locator(CAPTCHA_SEL).all():
        try:
            box = frame.bounding_box()
            if frame.is_visible() and box and box["height"] > 40 and box["width"] > 40:
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


def _text(page):
    try:
        return page.inner_text("body", timeout=5000)
    except Exception:  # noqa: BLE001
        return ""


def apply_to(context, job, profile, resume_pdf, cfg, llm=None, dry_run=True, url=None):
    """Open the job's form, fill it, and submit unless dry_run. Returns an Outcome."""
    url = url or apply_url(job)
    if not url:
        return Outcome("manual", "site is not auto-appliable")
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_selector("input[type=file], input[type=email], input[name=email]", state="attached", timeout=20000)
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        filler = FormFiller(page, profile, job, resume_pdf, cfg, llm)
        unanswered = filler.fill()
        detail(f"{job.company}: filled {filler.filled} fields, {len(unanswered)} required unanswered")
        if filler.filled < 2:
            return Outcome("error", "application form not found (posting closed or layout changed)")
        if unanswered:
            return Outcome("needs_answers", "required questions the profile cannot answer", unanswered, filler.llm_used)
        if _captcha(page):
            return Outcome("captcha", "form shows a CAPTCHA", llm_answers=filler.llm_used)
        submit = page.get_by_role("button", name=re.compile(r"submit|send application|^apply( now)?$", re.I))
        if not submit.count():
            submit = page.locator("button[type=submit], input[type=submit]")
        if not submit.count():
            return Outcome("error", "submit button not found", llm_answers=filler.llm_used)
        if dry_run:
            return Outcome("dry_run_ok", f"form complete ({filler.filled} fields); would submit", llm_answers=filler.llm_used)

        before_url, had_confirm = page.url, bool(CONFIRM.search(_text(page)))
        submit.last.click(timeout=10000)
        deadline = time.time() + 30
        while time.time() < deadline:
            page.wait_for_timeout(1500)
            if _captcha(page):
                return Outcome("captcha", "CAPTCHA challenge after pressing submit", llm_answers=filler.llm_used)
            body = _text(page)
            if VERIFY.search(body):
                return Outcome("verification", "site asked for an emailed verification code", llm_answers=filler.llm_used)
            moved = page.url != before_url and re.search(r"confirm|thank|success|submitted", page.url, re.I)
            if moved or (CONFIRM.search(body) and not had_confirm):
                return Outcome("submitted", "confirmation page seen", llm_answers=filler.llm_used)
        return Outcome("unconfirmed", "submitted but no confirmation seen - check your inbox", llm_answers=filler.llm_used)
    except Exception as e:  # noqa: BLE001 - message may echo typed values, so keep only the type
        return Outcome("error", type(e).__name__)
    finally:
        page.close()
