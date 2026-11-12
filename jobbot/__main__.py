import argparse
import os
import re
import sys
import time
import traceback

from . import util
from .util import detail, log


def run(args):
    from playwright.sync_api import sync_playwright

    from . import notify, report
    from .cover import cover_letter
    from .apply import apply_to, apply_url
    from .config import ROOT, load_config, load_profile
    from .llm import LLM
    from .match import select
    from .models import Outcome
    from .render import render_letter_pdf, render_pdf
    from .sources import crawl
    from .state import State
    from .tailor import tailor

    cfg = load_config()
    profile = load_profile(args.demo)
    acfg = dict(cfg["apply"], llm_answer_questions=cfg["llm"].get("answer_questions", True))
    dry = bool(acfg.get("dry_run", True) or args.dry_run or args.demo or os.environ.get("JOBBOT_DRY_RUN") == "1")
    out_dir = os.path.join(ROOT, "out")
    os.makedirs(out_dir, exist_ok=True)
    state = State(os.path.join(ROOT, ".state", "demo.enc" if args.demo else "state.enc"), profile["state_key"])
    acfg["memory"] = state.meta.setdefault("answers", {})   # remembered multiple-choice answers
    llm = LLM(cfg["llm"], profile)
    log(f"mode: {'DRY RUN' if dry else 'LIVE'}{' (demo profile)' if args.demo else ''}; "
        f"llm: {'on' if llm.enabled else 'off'}; remembered jobs: {len(state.jobs)}")

    reapply = int(acfg.get("reapply_after_days", 0) or 0)
    picks = select(crawl(cfg["sources"]), profile, cfg, lambda job: state.seen(job, reapply))
    log(f"new matching jobs: {len(picks)}")

    # Choose what to handle this run, best score first, within the caps.
    chosen, per_company, n_auto, n_manual = [], {}, 0, 0
    max_auto = args.limit or acfg["max_per_run"]
    for job in picks:
        company = job.company.lower()
        if per_company.get(company, 0) >= acfg.get("max_per_company", 2):
            continue
        auto = apply_url(job) is not None
        if auto and n_auto < max_auto:
            n_auto += 1
        elif not auto and n_manual < (args.limit or acfg.get("max_manual_per_run", 10)):
            n_manual += 1
        else:
            continue
        per_company[company] = per_company.get(company, 0) + 1
        chosen.append((job, auto))

    results = []
    started, budget = time.time(), 60 * float(acfg.get("max_minutes", 70))
    file_name = re.sub(r"\W+", "_", profile["name"]).strip("_") + "_Resume.pdf"
    letter_name = file_name.replace("_Resume.pdf", "_Cover_Letter.pdf")
    with sync_playwright() as pw:
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")  # honour a corporate/sandbox proxy
        browser = pw.chromium.launch(executable_path=os.environ.get("JOBBOT_CHROMIUM") or None,
                                     proxy={"server": proxy} if proxy else None)
        context = browser.new_context(locale="en-US", viewport={"width": 1280, "height": 1600})
        for n, (job, auto) in enumerate(chosen, 1):
            if time.time() - started > budget:
                # Leave time to save state and upload the report; the rest is picked up next run.
                log(f"time budget reached after {n - 1} of {len(chosen)} jobs; remaining jobs wait for the next run")
                break
            detail(f"[{n}/{len(chosen)}] {job.title} @ {job.company} ({job.source}, score {job.score})")
            pdf, letter_pdf, used_llm = None, None, False
            try:
                tailored, used_llm = tailor(profile, job, llm)
                job_dir = os.path.join(out_dir, f"{n:02d}_" + re.sub(r"\W+", "_", job.company)[:30])
                os.makedirs(job_dir, exist_ok=True)
                pdf = render_pdf(browser, tailored, os.path.join(job_dir, file_name))
                letter, _ = cover_letter(profile, tailored, job, llm)
                with open(os.path.join(job_dir, "cover_letter.txt"), "w") as f:
                    f.write(letter)
                if auto and not dry:
                    state.record(job, "attempting")
                    state.save()
                letter_pdf = render_letter_pdf(browser, tailored, letter, os.path.join(job_dir, letter_name))
                outcome = apply_to(context, job, profile, pdf, acfg, llm, dry, cover=(letter_pdf, letter)) if auto else \
                    Outcome("manual", "site is not auto-appliable")
            except Exception as e:  # noqa: BLE001
                outcome = Outcome("error", type(e).__name__)
            detail(f"   -> {outcome.status}: {outcome.detail}" + "".join(f"\n      ? {q}" for q in outcome.unanswered))
            results.append((job, outcome, pdf, used_llm))
            if not dry:
                state.record(job, outcome.status)
                state.save()  # after every job, so a later crash loses nothing
                notify.alert(profile, job, outcome, [x for x in (pdf, letter_pdf) if x], state)
                if auto and n < len(chosen):
                    time.sleep(acfg.get("delay_seconds", 20))
        browser.close()

    counts = {}
    for _, o, _, _ in results:
        counts[o.status] = counts.get(o.status, 0) + 1
    summary = {"submitted": counts.get("submitted", 0),
               "todo": sum(v for k, v in counts.items() if k not in ("submitted", "dry_run_ok"))}
    asked = state.meta.setdefault("unanswered", {})         # how often each open question has come up
    for _, o, _, _ in results:
        for q in o.unanswered:
            asked[q] = asked.get(q, 0) + 1
    text = report.build(results, dry, out_dir, asked)
    report.dashboard(state, results, dry, out_dir)
    log("results: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing to do"))
    log(f"resumes tailored by LLM: {sum(1 for r in results if r[3])}/{len(results)}; "
        f"model calls answered: {llm.answered}/{llm.calls}; took {int((time.time() - started) / 60)} min")
    if util.VERBOSE:
        print(text, file=sys.stderr)

    if not args.demo:
        report.encrypt_zip(out_dir, profile["state_key"])
        if notify.daily_log(profile, results, dry, sum(1 for r in results if r[3]), state):
            log("daily log sent to phone")
        state.save()  # dry runs record no jobs, so this only keeps settings such as the Telegram chat
        if profile.get("smtp") and results:
            try:
                report.send_email(profile["smtp"], text, results, summary)
                log("report emailed")
            except Exception as e:  # noqa: BLE001
                log(f"email failed: {type(e).__name__}")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(f"### jobbot ({'dry run' if dry else 'live'})\n\n| outcome | count |\n|---|---|\n"
                    + "".join(f"| {k} | {v} |\n" for k, v in sorted(counts.items()))
                    + "\nDetails are in the encrypted `jobbot-report` artifact (password = your `state_key`).\n")


def main():
    ap = argparse.ArgumentParser(prog="jobbot")
    ap.add_argument("--demo", action="store_true", help="use profile.example.yml, never submit, verbose output")
    ap.add_argument("--dry-run", action="store_true", help="fill forms but never submit")
    ap.add_argument("--limit", type=int, default=0, help="cap jobs handled this run")
    args = ap.parse_args()
    if args.demo:
        util.VERBOSE = True
    try:
        run(args)
    except Exception as e:  # noqa: BLE001
        if util.VERBOSE:
            raise
        # Logs of a public repo are public: print where it failed, never the message (it may hold personal data).
        frames = " <- ".join(f"{os.path.basename(f.filename)}:{f.lineno}" for f in reversed(traceback.extract_tb(e.__traceback__)[-4:]))
        log(f"fatal: {type(e).__name__} at {frames}")
        sys.exit(1)


if __name__ == "__main__":
    main()
