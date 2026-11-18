"""The resume as the user's own LaTeX, tailored only by bolding what the job asks for.

The source lives in the encrypted profile (`resume_tex`). For each job:
- between the `% JOBBOT-SKILLS-BEGIN/END` markers, skills this posting mentions are bolded;
- in experience and project bullets, technologies the posting mentions are bolded.
The user's own bolding always stays: tailoring only ever adds \textbf.
Nothing is added, removed or reworded. If the tailored version runs past one page, the
untailored source is used; if LaTeX is not installed or fails, the caller falls back to HTML.
"""
import os
import re
import shutil
import subprocess
import tempfile

from .util import detail, has_term, norm

# Abbreviations used on the resume -> terms a posting would use
ALIASES = {
    "js/ts": ["javascript", "typescript"], "postgres": ["postgresql", "postgres"], "k8s": ["kubernetes", "k8s"],
    "drf": ["django rest framework", "drf"], "node": ["node.js", "nodejs"], "d3": ["d3.js", "d3"],
    "gh actions": ["github actions"], "protobuf": ["protocol buffers", "protobuf"], "mqs": ["message queue", "message queues"],
    "rest": ["rest", "restful", "rest api", "rest apis"], "html/css": ["html", "css"], "tsdbs": ["time series", "time-series"],
    "query opt.": ["query optimization", "query tuning"], "lsm/b-trees": ["lsm", "b-tree"], "eb": ["elastic beanstalk"],
    "x86 asm": ["assembly", "x86"], "queues/jobs": ["queues", "background jobs", "job queues"], "caching": ["caching", "cache"],
    "aws": ["aws", "amazon web services"], "ci/cd": ["ci/cd", "continuous integration", "continuous delivery"],
    "tracing": ["tracing", "distributed tracing"], "raft": ["raft", "consensus"], "spring boot": ["spring boot", "spring"],
    "hibernate/jpa": ["hibernate", "jpa"], "webgl/three.js": ["webgl", "three.js"], "ds": ["data structures"],
    "dp": ["dynamic programming"], "graphs": ["graph algorithms", "graphs"], "os internals": ["operating systems", "os internals"],
    "crypto basics": ["cryptography"], "memory mgmt": ["memory management"], "tech writing": ["technical writing", "documentation"],
    "react.js": ["react"], "next.js": ["next.js", "nextjs"],
}
SKILLS = re.compile(r"(% JOBBOT-SKILLS-BEGIN\n)(.*?)(% JOBBOT-SKILLS-END)", re.S)
TEXTBF = re.compile(r"\\textbf\{([^{}]*)\}")


def _wanted(item, job_text):
    key = norm(re.sub(r"\\", "", item)).strip(" .")
    for alias in ALIASES.get(key, [key]) + ([key] if key in ALIASES else []):
        if alias and has_term(alias, job_text):
            return True
    # "AWS (EC2, S3)" or "Hibernate/JPA": any named part the posting mentions
    raw = item.replace("\\", "").lower()
    parts = [norm(p) for p in re.split(r"[/(),]", raw) if len(p.strip()) > 1]
    return len(parts) > 1 and any(p and (has_term(p, job_text) or any(has_term(a, job_text) for a in ALIASES.get(p, [])))
                                  for p in parts)


def _split_top(s):
    """Split a skills list on commas outside parentheses and braces (a \\textbf{a, b} group stays whole)."""
    out, depth, cur = [], 0, ""
    for ch in s:
        depth += ch in "({"
        depth -= ch in ")}"
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    return out + [cur]


def _bold_skills(block, job_text, hits):
    def line(m):
        label, items = m.group(1), m.group(2)
        parts = _split_top(items)
        out = []
        for p in parts:
            lead = p[:len(p) - len(p.lstrip())]
            core = p.strip()
            if core and not core.startswith("\\textbf{") and _wanted(core, job_text):
                hits.append(core)
                out.append(f"{lead}\\textbf{{{core}}}")
            else:
                out.append(p)
        return label + ",".join(out) + "}"
    return re.sub(r"(\\cvitem(?:\[[^\]]*\])?\{\\skilllabel\{[^{}]*\}\}\{)(.*)\}[ \t]*$", line, block, flags=re.M)


PLAIN = re.compile(r"(\\[a-zA-Z]+\*?(?:\[[^\]]*\])?(?:\{[^{}]*\})*|\$[^$]*\$|\{[^{}]*\})")


def _bold_bullets(tex, terms, hits):
    """Bold the given terms where they occur as plain text on \\item lines."""
    if not terms:
        return tex
    pat = re.compile(r"(?<![A-Za-z0-9+#/.])(" + "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
                     + r")(?![A-Za-z0-9+#])", re.I)

    def item_line(m):
        pieces = PLAIN.split(m.group(2))
        for i in range(0, len(pieces), 2):  # even indexes are plain text; odd are commands, braces, math
            def mark(x):
                hits.append(x.group(1))
                return f"\\textbf{{{x.group(1)}}}"
            pieces[i] = pat.sub(mark, pieces[i])
        return m.group(1) + "".join(pieces)
    return re.sub(r"^(\s*\\item\s)(.*)$", item_line, tex, flags=re.M)


def tailor_tex(tex, job, profile):
    """(tailored source, list of bolded terms)."""
    job_text = norm(job.title + " " + job.description)
    hits = []
    m = SKILLS.search(tex)
    if m:
        tex = tex[:m.start(2)] + _bold_skills(m.group(2), job_text, hits) + tex[m.end(2):]
    skills = profile.get("skills") or []
    if isinstance(skills, dict):
        skills = [s for v in skills.values() for s in v]
    terms = {s for s in skills if len(s) > 2 and has_term(s, job_text) and not re.search(r"[\\{}$%&#_^~]", s)}
    tex = _bold_bullets(tex, terms, hits)
    return tex, sorted(set(hits), key=str.lower)


def _fontawesome_missing():
    try:
        return subprocess.run(["kpsewhich", "fontawesome5.sty"], capture_output=True, text=True, timeout=20).stdout.strip() == ""
    except (OSError, subprocess.SubprocessError):
        return True


STUB_ICONS = r"""\NeedsTeXFormat{LaTeX2e}
\ProvidesPackage{fontawesome5}[2099/01/01 jobbot stub: Font Awesome not installed]
\providecommand{\faCircle}[1][]{\textbullet}
\providecommand{\faIcon}[2][]{}
"""


def _stub(workdir):
    import glob
    names = set()
    for f in glob.glob("/usr/share/texlive/texmf-dist/tex/latex/moderncv/*.sty") + glob.glob("/usr/share/texlive/texmf-dist/tex/latex/moderncv/*.cls"):
        names |= set(re.findall(r"\\(fa[A-Z][A-Za-z]*)", open(f, errors="ignore").read()))
    body = STUB_ICONS + "".join(rf"\providecommand{{\{n}}}[1][]{{}}" + "\n" for n in sorted(names - {"faCircle"}))
    with open(os.path.join(workdir, "fontawesome5.sty"), "w") as f:
        f.write(body)


def compile_pdf(tex, out_pdf):
    """Compile LaTeX to out_pdf. Returns the page count, or 0 if LaTeX is unavailable or failed."""
    if not shutil.which("pdflatex"):
        return 0
    with tempfile.TemporaryDirectory() as d:
        for attempt in ("as-is", "stub-icons"):
            src = tex
            if attempt == "stub-icons" or _fontawesome_missing():
                _stub(d)
                src = re.sub(r"(\\moderncvstyle\{[^}]*\})", r"\1\n\\moderncvicons{symbols}", tex, count=1)
            with open(os.path.join(d, "resume.tex"), "w") as f:
                f.write(src)
            try:
                r = subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "resume.tex"],
                                   cwd=d, capture_output=True, text=True, timeout=120)
            except subprocess.TimeoutExpired:
                return 0
            pdf = os.path.join(d, "resume.pdf")
            if r.returncode == 0 and os.path.exists(pdf):
                shutil.copy(pdf, out_pdf)
                m = re.search(r"Output written on resume\.pdf \((\d+) page", r.stdout)
                return int(m.group(1)) if m else 1
            detail(f"pdflatex failed ({attempt}): " + "; ".join(l for l in r.stdout.splitlines() if l.startswith("!"))[:200])
            if attempt == "stub-icons" or "fontawesome5" not in r.stdout:
                return 0
    return 0


def render(profile, job, out_pdf):
    """Tailored LaTeX resume -> out_pdf. Returns the bolded terms, or None if LaTeX could not be used."""
    tex = profile.get("resume_tex")
    if not tex:
        return None
    tailored, hits = tailor_tex(tex, job, profile)
    base_pages = None
    pages = compile_pdf(tailored, out_pdf)
    if pages > 1:
        base_pages = compile_pdf(tex, out_pdf)  # bolding pushed it onto a second page: keep the original layout
        hits = [] if base_pages else hits
    if not pages or (pages > 1 and not base_pages):
        return None
    return hits
