import os

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REQUIRED = ["state_key", "name", "email", "experience"]


def load_config(path=None):
    with open(path or os.path.join(ROOT, "config.yml")) as f:
        return yaml.safe_load(f)


def load_profile(demo=False):
    """Profile comes from the PROFILE_YAML secret, or a local profile.yml."""
    if demo:
        with open(os.path.join(ROOT, "profile.example.yml")) as f:
            text = f.read()
    elif os.environ.get("PROFILE_YAML", "").strip():
        text = os.environ["PROFILE_YAML"]
    elif os.path.exists(os.path.join(ROOT, "profile.yml")):
        with open(os.path.join(ROOT, "profile.yml")) as f:
            text = f.read()
    else:
        raise SystemExit("No profile: set the PROFILE_YAML secret (see profile.example.yml) or run with --demo.")
    profile = yaml.safe_load(text)
    missing = [k for k in REQUIRED if not profile.get(k)]
    if missing:
        raise SystemExit("Profile is missing required fields: " + ", ".join(missing))
    if not demo and str(profile["state_key"]).startswith("change-me"):
        raise SystemExit("Set state_key in your profile to your own long random passphrase.")
    profile.setdefault("links", {})
    profile.setdefault("answers", {})
    profile.setdefault("education", [])
    profile.setdefault("projects", [])
    skills = profile.get("skills") or []
    if isinstance(skills, dict):  # allow {category: [skills]}
        skills = [s for group in skills.values() for s in group]
    profile["skills"] = [str(s) for s in skills]
    search = profile.setdefault("search", {})
    if not search.get("titles"):  # least-effort default: look for the titles you already held
        search["titles"] = sorted({e["title"] for e in profile["experience"] if e.get("title")})
    return profile
