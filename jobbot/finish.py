"""Pick up applications where the bot had to stop - on your own computer.

    JOBBOT_KEY=<key> python -m jobbot.finish                 # everything that is waiting
    JOBBOT_KEY=<key> python -m jobbot.finish --only verification,captcha --limit 10

For each waiting application this opens a real browser window, fills the form
again in a few seconds (same answers, a freshly tailored resume and cover
letter), and then hands the window to you at exactly the point the bot could
not pass: the emailed code, the CAPTCHA, or a question only you can answer.
You do that part and press Submit. Nothing is submitted without you.

Do not run it while the scheduled bot is running: both write the same memory.
"""
import argparse
import os
import re
import subprocess
import sys
import time

from .config import ROOT

WAITING = ("verification", "captcha", "rejected", "unconfirmed", "needs_answers", "error")
STATE = os.path.join(ROOT, ".state", "state.enc")


def _git(*args, cwd=ROOT):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def sync_down():
    """Fetch the bot's encrypted memory from the bot-state branch."""
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    if _git("fetch", "-q", "--depth=1", "origin", "bot-state").returncode != 0:
        return False
    blob = subprocess.run(["git", "show", "FETCH_HEAD:state.enc"], cwd=ROOT, capture_output=True)
    if blob.returncode != 0 or not blob.stdout:
        return False
    with open(STATE, "wb") as f:
        f.write(blob.stdout)
    return True


def sync_up():
    """Publish the updated memory the same way the workflow does (single commit, bot-state branch)."""
    url = _git("remote", "get-url", "origin").stdout.strip()
    d = os.path.dirname(STATE)
    for step in (["init", "-q", "-b", "bot-state"], ["add", "state.enc"],
                 ["-c", "user.name=jobbot", "-c", "user.email=jobbot@users.noreply.github.com", "commit", "-qm", "encrypted state"],
                 ["push", "-qf", url, "bot-state"]):
        r = _git(*step, cwd=d)
        if r.returncode != 0 and step[0] != "init":
            return False
    return True


def waiting(state, only=None):
    """(key, record) of applications that still need a human, newest first."""
    kinds = set(only or WAITING)
    rows = [(k, v) for k, v in state.jobs.items() if v.get("status") in kinds]
    return sorted(rows, key=lambda kv: -kv[1]["ts"])


def job_from_record(key, record, live):
    """Rebuild the Job for a remembered application. `live` maps key -> freshly crawled Job;
    a posting that is no longer listed returns None (it has closed)."""
    return live.get(key)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jobbot.finish", description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", default="", help="comma-separated reasons: " + ", ".join(WAITING))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-sync", action="store_true", help="use the local .state/state.enc and do not publish changes")
    args = ap.parse_args(argv)

    from playwright.sync_api import sync_playwright

    from .apply import CONFIRM, apply_url, fill_form
    from .config import load_config, load_profile
    from .cover import cover_letter
    from .llm import LLM
    from .render import render_letter_pdf, render_pdf
    from .sources import crawl
    from .state import State
    from .tailor import tailor

    cfg, profile = load_config(), load_profile()
    if not args.no_sync and not sync_down():
        print("Could not fetch the bot's memory (bot-state branch). Has the bot run yet? Use --no-sync to work from a local copy.")
        return 1
    state = State(STATE, profile["state_key"])
    rows = waiting(state, [x.strip() for x in args.only.split(",") if x.strip()] or None)
    if args.limit:
        rows = rows[:args.limit]
    if not rows:
        print("Nothing is waiting for you.")
        return 0
    print(f"{len(rows)} application(s) waiting. Checking which postings are still open...")
    live = {j.key: j for j in crawl(cfg["sources"])}
    llm = LLM(cfg["llm"], profile)
    acfg = dict(cfg["apply"], llm_answer_questions=cfg["llm"].get("answer_questions", True),
                memory=state.meta.setdefault("answers", {}))
    out_dir = os.path.join(ROOT, "out", "finish")
    name = re.sub(r"\W+", "_", profile["name"]).strip("_")
    done = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, executable_path=os.environ.get("JOBBOT_CHROMIUM") or None)
        context = browser.new_context(locale="en-US", viewport=None)
        for n, (key, rec) in enumerate(rows, 1):
            job = job_from_record(key, rec, live)
            print(f"\n[{n}/{len(rows)}] {rec['title']} - {rec['company']}  (stopped: {rec['status']})")
            if job is None or not apply_url(job):
                print("    The posting is no longer listed - marking it closed.")
                rec["status"] = "closed"
                continue
            job_dir = os.path.join(out_dir, f"{n:02d}_" + re.sub(r"\W+", "_", job.company)[:30])
            os.makedirs(job_dir, exist_ok=True)
            tailored, _ = tailor(profile, job, llm)
            pdf = render_pdf(browser, tailored, os.path.join(job_dir, f"{name}_Resume.pdf"))
            letter, _ = cover_letter(profile, tailored, job, llm)
            letter_pdf = render_letter_pdf(browser, tailored, letter, os.path.join(job_dir, f"{name}_Cover_Letter.pdf"))
            page = context.new_page()
            try:
                filler, open_questions = fill_form(page, apply_url(job), job, profile, pdf, acfg, llm, (letter_pdf, letter))
            except Exception as e:  # noqa: BLE001
                print(f"    Could not open the form ({type(e).__name__}); skipping.")
                page.close()
                continue
            print(f"    Filled {filler.filled} fields. The browser window is yours now:")
            for q in open_questions:
                print(f"      - answer: {q}")
            print("      - solve any CAPTCHA / enter the emailed code if the page asks, then press Submit.")
            confirmed = False
            answer = ""
            while True:
                answer = input("    Enter = I submitted it   s = skip for now   x = never show again   q = quit : ").strip().lower()
                try:
                    confirmed = bool(CONFIRM.search(page.inner_text("body", timeout=3000)))
                except Exception:  # noqa: BLE001
                    confirmed = False
                if answer in ("", "s", "x", "q"):
                    break
            if answer == "":
                rec.update(status="submitted", ts=int(time.time()), rounds=rec.get("rounds", 0) + 1)
                done += 1
                print("    Recorded as submitted" + ("" if confirmed else " (no confirmation text was visible - check your inbox)") + ".")
            elif answer == "x":
                rec.update(status="dismissed", ts=int(time.time()))
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
            state.save()
            if answer == "q":
                break
        browser.close()
    state.save()
    if not args.no_sync:
        print("\nSaving the bot's memory..." + (" done." if sync_up() else " FAILED - run again later so the bot does not redo these."))
    print(f"{done} application(s) submitted by you this session.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
