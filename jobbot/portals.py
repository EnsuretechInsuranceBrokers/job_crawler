"""Apply on Naukri.com and Internshala from your own computer, logged in as you.

    JOBBOT_KEY=<key> python -m jobbot.portals naukri --limit 30
    JOBBOT_KEY=<key> python -m jobbot.portals internshala --limit 20
    JOBBOT_KEY=<key> python -m jobbot.portals internshala --kind internships

Both sites need an account, so this does not run on GitHub. It opens a real
browser window with its own saved profile (``~/.jobbot/browser``). The first
time, you log in yourself (password / OTP); later runs reuse that session.

For every matching posting it presses Apply and answers the site's questions
from your profile (the same `answers:`, salary estimate and memory as the
scheduled bot). When a question has no known answer, it waits for you in the
terminal, types your reply into the page, and remembers it for next time. A
CAPTCHA or login prompt also hands the window to you. Postings that send you
to the company's own site are skipped (the scheduled bot covers those).

Both sites' terms restrict automated use. Keep the limits modest (Naukri
itself caps applies per day); the account and the risk are yours.
"""
import argparse
import os
import re
import sys
import time
import urllib.parse

from .config import ROOT
from .models import Job

from .browser import PROFILE_DIR, open_profile, renderer as make_renderer, work_page  # noqa: F401
PACE_SECONDS = 25            # pause between applications: stay a polite, human-rate user of the site

APPLIED = re.compile(r"applied successfully|successfully applied|application (has been )?(sent|submitted)|"
                     r"you have successfully|already applied|\bapplied\b", re.I)
EXTERNAL = re.compile(r"apply on company (site|website)|company site", re.I)
LOGIN = re.compile(r"\blog ?in\b|\bsign ?in\b|register", re.I)


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def naukri_search_url(title, location="", experience=None, page=1):
    path = f"{slug(title)}-jobs" + (f"-in-{slug(location)}" if location else "") + (f"-{page}" if page > 1 else "")
    q = {"k": title, "jobAge": 7}
    if location:
        q["l"] = location
    if experience is not None:
        q["experience"] = int(experience)
    return f"https://www.naukri.com/{path}?" + urllib.parse.urlencode(q)


def internshala_search_url(title, kind="jobs", page=1):
    base = f"https://internshala.com/{kind}/{slug(title)}-{kind}/"
    return base + (f"page-{page}/" if page > 1 else "")


def job_id(site, url):
    m = re.search(r"-(\d{6,})(?:[/?#]|$)", url) or re.search(r"(\d{6,})", url)
    return m.group(1) if m else slug(url)[-60:]


def experience_years(profile):
    """Whole years of experience from the profile (for Naukri's experience filter), else None."""
    from .apply import rule_answer
    v = profile.get("years_experience") or rule_answer("How many years of experience do you have?", profile)
    v = v[0] if isinstance(v, list) and v else v
    m = re.search(r"\d+", str(v or ""))
    return int(m.group()) if m else None


def _search_titles(profile, n):
    titles = (profile.get("search") or {}).get("titles") or []
    return titles[:n] if n else titles


def _text(loc):
    try:
        return re.sub(r"\s+", " ", loc.inner_text(timeout=3000)).strip()
    except Exception:  # noqa: BLE001
        return ""


# ----------------------------------------------------------------- listings

def naukri_listings(page, title, location, experience):
    page.goto(naukri_search_url(title, location, experience), wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_selector("a.title, .srp-jobtuple-wrapper, .cust-job-tuple", timeout=15000)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for card in page.locator(".srp-jobtuple-wrapper, .cust-job-tuple, article.jobTuple").all()[:40]:
        a = card.locator("a.title").first
        if not a.count():
            continue
        href = a.get_attribute("href") or ""
        comp = card.locator("a.comp-name, .comp-name, .subTitle").first
        loc = card.locator(".locWdth, .loc, .location").first
        out.append(Job("naukri", job_id("naukri", href), _text(comp) if comp.count() else "", _text(a),
                       _text(loc) if loc.count() else location, href.split("?")[0], _text(card)))
    return out


def internshala_listings(page, title, kind):
    page.goto(internshala_search_url(title, kind), wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_selector(".individual_internship", timeout=15000)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for card in page.locator(".individual_internship[data-href], .individual_internship").all()[:40]:
        href = card.get_attribute("data-href") or ""
        if not href:
            link = card.locator("a.job-title-href, a.view_detail_button, h3 a").first
            href = link.get_attribute("href") if link.count() else ""
        if not href:
            continue
        url = urllib.parse.urljoin("https://internshala.com/", href)
        name = card.locator(".job-internship-name, .profile, h3").first
        comp = card.locator(".company-name, .company_name, p.company-name, h4").first
        loc = card.locator(".locations, .location_link, #location_names").first
        out.append(Job("internshala", job_id("internshala", url), _text(comp) if comp.count() else "",
                       _text(name) if name.count() else title, _text(loc) if loc.count() else "",
                       url.split("?")[0], _text(card), remote="work from home" in _text(card).lower()))
    return out


# ----------------------------------------------------------------- answering

class Answerer:
    """Answers one question: profile rules -> salary estimate -> remembered reply -> ask the person."""

    def __init__(self, profile, job, llm, memory, salary_cache, ask=None):
        self.profile, self.job, self.llm, self.memory, self.cache = profile, job, llm, memory, salary_cache
        self.ask = ask or (lambda prompt: input(prompt))

    def candidates(self, question, kind="text"):
        from .apply import rule_answer
        a = rule_answer(question, self.profile, self.job)
        if a is None and (self.profile.get("salary") or {}).get("expected") == "ai":
            from .salary import answers_for, estimate, is_expected_salary
            if is_expected_salary(question):
                a = answers_for(estimate(self.job, self.profile, self.llm, self.cache), question, kind) or None
        if a is None:
            from .apply import label_key
            a = self.memory.get(label_key(question))
        return [a] if isinstance(a, str) else list(a or [])

    def human(self, question, options=()):
        print(f"\n    The site asks: {question}")
        for i, o in enumerate(options, 1):
            print(f"      {i}. {o}")
        reply = self.ask("    Your answer (number or text, Enter = I'll answer in the browser): ").strip()
        if reply.isdigit() and options and 1 <= int(reply) <= len(options):
            reply = options[int(reply) - 1]
        if reply:
            from .apply import label_key
            self.memory[label_key(question)] = reply
        return reply


def choose(answers, options):
    """Index of the option that best matches any of `answers` (in order), else None."""
    from .apply import pick_option
    opts = [{"text": o} for o in options]
    for a in answers:
        i = pick_option(a, opts)
        if i is not None:
            return i
    return None


# ----------------------------------------------------------------- naukri

NAUKRI_BOT = ".chatbot_DrawerContentWrapper, .chatbot_Drawer, [class*=chatbot_Drawer]"


def naukri_chatbot(page, ans, max_turns=25):
    """Answer the recruiter questionnaire Naukri opens after Apply. Returns True when it closes."""
    for _ in range(max_turns):
        page.wait_for_timeout(1500)
        drawer = page.locator(NAUKRI_BOT).first
        if not drawer.count() or not drawer.is_visible():
            return True
        bot = drawer.locator(".botMsg, [class*=botMsg]").last
        question = _text(bot) if bot.count() else ""
        radios = drawer.locator(".ssrc__radio-btn-container, .singleselect-radiobutton-container label, "
                                "[class*=radio-btn-container], .chatbot_Chip, [class*=chip]").all()
        checks = drawer.locator(".multicheckboxes-container label, [class*=multiselect] label").all()
        options = [_text(o) for o in (radios or checks)]
        if options:
            i = choose(ans.candidates(question, "radio"), options)
            if i is None:
                reply = ans.human(question, options)
                i = choose([reply], options) if reply else None
            if i is None:
                input("    Answer it in the browser, then press Enter here... ")
                continue
            (radios or checks)[i].click(timeout=5000)
        else:
            box = drawer.locator("div.textArea[contenteditable], [contenteditable=true], textarea, input[type=text]").last
            if not box.count():
                input("    Naukri shows something the bot cannot read - finish it in the browser, then press Enter... ")
                continue
            vals = ans.candidates(question)
            reply = vals[0] if vals else ans.human(question)
            if not reply:
                input("    Answer it in the browser, then press Enter here... ")
                continue
            box.click(timeout=5000)
            box.fill(reply) if box.evaluate("e => e.tagName") in ("INPUT", "TEXTAREA") else page.keyboard.type(reply, delay=20)
        send = drawer.locator(".sendMsg, [class*=sendMsg], button:has-text('Save'), button:has-text('Submit')").last
        if send.count():
            send.click(timeout=5000)
    return False


def naukri_apply(page, job, ans):
    page.goto(job.url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2000)
    body = _text(page.locator("body"))
    if re.search(r"\balready applied\b", body, re.I):
        return "already"
    btn = page.locator("#apply-button, button.apply-button, button:has-text('Apply')").first
    if not btn.count():
        return "closed"
    label = _text(btn)
    if EXTERNAL.search(label) or page.locator("#company-site-button, button:has-text('Apply on company site')").count():
        return "external"
    btn.click(timeout=10000)
    page.wait_for_timeout(2500)
    if LOGIN.search(page.url) or page.locator("#usernameField, input[placeholder*='Email ID']").count():
        return "login"
    if not naukri_chatbot(page, ans):
        return "stuck"
    page.wait_for_timeout(2000)
    return "submitted" if APPLIED.search(_text(page.locator("body"))) else "unconfirmed"


# ----------------------------------------------------------------- internshala

def internshala_apply(page, job, ans, profile, resume_pdf, cover_text, acfg, llm):
    from .apply import FormFiller, _captcha
    page.goto(job.url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(1500)
    if page.locator("button:has-text('Already applied'), .already_applied").count():
        return "already"
    btn = page.locator("#easy_apply_button, #top_easy_apply_button, button:has-text('Apply now'), a:has-text('Apply now')").first
    if not btn.count():
        return "closed"
    btn.click(timeout=10000)
    page.wait_for_timeout(2000)
    if "login" in page.url or page.locator("#modal_email, #login-modal:visible").count():
        return "login"
    proceed = page.locator("#continue_button, button:has-text('Proceed to application'), button:has-text('Proceed')").first
    if proceed.count() and proceed.is_visible():
        proceed.click(timeout=10000)
        page.wait_for_timeout(2000)
    # Cover letter: a rich-text (Quill) editor answering "Why should you be hired for this role?"
    editor = page.locator("#cover_letter_holder .ql-editor, .cover_letter .ql-editor, textarea#cover_letter").first
    if editor.count() and cover_text and not _text(editor):
        editor.click(timeout=5000)
        page.keyboard.type(cover_text[:2000], delay=2)
    avail = page.locator("input[name*=confirm_availability][value=yes], label:has-text('Yes, I am available')").first
    if avail.count():
        avail.click(timeout=5000)
    filler = FormFiller(page, profile, job, resume_pdf, acfg, llm, (None, cover_text))
    open_q = filler.fill()
    for q in open_q:
        reply = ans.human(q)
        if reply:
            field = page.get_by_label(q, exact=False).first
            if field.count():
                field.fill(reply)
                continue
        input(f"    Answer '{q[:60]}' in the browser, then press Enter here... ")
    if _captcha(page):
        input("    The page shows a CAPTCHA - solve it in the browser, then press Enter... ")
    submit = page.locator("#submit, input#submit, button:has-text('Submit')").last
    if not submit.count():
        return "stuck"
    submit.click(timeout=10000)
    page.wait_for_timeout(3000)
    return "submitted" if APPLIED.search(_text(page.locator("body"))) or "success" in page.url else "unconfirmed"


# ----------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(prog="jobbot.portals", description=__doc__.split("\n\n")[0])
    ap.add_argument("site", choices=("naukri", "internshala"))
    ap.add_argument("--limit", type=int, default=30, help="applications this run (default 30)")
    ap.add_argument("--kind", choices=("jobs", "internships"), default="jobs", help="Internshala only")
    ap.add_argument("--titles", type=int, default=12, help="how many of your search titles to search (0 = all)")
    ap.add_argument("--location", default="", help="Naukri location filter, e.g. Mumbai (default: anywhere)")
    ap.add_argument("--no-sync", action="store_true", help="use the local .state/state.enc and do not publish changes")
    args = ap.parse_args(argv)

    from playwright.sync_api import sync_playwright

    from .config import load_config, load_profile
    from .cover import cover_letter
    from .sync import STATE, sync_down, sync_up
    from .llm import make_llm
    from .match import score_job
    from .render import render_resume
    from .state import State
    from .tailor import tailor

    cfg, profile = load_config(), load_profile()
    if not args.no_sync and not sync_down():
        print("Could not fetch the bot's memory; continuing with a local copy (it will not be published).")
        args.no_sync = True
    state = State(STATE, profile["state_key"])
    from .apply import with_learned
    memory = state.meta.setdefault("learned", {})        # answers you type once; the scheduled bot uses them too
    profile = with_learned(profile, memory)
    cache = cfg.setdefault("salary_cache", {})
    llm = make_llm(cfg["llm"], profile)
    acfg = dict(cfg["apply"], llm_answer_questions=cfg["llm"].get("answer_questions", True), memory=state.meta.setdefault("answers", {}))
    reapply = int(cfg["apply"].get("reapply_after_days", 7))
    years = experience_years(profile)
    out_dir = os.path.join(ROOT, "out", "portals", args.site)
    os.makedirs(out_dir, exist_ok=True)
    name = re.sub(r"\W+", "_", profile["name"]).strip("_")

    done, tally = 0, {}
    with sync_playwright() as pw:
        context = open_profile(pw)
        renderer = make_renderer(pw)  # resume PDFs
        page = work_page(context)
        home = "https://www.naukri.com/nlogin/login" if args.site == "naukri" else "https://internshala.com/login/user"
        page.goto(home, wait_until="domcontentloaded", timeout=45000)
        input("Log in in the browser window if it asks (skip if you are already logged in), then press Enter here... ")

        seen_ids = set()
        for title in _search_titles(profile, args.titles):
            if done >= args.limit:
                break
            print(f"\nSearching {args.site} for '{title}'...")
            try:
                jobs = (naukri_listings(page, title, args.location, years) if args.site == "naukri"
                        else internshala_listings(page, title, args.kind))
            except Exception as e:  # noqa: BLE001
                print(f"  search failed ({type(e).__name__})")
                continue
            for job in jobs:
                if done >= args.limit:
                    break
                if job.key in seen_ids or state.seen(job, reapply):
                    continue
                seen_ids.add(job.key)
                if not score_job(job, profile):
                    continue
                print(f"  -> {job.title} - {job.company or '?'} ({job.location or '-'})")
                ans = Answerer(profile, job, llm, memory, cache)
                try:
                    if args.site == "naukri":
                        status = naukri_apply(page, job, ans)
                    else:
                        tailored, _ = tailor(profile, job, llm)
                        jd = os.path.join(out_dir, job.id)
                        os.makedirs(jd, exist_ok=True)
                        pdf = render_resume(renderer, profile, tailored, job,
                                            os.path.join(jd, f"{name}_Resume.pdf"))
                        letter, _ = cover_letter(profile, tailored, job, llm)
                        status = internshala_apply(page, job, ans, profile, pdf, letter, acfg, llm)
                except Exception as e:  # noqa: BLE001
                    status = "error"
                    print(f"     {type(e).__name__}")
                if status == "login":
                    input("     The site wants you to log in - do it in the browser, then press Enter (this job is retried next run)... ")
                    continue
                if status == "stuck":
                    r = input("     Finish this one in the browser. Enter = I submitted it, s = skip: ").strip().lower()
                    status = "submitted" if r == "" else "skipped"
                tally[status] = tally.get(status, 0) + 1
                print(f"     {status}")
                if status in ("submitted", "unconfirmed", "already", "external", "closed"):
                    state.record(job, "submitted" if status in ("submitted", "unconfirmed", "already") else "manual")
                    state.save()
                if status in ("submitted", "unconfirmed"):
                    done += 1
                    if not args.no_sync and sum(tally.get(k, 0) for k in ('submitted', 'unconfirmed')) % 10 == 0:
                        state.save()
                        sync_up(profile["state_key"])  # every 10: share progress with the other lanes as you go
                    time.sleep(PACE_SECONDS)
        context.close()
        renderer.close()
    state.save()
    if not args.no_sync:
        print("\nSaving the bot's memory..." + (" done." if sync_up(profile["state_key"]) else " FAILED - run again later."))
    print(f"\n{done} application(s) sent on {args.site}. " + ", ".join(f"{k}: {v}" for k, v in sorted(tally.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
