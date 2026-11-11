"""GitHub Models client. Free in Actions through the built-in GITHUB_TOKEN."""
import json
import os

import requests

from .util import log

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
        try:
            r = requests.post(URL, json=body, timeout=90, headers={
                "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json"})
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"].strip()
            self.failures = 0
            return json.loads(text) if as_json else text
        except Exception as e:  # noqa: BLE001
            self.failures += 1
            status = getattr(getattr(e, "response", None), "status_code", "")
            if self.failures >= 3 or status in (401, 403, 404):
                self.enabled = False  # quota exhausted or Models not enabled: stop trying this run
                log(f"LLM disabled for this run ({type(e).__name__} {status}); using keyword tailoring")
            return None
