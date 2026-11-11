"""WhatsApp notifications through CallMeBot (free for personal use).

One-time activation by the phone's owner: see README. The phone number and API
key live only in the PROFILE_YAML secret and are never logged.
"""
import requests

from .util import log

URL = "https://api.callmebot.com/whatsapp.php"
CHUNK = 900


def whatsapp(profile, text, max_messages=4):
    """Send text to the user's WhatsApp. Returns True if every part was accepted."""
    cfg = profile.get("whatsapp") or {}
    if not (cfg.get("phone") and cfg.get("apikey")):
        return False
    chunks, current = [], ""
    for line in text.split("\n"):  # split on line boundaries so links stay intact
        if current and len(current) + len(line) + 1 > CHUNK:
            chunks.append(current)
            current = ""
        current = (current + "\n" + line) if current else line[:CHUNK]
    chunks.append(current)
    if len(chunks) > max_messages:
        chunks = chunks[:max_messages]
        chunks[-1] += "\n... (more in the report)"
    ok = True
    for chunk in chunks:
        try:
            r = requests.get(URL, timeout=30, params={
                "phone": str(cfg["phone"]).replace(" ", ""), "apikey": str(cfg["apikey"]), "text": chunk})
            ok = ok and r.status_code == 200
        except Exception as e:  # noqa: BLE001 - the URL holds the phone number: log the type only
            log(f"whatsapp failed: {type(e).__name__}")
            return False
    if not ok:
        log("whatsapp: message not accepted (check phone/apikey activation)")
    return ok


def alert(profile, job, outcome):
    """Immediate ping for something only a human can finish."""
    what = {"captcha": "CAPTCHA to solve", "verification": "Email verification code needed",
            "unconfirmed": "Submitted but not confirmed - please check"}.get(outcome.status)
    if not what:
        return False
    return whatsapp(profile, f"jobbot: {what}\n{job.title} - {job.company}\nFinish here: {job.url}\n"
                             "(the form is short; your tailored resume + cover letter are in today's report)")


def daily_log(profile, results, dry_run, llm_count):
    """End-of-run digest. results: list of (job, outcome, pdf, used_llm)."""
    sent = [r for r in results if r[1].status == "submitted"]
    ready = [r for r in results if r[1].status == "dry_run_ok"]
    todo = [r for r in results if r[1].status not in ("submitted", "dry_run_ok")]
    why = {"needs_answers": "unanswered question", "captcha": "captcha", "verification": "email code",
           "unconfirmed": "check inbox", "manual": "apply on site", "error": "error"}
    lines = [f"jobbot daily log{' (DRY RUN - nothing sent)' if dry_run else ''}",
             f"Submitted: {len(sent)} | Need you: {len(todo)}" + (f" | Ready when live: {len(ready)}" if ready else "")]
    if not results:
        lines.append("No new matching jobs today.")
    for title, rows in (("Submitted", sent), ("Ready when live", ready)):
        if rows:
            lines += ["", f"{title}:"] + [f"- {j.title} - {j.company}" for j, *_ in rows]
    if todo:
        lines += ["", "Need you:"]
        lines += [f"- {j.title} - {j.company} ({why.get(o.status, o.status)})\n  {j.url}" for j, o, *_ in todo]
    questions = sorted({q for _, o, *_ in results for q in o.unanswered})
    if questions:
        lines += ["", "Add answers for:"] + [f"- {q[:90]}" for q in questions[:8]]
    lines += ["", f"Resumes tailored by AI: {llm_count}/{len(results)}"]
    return whatsapp(profile, "\n".join(lines))
