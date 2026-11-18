"""Apply on big companies' own career sites (Workday, Amazon, Microsoft, Google, Apple,
SmartRecruiters) from your computer, signed in to your accounts.

    JOBBOT_KEY=<key> python -m jobbot.careers login              # once: sign in to every site
    JOBBOT_KEY=<key> python -m jobbot.careers apply --limit 20   # whenever you like, e.g. daily
    JOBBOT_KEY=<key> python -m jobbot.careers apply --only workday,amazon

These sites need an account, so the scheduled bot on GitHub only prepares
"apply yourself" packets for them. This command finishes those packets for you:
it finds the matching postings itself, opens each one in a browser window that
remembers your sign-ins (``~/.jobbot/browser``), presses Apply, and walks the
application page by page - uploading the tailored resume, answering from your
profile, pressing Next and finally Submit.

It stops only for what needs you: a sign-in, a CAPTCHA, an emailed code, or a
question it has never seen. A question you answer in the terminal is remembered
and answered automatically from then on, here and by the scheduled bot.
"""
import argparse
import os
import re
import sys
import time

from .config import ROOT

from .browser import PROFILE_DIR, open_profile, renderer as make_renderer, work_page  # noqa: F401
SOURCES = ("workday", "amazon", "microsoft", "google", "apple", "smartrecruiters")

# Per site: where to sign in, and the buttons that lead from the posting into the application.
# Each stage lists alternatives in order of preference; a later stage may be absent.
SITES = {
    "workday": {"entry": [[r"^apply$"], [r"use my last application", r"apply manually"]]},
    "amazon": {"login": "https://account.amazon.jobs/en-US/login", "entry": [[r"^apply now$", r"^apply$"]]},
    "microsoft": {"login": "https://jobs.careers.microsoft.com/global/en/profile", "entry": [[r"^apply$", r"^apply now$"]]},
    "google": {"login": "https://www.google.com/about/careers/applications/profile/", "entry": [[r"^apply$", r"^apply now$"]]},
    "apple": {"login": "https://jobs.apple.com/en-in/profile/info", "entry": [[r"^submit resume$", r"^apply now$", r"^apply$"]]},
    "smartrecruiters": {"entry": [[r"i'?m interested", r"^apply now$", r"^apply$"]]},
}
NEXT = re.compile(r"^\s*(save and continue|save & continue|continue|next|next step|review|review application|"
                  r"proceed|save and next)\s*$", re.I)
SUBMIT = re.compile(r"^\s*(submit|submit application|send application|apply|finish|complete( application)?)\s*$", re.I)
SIGN_IN = re.compile(r"\b(sign in|log in|create account|sign up)\b", re.I)
DONE = ("submitted", "unconfirmed", "dismissed", "ineligible")


def workday_home(spec):
    tenant, wd, site = spec.split("/")
    return f"https://{tenant}.{wd}.myworkdayjobs.com/{site}"


def login_pages(sources_cfg):
    pages = [workday_home(s) for s in sources_cfg.get("workday") or []]
    pages += [SITES[s]["login"] for s in ("amazon", "microsoft", "google", "apple") if sources_cfg.get(s)]
    return pages


def pending(state, job, reapply_days):
    """True unless this job (or the same role elsewhere) was applied to within reapply_days.
    "manual" packets from the scheduled bot count as pending - finishing them is the point."""
    from .match import role_key
    now, role = time.time(), role_key(job)
    for key, v in state.jobs.items():
        if (key == job.key or v.get("role") == role) and v.get("status") in DONE \
                and (not reapply_days or now - v["ts"] < reapply_days * 86400):
            return False
    return True


def visible_button(page, rx):
    for role in ("button", "link"):
        loc = page.get_by_role(role, name=rx)
        for i in range(loc.count() - 1, -1, -1):
            b = loc.nth(i)
            try:
                if b.is_visible() and b.is_enabled():
                    return b
            except Exception:  # noqa: BLE001
                pass
    return None


def wants_sign_in(page):
    try:
        if page.locator("input[type=password]:visible").count():
            return True
    except Exception:  # noqa: BLE001
        pass
    return bool(re.search(r"/(login|signin|sign-in|auth)\b", page.url, re.I))


def _newest(context, page):
    return context.pages[-1] if context.pages and context.pages[-1] is not page else page


def enter(context, page, job):
    """From the posting to the first page of the application. Returns the page (it may be a new tab)."""
    page.goto(job.url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)
    for n, stage in enumerate(SITES[job.source]["entry"]):
        btn = None
        for _ in range(6):
            btn = next((b for b in (visible_button(page, re.compile(rx, re.I)) for rx in stage) if b), None)
            if btn:
                break
            page.wait_for_timeout(1000)
        if not btn:
            if n == 0:
                return None
            break
        before = len(context.pages)
        btn.click(timeout=10000)
        page.wait_for_timeout(3000)
        if len(context.pages) > before:
            page = _newest(context, page)
            page.wait_for_load_state("domcontentloaded")
    return page


def ask(prompt):
    return input(prompt).strip().lower()  # noqa: the person at the keyboard


def walk(page, job, profile, learned, pdf, letter_pdf, letter, acfg, llm, auto_submit=True, max_steps=15):
    """Fill and advance an application page by page. Returns an outcome status."""
    from .apply import CONFIRM, VERIFY, FormFiller, _captcha, _form_errors, with_learned
    from .portals import Answerer
    last, stuck = None, 0
    for _ in range(max_steps):
        page.wait_for_timeout(2000)
        if wants_sign_in(page):
            ask("    Sign in (or create the account) in the browser window, then press Enter... ")
            continue
        if _captcha(page):
            ask("    Solve the CAPTCHA in the browser, then press Enter... ")
        try:
            body = page.inner_text("body", timeout=5000)
        except Exception:  # noqa: BLE001
            body = ""
        if CONFIRM.search(body):
            return "submitted"
        if VERIFY.search(body):
            ask("    Enter the emailed code in the browser, then press Enter... ")
            continue
        prof = with_learned(profile, learned)
        filler = FormFiller(page, prof, job, pdf, acfg, llm, (letter_pdf, letter))
        open_q = filler.fill()
        if open_q:
            ans = Answerer(prof, job, llm, learned, acfg.setdefault("salary_cache", {}))
            for q in open_q:
                ans.human(q)  # remembered in `learned`, then typed in by the refill below
            filler = FormFiller(page, with_learned(profile, learned), job, pdf, acfg, llm, (letter_pdf, letter))
            left = filler.fill()
            if left:
                print("    Still open: " + "; ".join(q[:60] for q in left))
                ask("    Fill those in the browser, then press Enter... ")
        nxt, sub = visible_button(page, NEXT), visible_button(page, SUBMIT)
        state = (page.url, len(body), body[:2000])
        stuck = stuck + 1 if state == last else 0
        last = state
        if stuck >= 2 or not (nxt or sub):
            r = ask("    The bot cannot move this one on. Finish it in the browser: Enter = I submitted it, s = skip: ")
            return "submitted" if r == "" else "skipped"
        if nxt:
            nxt.click(timeout=10000)
            page.wait_for_timeout(2500)
            errors = _form_errors(page)
            if errors:
                print("    The page says: " + " | ".join(errors)[:300])
                ask("    Fix that in the browser, then press Enter... ")
            continue
        if not auto_submit and ask("    Ready to submit. Enter = submit, s = skip: ") == "s":
            return "skipped"
        sub.click(timeout=10000)
        deadline = time.time() + 25
        while time.time() < deadline:
            page.wait_for_timeout(1500)
            try:
                if CONFIRM.search(page.inner_text("body", timeout=3000)):
                    return "submitted"
            except Exception:  # noqa: BLE001
                pass
        errors = _form_errors(page)
        if errors:
            print("    The page says: " + " | ".join(errors)[:300])
            ask("    Fix that in the browser (and press Submit), then press Enter... ")
        return "unconfirmed"
    return "skipped"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jobbot.careers", description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("login", "apply"))
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--only", default="", help="comma-separated: " + ", ".join(SOURCES))
    ap.add_argument("--confirm", action="store_true", help="ask before pressing each final Submit")
    ap.add_argument("--no-sync", action="store_true", help="use the local .state/state.enc and do not publish changes")
    args = ap.parse_args(argv)

    from playwright.sync_api import sync_playwright

    from .config import load_config, load_profile

    cfg, profile = load_config(), load_profile()
    only = [s.strip() for s in args.only.split(",") if s.strip()] or list(SOURCES)
    scfg = {k: v for k, v in cfg["sources"].items() if k in only}
    with sync_playwright() as pw:
        context = open_profile(pw)
        if args.command == "login":
            for url in login_pages(scfg):
                p = context.new_page()
                try:
                    p.goto(url, wait_until="domcontentloaded", timeout=60000)
                except Exception:  # noqa: BLE001
                    pass
            print("Each site is open in its own tab. Sign in, or create the account (Workday: 'Sign In' /\n"
                  "'Create Account' at the top right - every company is a separate account). Use one\n"
                  "password manager entry per site. The browser remembers you for later runs.")
            input("Press Enter here when you are done... ")
            context.close()
            return 0
        rc = _apply(context, pw, cfg, scfg, profile, args)
        context.close()
    return rc


def _apply(context, pw, cfg, scfg, profile, args):
    from .cover import cover_letter
    from .sync import STATE, sync_down, sync_up
    from .llm import make_llm
    from .match import select
    from .render import render_letter_pdf, render_resume
    from .sources import crawl
    from .state import State
    from .tailor import tailor

    if not args.no_sync and not sync_down():
        print("Could not fetch the bot's memory; continuing with a local copy (it will not be published).")
        args.no_sync = True
    state = State(STATE, profile["state_key"])
    learned = state.meta.setdefault("learned", {})
    acfg = dict(cfg["apply"], llm_answer_questions=cfg["llm"].get("answer_questions", True),
                memory=state.meta.setdefault("answers", {}))
    llm = make_llm(cfg["llm"], profile)
    reapply = int(cfg["apply"].get("reapply_after_days", 7))
    print("Finding matching postings on: " + ", ".join(scfg) + " ...")
    jobs = [j for j in crawl(scfg) if j.source in SITES]
    picks = select(jobs, profile, cfg, lambda j: not pending(state, j, reapply))
    per_company, chosen = {}, []
    cap = int(cfg["apply"].get("max_per_company", 5))
    for j in picks:
        if per_company.get(j.company, 0) < cap:
            per_company[j.company] = per_company.get(j.company, 0) + 1
            chosen.append(j)
    chosen = chosen[:args.limit]
    print(f"{len(picks)} matching posting(s); applying to {len(chosen)}.")
    out_dir = os.path.join(ROOT, "out", "careers")
    name = re.sub(r"\W+", "_", profile["name"]).strip("_")
    renderer = make_renderer(pw)
    page = work_page(context)
    tally = {}
    for n, job in enumerate(chosen, 1):
        print(f"\n[{n}/{len(chosen)}] {job.title} - {job.company} ({job.location})")
        jd = os.path.join(out_dir, f"{job.source}_{re.sub(r'[^A-Za-z0-9]+', '_', job.id)[-40:]}")
        os.makedirs(jd, exist_ok=True)
        status = "error"
        try:
            tailored, _ = tailor(profile, job, llm)
            pdf = render_resume(renderer, profile, tailored, job, os.path.join(jd, f"{name}_Resume.pdf"))
            letter, _ = cover_letter(profile, tailored, job, llm)
            letter_pdf = render_letter_pdf(renderer, tailored, letter, os.path.join(jd, f"{name}_Cover_Letter.pdf"))
            app = enter(context, page, job)
            if app is None:
                status = "closed"
            else:
                status = walk(app, job, profile, learned, pdf, letter_pdf, letter, acfg, llm, not args.confirm)
                if app is not page:
                    app.close()
        except KeyboardInterrupt:
            break
        except Exception as e:  # noqa: BLE001
            print(f"    {type(e).__name__}: {str(e)[:120]}")
        tally[status] = tally.get(status, 0) + 1
        print(f"    -> {status}")
        if status in ("submitted", "unconfirmed"):
            state.record(job, status)
            if not args.no_sync and sum(tally.get(k, 0) for k in ('submitted', 'unconfirmed')) % 10 == 0:
                state.save()
                sync_up(profile["state_key"])  # every 10: share progress with the other lanes as you go
        elif status == "closed":
            state.record(job, "dismissed")
        state.save()
    renderer.close()
    if not args.no_sync:
        print("\nSaving the bot's memory..." + (" done." if sync_up(profile["state_key"]) else " FAILED - run again later."))
    print("\n" + ", ".join(f"{k}: {v}" for k, v in sorted(tally.items())) if tally else "\nNothing to do.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
