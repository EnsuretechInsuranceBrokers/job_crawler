"""GitHub Models client. Free in Actions through the built-in GITHUB_TOKEN."""
import json
import os
import re
import time

import requests

from .util import detail, log

URL = "https://models.github.ai/inference/chat/completions"


class LLM:
    def __init__(self, cfg):
        self.token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_MODELS_TOKEN")
        self.model = cfg.get("model", "openai/gpt-4o-mini")
        self.enabled = bool(cfg.get("enabled", True) and self.token)
        self.failures = 0

    def ask(self, system, user, as_json=False):
        """Returns text (or parsed JSON), or None when the model is unavailable."""
        if not self.enabled:
            return None
        body = {"model": self.model, "temperature": 0.2,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if as_json:
            body["response_format"] = {"type": "json_object"}
        r = None
        try:
            for attempt in (1, 2):
                r = requests.post(URL, json=body, timeout=90, headers={
                    "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
                wait = float(r.headers.get("retry-after") or 0)
                if r.status_code == 429 and attempt == 1 and 0 < wait <= 30:
                    time.sleep(wait + 1)  # short per-minute limit: wait once; longer limits fall through
                    continue
                break
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"].strip()
            if as_json:
                text = _json_part(text)
                parsed = json.loads(text)
            self.failures = 0
            return parsed if as_json else text
        except Exception as e:  # noqa: BLE001
            self.failures += 1
            status = getattr(r, "status_code", "")
            # API error bodies carry no personal data, but only show them in verbose/demo runs anyway
            detail(f"LLM call failed: {type(e).__name__} status={status} body={(r.text[:300] if r is not None else '')!r}")
            if self.failures >= 3 or status in (401, 403, 404):
                self.enabled = False  # quota exhausted or Models not enabled: stop trying this run
                log(f"LLM disabled for this run ({type(e).__name__} {status}); using keyword tailoring")
            return None


def _json_part(text):
    """Models sometimes wrap JSON in ``` fences or add a sentence around it."""
    m = re.search(r"\{.*\}", text, re.S)
    return m.group(0) if m else text
