import os

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import ROOT

_env = Environment(loader=FileSystemLoader(os.path.join(ROOT, "templates")),
                   autoescape=select_autoescape(["html", "j2"]))


def render_html(profile):
    return _env.get_template("resume.html.j2").render(p=profile)


def render_letter_pdf(browser, profile, text, path):
    paragraphs = [x.strip() for x in text.split("\n\n") if x.strip()]
    html = _env.get_template("letter.html.j2").render(p=profile, paragraphs=paragraphs)
    return render_pdf(browser, profile, path, html, margin="22mm")


def render_pdf(browser, profile, path, html=None, margin="14mm"):
    page = browser.new_page()
    try:
        page.set_content(html or render_html(profile), wait_until="load")
        page.pdf(path=path, format="Letter", print_background=True,
                 margin={"top": margin, "bottom": margin, "left": margin, "right": margin})
    finally:
        page.close()
    return path
