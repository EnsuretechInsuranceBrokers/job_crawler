"""GitHub Models client. Free in Actions through the built-in GITHUB_TOKEN."""
import json
import os
import re
import time

import requests

from .util import detail, log

def endpoints():
    """GitHub Models has had several front doors; use the first one that answers properly."""
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    urls = ["https://models.github.ai/inference/chat/completions"]
    if owner:
        urls.append(f"https://models.github.ai/orgs/{owner}/inference/chat/completions")
    urls.append("https://models.inference.ai.azure.com/chat/completions")
    return urls


class LLM:
    def __init__(self, cfg):
        self.token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_MODELS_TOKEN")
        self.model = cfg.get("model", "openai/gpt-4o-mini")
        self.enabled = bool(cfg.get("enabled", True) and self.token)
        self.failures = 0
        self.url = None  # the endpoint that worked

    def _post(self, url, body):
        if "azure.com" in url:  # the legacy endpoint wants the bare model name
            body = dict(body, model=body["model"].split("/")[-1])
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                   "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        for attempt in (1, 2):
            # no redirects: a redirected POST silently becomes a GET and returns nonsense
            r = requests.post(url, json=body, timeout=90, headers=headers, allow_redirects=False)
            wait = float(r.headers.get("retry-after") or 0)
            if r.status_code == 429 and attempt == 1 and 0 < wait <= 30:
                time.sleep(wait + 1)  # short per-minute limit: wait once; longer limits fall through
                continue
            break
        return r

    def ask(self, system, user, as_json=False):
        """Returns text (or parsed JSON), or None when the model is unavailable."""
        if not self.enabled:
            return None
        body = {"model": self.model, "temperature": 0.2,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if as_json:
            body["response_format"] = {"type": "json_object"}
        status = ""
        for url in ([self.url] if self.url else endpoints()):
            r = None
            try:
                r = self._post(url, body)
                status = r.status_code
                r.raise_for_status()
                text = r.json()["choices"][0]["message"]["content"].strip()
                parsed = json.loads(_json_part(text)) if as_json else text
                self.failures, self.url = 0, url
                return parsed
            except Exception as e:  # noqa: BLE001
                # API error bodies carry no personal data, but only show them in verbose/demo runs anyway
                host = url.split("/")[2] + ("/orgs" if "/orgs/" in url else "")
                detail(f"LLM call failed: {type(e).__name__} via {host} status={status} "
                       f"type={(r.headers.get('content-type') if r is not None else '')} "
                       f"location={(r.headers.get('location') if r is not None else '')} "
                       f"body={(r.text[:200] if r is not None else '')!r}")
                if self.url:  # a known-good endpoint failed: a real (rate-limit or content) failure
                    break
        self.failures += 1
        if self.failures >= 3 or status in (401, 403, 404):
            self.enabled = False  # quota exhausted or Models not enabled: stop trying this run
            log(f"LLM disabled for this run (status {status}); using keyword tailoring")
        return None


def _json_part(text):
    """Models sometimes wrap JSON in ``` fences or add a sentence around it."""
    m = re.search(r"\{.*\}", text, re.S)
    return m.group(0) if m else text
