import html
import os
import re
import sys

VERBOSE = os.environ.get("JOBBOT_VERBOSE") == "1"


def log(msg):
    """Always printed. Must never contain personal data: Actions logs are public."""
    print(msg, flush=True)


def detail(msg):
    """Printed only in verbose/demo mode (may name companies and questions)."""
    if VERBOSE:
        print("  " + msg, file=sys.stderr, flush=True)


def strip_html(text):
    text = html.unescape(text or "")
    text = re.sub(r"<(br|/p|/li|/div|/h\d)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


def norm(text):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#.]+", " ", (text or "").lower())).strip()


def has_term(term, haystack_norm):
    """Whole-term match of a skill/keyword inside normalised text."""
    t = norm(term)
    return bool(t) and re.search(r"(?<![a-z0-9+#])" + re.escape(t) + r"(?![a-z0-9+#])", haystack_norm) is not None
