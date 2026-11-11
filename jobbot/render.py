import os

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import ROOT

_env = Environment(loader=FileSystemLoader(os.path.join(ROOT, "templates")),
                   autoescape=select_autoescape(["html", "j2"]))


def render_html(profile):
    return _env.get_template("resume.html.j2").render(p=profile)


def render_pdf(browser, profile, path):
    page = browser.new_page()
    try:
        page.set_content(render_html(profile), wait_until="load")
        page.pdf(path=path, format="Letter", print_background=True,
                 margin={"top": "14mm", "bottom": "14mm", "left": "15mm", "right": "15mm"})
    finally:
        page.close()
    return path
