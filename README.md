# job_crawler

A bot that runs for free on GitHub Actions: it crawls job boards, tailors your
resume to each matching job, and applies for you where the application form
allows it. Whatever it cannot submit lands in a short report with the tailored
PDF and the link, so finishing by hand takes a minute.

## What it does on every run

1. **Crawl** – public JSON APIs of company boards on Greenhouse, Lever and Ashby
   (list in `config.yml`), plus the Remotive, RemoteOK and Arbeitnow aggregators.
2. **Match** – job title must contain one of your target titles, location must
   fit, then jobs are ranked by overlap with your skills. Jobs handled before
   (or the same role in another city) are skipped.
3. **Tailor** – reorders your skills and picks the most relevant bullets for
   each job, and rewrites the two-line summary. It can only *select and reorder
   what is already in your profile*; a summary that mentions anything your
   profile lacks is thrown away. Uses GitHub Models (free in Actions), with a
   keyword-based fallback.
4. **Cover letter** – written per job under the same rule (facts from your
   profile only; anything else falls back to a plain template), as PDF and text.
5. **Apply** – opens the Greenhouse/Lever/Ashby form in headless Chromium, fills
   it from your profile and your `answers`, uploads the tailored resume and
   cover letter and submits – but only if every required question has an answer.
6. **Notify** – a WhatsApp alert the moment a form needs you (CAPTCHA,
   verification code), a WhatsApp daily log after every run, an encrypted zip
   (workflow artifact) and, optionally, an email with the PDFs attached.

## Setup (about 10 minutes, once)

1. Copy `profile.example.yml`, fill in your details, and paste the whole file
   into a repository secret named **`PROFILE_YAML`**
   (Settings → Secrets and variables → Actions → New repository secret).
   Pick your own long `state_key`.
2. Run the **jobbot** workflow from the Actions tab. It starts in dry-run mode:
   forms are filled but nothing is submitted.
3. Download the `jobbot-report` artifact from the run and open it with your
   `state_key` as the zip password (use 7-Zip / Keka / `7z x`; the zip is
   AES-encrypted). Check the tailored PDFs and the "Teach the bot" list.
4. When it looks right, set `apply.dry_run: false` in `config.yml`. From then on
   it runs every weekday by itself.

That is the only required manual input. Optional: WhatsApp (below), an `smtp:`
block in the profile to get the report and PDFs by email, and your own company
lists in `config.yml`.

### WhatsApp alerts and daily log (2 minutes, once)

Uses [CallMeBot](https://www.callmebot.com/blog/free-api-whatsapp-messages/),
which is free for personal use.

1. From your phone, send the WhatsApp message `I allow callmebot to send me messages`
   to the CallMeBot number shown on that page. It replies with an API key.
2. Add to your `PROFILE_YAML` secret:
   ```yaml
   whatsapp:
     phone: "+911234567890"
     apikey: "the-key-you-received"
   ```

You then get:

- **an alert as soon as a form stops at a CAPTCHA or asks for an emailed code**,
  with the job and the link to finish it;
- **a daily log** after each run: what was submitted, what needs you (with
  links), and which questions to add to `answers:`.

A CAPTCHA cannot be solved *through* WhatsApp: it is bound to the browser
session on GitHub's runner, which is gone by the time you read the message. The
alert is your cue to open the link and finish that one application yourself;
the tailored resume and cover letter are in the report (add `smtp:` to have
them in your inbox, which is the easiest way to get them onto your phone).

## What will still need you

No bot can honestly promise 100% unattended applications:

| Situation | What the bot does |
|---|---|
| Form shows a CAPTCHA or asks for an emailed verification code | Stops, puts the job in "Needs you". It never tries to defeat these. |
| Required question it cannot answer from your profile | Stops and lists the question under "Teach the bot". Add one regex to `answers:` and that question is automated from then on. |
| Aggregator job that links to some other website | Tailors the resume and gives you the link. |
| LinkedIn, Indeed, Workday and other login-walled sites | Not supported: their terms forbid automation and accounts get banned. |

Expect the manual share to shrink over the first week as your `answers:` grow.

## Privacy in a public repo

- Your resume, search criteria and answers exist only in the `PROFILE_YAML` secret.
- The bot's memory (`bot-state` branch) and the report artifact are encrypted
  with your `state_key`.
- Workflow logs contain counts only – no names, companies or questions.
- The workflow has no pull-request trigger, so forks cannot reach the secret.

## Cost

$0. Actions minutes are free for public repositories, GitHub Models has a free
quota for the built-in token, and all job sources are free public APIs. If the
model quota runs out the bot falls back to keyword tailoring. Use is still
subject to GitHub's Actions terms and each site's terms; the bot is deliberately
low-volume (`max_per_run`, `delay_seconds`).

## Running locally

```bash
pip install -r requirements.txt pytest && playwright install chromium
python -m pytest -q                 # unit tests + form-filling tests on local fixtures
python -m jobbot --demo --limit 3   # fake profile against live boards, never submits
python -m jobbot --dry-run          # your profile.yml (git-ignored), never submits
```

## Layout

| Path | Purpose |
|---|---|
| `config.yml` | sources, thresholds, caps, dry-run switch |
| `profile.example.yml` | template for the `PROFILE_YAML` secret |
| `jobbot/sources.py` | job board API clients |
| `jobbot/match.py` | filtering and scoring |
| `jobbot/tailor.py` | truthful resume tailoring |
| `jobbot/apply.py` | label-driven form filler and submitter |
| `jobbot/cover.py` | cover letters |
| `jobbot/notify.py` | WhatsApp alerts and daily log |
| `jobbot/state.py`, `jobbot/report.py` | encrypted memory, report, email |
| `.github/workflows/jobbot.yml` | the scheduled run |
| `.github/workflows/ci.yml` | tests plus a dry-run canary against live boards |
