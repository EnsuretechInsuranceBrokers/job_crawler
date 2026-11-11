"""Run report: markdown, AES-encrypted zip for the public artifact, optional email."""
import os
import smtplib
import time
from email.message import EmailMessage

import pyzipper

LABELS = {"needs_answers": "form has questions your profile cannot answer", "captcha": "CAPTCHA",
          "verification": "email verification code required", "unconfirmed": "submitted, but no confirmation seen",
          "manual": "site does not support auto-apply", "error": "error"}


def build(results, dry_run, out_dir):
    """results: list of (job, outcome, pdf_path, used_llm)."""
    sent = [r for r in results if r[1].status == "submitted"]
    ready = [r for r in results if r[1].status == "dry_run_ok"]
    todo = [r for r in results if r[1].status not in ("submitted", "dry_run_ok")]
    lines = [f"# jobbot report - {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}", "",
             "**Mode: DRY RUN - nothing was submitted.** Set `apply.dry_run: false` in config.yml to go live."
             if dry_run else "**Mode: LIVE**", ""]

    def row(job, outcome, pdf, _):
        rel = os.path.relpath(pdf, out_dir) if pdf else "-"
        return f"- **{job.title}** - {job.company} ({job.location or 'n/a'}), score {job.score}\n  - {job.url}\n  - files: `{os.path.dirname(rel)}/` (resume + cover letter)"

    if sent:
        lines += [f"## Submitted automatically ({len(sent)})", ""] + [row(*r) for r in sent] + [""]
    if ready:
        lines += [f"## Would be submitted when live ({len(ready)})", ""] + [row(*r) for r in ready] + [""]
    if todo:
        lines += [f"## Needs you ({len(todo)})", "", "Open the link, upload the tailored PDF, submit.", ""]
        for job, outcome, pdf, llm in todo:
            lines.append(row(job, outcome, pdf, llm))
            lines.append(f"  - why: {LABELS.get(outcome.status, outcome.status)}" + (f" ({outcome.detail})" if outcome.status == "error" else ""))
            lines += [f"  - unanswered: {q}" for q in outcome.unanswered]
        lines.append("")
    questions = sorted({q for _, o, _, _ in results for q in o.unanswered})
    if questions:
        lines += ["## Teach the bot", "", "Add a regex for each of these to `answers:` in your PROFILE_YAML secret "
                  "and similar forms will go through unattended:", ""] + [f"- {q}" for q in questions] + [""]
    audit = [(job, q, a) for job, o, _, _ in results for q, a in o.llm_answers]
    if audit:
        lines += ["## Answers written by the model (please audit)", ""]
        lines += [f"- {job.company}: **{q}** -> {a}" for job, q, a in audit] + [""]
    if not results:
        lines.append("No new matching jobs this run.")
    text = "\n".join(lines)
    with open(os.path.join(out_dir, "report.md"), "w") as f:
        f.write(text)
    return text


def encrypt_zip(out_dir, password, name="jobbot-report.zip"):
    """Artifacts of a public repo are downloadable by anyone, so encrypt them."""
    path = os.path.join(out_dir, name)
    with pyzipper.AESZipFile(path, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as z:
        z.setpassword(str(password).encode())
        for root, _, files in os.walk(out_dir):
            for fn in files:
                full = os.path.join(root, fn)
                if full != path:
                    z.write(full, os.path.relpath(full, out_dir))
    return path


def send_email(smtp, text, results, counts=None, subject=None):
    msg = EmailMessage()
    msg["Subject"] = subject or f"jobbot: {counts['submitted']} submitted, {counts['todo']} need you"
    msg["From"], msg["To"] = smtp["user"], smtp.get("to") or smtp["user"]
    msg.set_content(text)
    size = 0
    for i, (job, outcome, pdf, _) in enumerate(results):
        if outcome.status in ("submitted", "dry_run_ok") or not pdf or size > 15e6:
            continue
        safe = "".join(c if c.isalnum() else "_" for c in job.company)[:30]
        for fn in sorted(os.listdir(os.path.dirname(pdf))):
            if fn.endswith(".pdf"):
                with open(os.path.join(os.path.dirname(pdf), fn), "rb") as f:
                    data = f.read()
                size += len(data)
                kind = "cover_letter" if "Cover" in fn else "resume"
                msg.add_attachment(data, maintype="application", subtype="pdf", filename=f"{i + 1:02d}_{safe}_{kind}.pdf")
    port = int(smtp.get("port", 587))
    cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
    with cls(smtp["host"], port, timeout=60) as s:
        if port != 465:
            s.starttls()
        s.login(smtp["user"], smtp["password"])
        s.send_message(msg)
