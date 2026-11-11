"""Phone notifications: alerts when a form needs a human, and a daily log.

Channels (use any, all are free; credentials live only in the PROFILE_YAML secret):
  telegram - bot token from @BotFather; can also deliver the tailored PDFs
  ntfy     - push via the ntfy app, no account: just a secret topic name
  whatsapp - CallMeBot, when it has free slots
Nothing here logs message text, phone numbers, tokens or topics.
"""
import os

import requests

from .util import log

CHUNK = 900


def _chunks(text, max_messages):
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
    return chunks


def _telegram_chat(cfg, state):
    """chat_id from the profile, the saved state, or the bot's most recent private message."""
    if cfg.get("chat_id"):
        return cfg["chat_id"]
    if state is not None and state.meta.get("telegram_chat_id"):
        return state.meta["telegram_chat_id"]
    r = requests.get(f"https://api.telegram.org/bot{cfg['token']}/getUpdates", timeout=30)
    chats = [u["message"]["chat"]["id"] for u in r.json().get("result", []) if u.get("message", {}).get("chat", {}).get("type") == "private"]
    if chats and state is not None:
        state.meta["telegram_chat_id"] = chats[-1]
    return chats[-1] if chats else None


def _telegram(cfg, chunks, files, state):
    chat = _telegram_chat(cfg, state)
    if not chat:
        log("telegram: send any message to your bot once so it learns your chat")
        return False
    base = f"https://api.telegram.org/bot{cfg['token']}"
    ok = True
    for chunk in chunks:
        r = requests.post(f"{base}/sendMessage", timeout=30,
                          json={"chat_id": chat, "text": chunk, "disable_web_page_preview": True})
        ok = ok and r.status_code == 200
    for path in files:
        with open(path, "rb") as fh:
            r = requests.post(f"{base}/sendDocument", timeout=60, data={"chat_id": chat},
                              files={"document": (os.path.basename(path), fh, "application/pdf")})
        ok = ok and r.status_code == 200
    return ok


def _ntfy(cfg, chunks):
    url = f"{str(cfg.get('server') or 'https://ntfy.sh').rstrip('/')}/{cfg['topic']}"
    return all(requests.post(url, data=c.encode(), timeout=30, headers={"Title": "jobbot"}).status_code == 200 for c in chunks)


def _whatsapp(cfg, chunks):
    return all(requests.get("https://api.callmebot.com/whatsapp.php", timeout=30, params={
        "phone": str(cfg["phone"]).replace(" ", ""), "apikey": str(cfg["apikey"]), "text": c}).status_code == 200 for c in chunks)


def send(profile, text, files=(), state=None, max_messages=4):
    """Send to every configured channel. Returns True if at least one accepted it."""
    chunks = _chunks(text, max_messages)
    channels = []
    tg, nt, wa = (profile.get(k) or {} for k in ("telegram", "ntfy", "whatsapp"))
    if tg.get("token"):
        channels.append(("telegram", lambda: _telegram(tg, chunks, files, state)))
    if nt.get("topic"):
        channels.append(("ntfy", lambda: _ntfy(nt, chunks)))
    if wa.get("phone") and wa.get("apikey"):
        channels.append(("whatsapp", lambda: _whatsapp(wa, chunks)))
    delivered = False
    for name, fn in channels:
        try:
            ok = fn()
        except Exception as e:  # noqa: BLE001 - URLs hold tokens: log the type only
            log(f"{name} failed: {type(e).__name__}")
            ok = False
        if not ok:
            log(f"{name}: message not accepted (check its settings in your profile)")
        delivered = delivered or ok
    return delivered


ALERTS = {"captcha": "CAPTCHA to solve", "verification": "Email verification code needed",
          "unconfirmed": "Submitted but not confirmed - please check"}


def alert(profile, job, outcome, files=(), state=None):
    """Immediate ping for something only a human can finish."""
    what = ALERTS.get(outcome.status)
    if not what:
        return False
    text = f"jobbot: {what}\n{job.title} - {job.company}\nFinish here: {job.url}"
    sent = send(profile, text, files, state)
    if profile.get("smtp"):  # email carries the tailored PDFs, handy on a phone
        try:
            from .report import send_email
            send_email(profile["smtp"], text, [(job, outcome, files[0] if files else None, False)], subject=f"jobbot: {what}")
            sent = True
        except Exception as e:  # noqa: BLE001
            log(f"alert email failed: {type(e).__name__}")
    return sent


def daily_log(profile, results, dry_run, llm_count, state=None):
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
    return send(profile, "\n".join(lines), state=state)
