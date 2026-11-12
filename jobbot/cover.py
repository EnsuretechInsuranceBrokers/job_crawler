"""Cover letter per job. Model-written when it stays within the profile's facts, otherwise a plain template."""
from .tailor import grounded

SYSTEM = """Write a cover letter for the candidate applying to the job below.
Rules: 150-220 words, plain text, three short paragraphs, no placeholders or brackets, no address block or date.
Use ONLY facts from the candidate profile - never claim a skill, tool, employer, degree, number or year that is not in it.
Connect two or three of the candidate's real achievements to what the posting asks for. Confident, concrete, no cliches.
Start with the given greeting line and end with "Sincerely," and the candidate's name on the next line."""


def template(tailored, job):
    """Deterministic letter built only from the (already tailored) profile."""
    where = f" at {job.company}" if job.named else ""
    recent = tailored["experience"][0]
    bullets = (recent.get("bullets") or [])[:2]
    parts = [greeting(job), "",
             f"I am writing to apply for the {job.title} position{where}. {(tailored.get('summary') or '').strip()}".strip(), ""]
    if bullets:
        parts += [f"Some highlights of my work, most recently as {recent.get('title')} at {recent.get('company')}:"]
        parts += [f"- {b}" for b in bullets] + [""]
    if tailored.get("skills"):
        parts += [f"The skills I would bring include {', '.join(tailored['skills'][:8])}.", ""]
    parts += ["I would welcome the chance to discuss how I can contribute to your team. Thank you for your consideration.",
              "", "Sincerely,", tailored["name"]]
    return "\n".join(parts)


def greeting(job):
    return f"Dear {job.company} hiring team," if job.named else "Dear hiring team,"


def cover_letter(profile, tailored, job, llm=None):
    """Returns (text, written_by_llm)."""
    if llm is not None and llm.enabled:
        import json
        facts = {k: tailored.get(k) for k in ("name", "summary", "skills", "experience", "projects", "education")}
        text = llm.ask(SYSTEM, f"GREETING: {greeting(job)}\nJOB: {job.title}\n{job.description[:5000]}\n\n"
                               f"CANDIDATE:\n{json.dumps(facts, default=str)}")
        if isinstance(text, str):
            text = text.strip().strip("`").strip()
            ok = 500 <= len(text) <= 2200 and "[" not in text and profile["name"] in text
            if ok and grounded(text, profile, job, max_len=2200, letter=True):
                return text, True
    return template(tailored, job), False
