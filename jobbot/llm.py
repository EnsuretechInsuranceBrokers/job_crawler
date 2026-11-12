"""Optional LLM, through any OpenAI-compatible API with a free tier.

Configured in the profile:
    llm: {provider: gemini, api_key: "..."}          # or groq / openrouter
    llm: {base_url: "https://host/v1", api_key: "...", model: "name"}   # anything else
Model names change often, so unless `model` is given the client asks the provider
which models exist and picks a suitable one.
"""
import json
import re
import time

import requests

from .util import detail, log

PROVIDERS = {
    # name: (base url, fallback model, ranking of model ids - higher is better, None = unusable)
    # Gemini is called through Google's own API (below) with its always-current model alias.
    "gemini": ("https://generativelanguage.googleapis.com/v1beta", "gemini-flash-latest", None),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile",
             lambda m: (("70b" in m) * 2 + ("versatile" in m), _version(m)) if "llama" in m and "guard" not in m else None),
    "openrouter": ("https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct:free",
                   lambda m: (("llama" in m) + ("70b" in m), _version(m)) if m.endswith(":free") else None),
}


def _version(model):
    return tuple(int(x) for x in re.findall(r"\d+", model)[:3])


class LLM:
    def __init__(self, cfg, profile=None):
        p = (profile or {}).get("llm") or {}
        base, default, self.rank = PROVIDERS.get(str(p.get("provider", "")).lower(), (None, None, None))
        self.base = str(p.get("base_url") or base or "").rstrip("/")
        self.key = p.get("api_key")
        self.model = p.get("model")
        self.default = default
        self.enabled = bool(cfg.get("enabled", True) and self.base and self.key)
        self.failures = 0
        self.json_mode = True
        self.calls = self.answered = 0
        self.min_interval = float(cfg.get("min_seconds_between_calls", 4))  # stay under free per-minute limits
        self._last = 0.0
        self.native_gemini = "generativelanguage.googleapis.com" in self.base and not self.base.endswith("/openai")
        if self.native_gemini and not self.model:
            self.model = default or "gemini-flash-latest"

    def _headers(self):
        return {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}

    def _pick_model(self):
        """Ask the provider what exists; fall back to a known name."""
        try:
            r = requests.get(f"{self.base}/models", headers=self._headers(), timeout=30)
            ids = [str(m.get("id", "")).split("/", 1)[-1] if str(m.get("id", "")).startswith("models/") else str(m.get("id", ""))
                   for m in r.json().get("data", [])]
            ranked = sorted(((self.rank(i), i) for i in ids if self.rank and self.rank(i) is not None), reverse=True)
            if ranked:
                return ranked[0][1]
        except Exception as e:  # noqa: BLE001
            detail(f"LLM model listing failed: {type(e).__name__}")
        return self.default

    def ask(self, system, user, as_json=False):
        """Returns text (or parsed JSON), or None when the model is unavailable."""
        if not self.enabled:
            return None
        wait = self._last + self.min_interval - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        self.calls += 1
        if not self.model:
            self.model = self._pick_model()
            detail(f"LLM model: {self.model}")
            if not self.model:
                self.enabled = False
                return None
        body = {"model": self.model, "temperature": 0.2,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        r, status = None, ""
        try:
            if self.native_gemini:
                return self._gemini(system, user, as_json)
            for attempt in (1, 2, 3):
                if as_json and self.json_mode:
                    body["response_format"] = {"type": "json_object"}
                else:
                    body.pop("response_format", None)
                # no redirects: a redirected POST silently becomes a GET
                r = requests.post(f"{self.base}/chat/completions", json=body, timeout=90,
                                  headers=self._headers(), allow_redirects=False)
                status = r.status_code
                wait = float(r.headers.get("retry-after") or 0)
                if status == 429 and attempt < 3 and wait <= 45:
                    time.sleep((wait or 20) + 1)  # free tiers limit per minute: wait and retry
                    continue
                if status == 400 and as_json and self.json_mode:
                    self.json_mode = False  # provider/model without JSON mode: ask in plain text
                    continue
                break
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"].strip()
            parsed = json.loads(_json_part(text)) if as_json else text
            self.failures = 0
            self.answered += 1
            return parsed
        except Exception as e:  # noqa: BLE001
            self.failures += 1
            r = r if r is not None else getattr(e, "response", None)
            status = status or getattr(r, "status_code", "")
            # error bodies carry no personal data, but only show them in verbose/demo runs anyway
            detail(f"LLM call failed: {type(e).__name__} status={status} body={(r.text[:200] if r is not None else '')!r}")
            if self.failures >= 3 or status in (401, 403, 404):
                self.enabled = False  # bad key or quota gone: stop trying this run
                log(f"LLM disabled for this run ({type(e).__name__} {status}); using keyword tailoring")
            return None


GEMINI_ALIASES = ["gemini-flash-latest", "gemini-flash-lite-latest"]  # Google keeps these pointing at current models


def _gemini_models(self):
    """Aliases first, then whatever text 'flash' models the key can use, newest first."""
    models = [self.model] if self.model and self.model not in GEMINI_ALIASES else []
    models += GEMINI_ALIASES
    try:
        r = requests.get(f"{self.base}/models?pageSize=200", headers={"x-goog-api-key": str(self.key)}, timeout=30)
        names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
        plain = [n for n in names if re.fullmatch(r"gemini-[\d.]+-flash(-lite)?", n)]
        models += sorted(plain, key=lambda n: (_version(n), "lite" not in n), reverse=True)
    except Exception as e:  # noqa: BLE001
        detail(f"gemini model listing failed: {type(e).__name__}")
    return list(dict.fromkeys(models))


def _gemini_call(self, system, user, as_json):
    """Google's generateContent API, as in their quickstart (key in the x-goog-api-key header).

    Popular models are often overloaded on the free tier, so on "unavailable", "gone" or a
    timeout the client moves to the next model and stays with the one that answers.
    """
    body = {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.2, **({"responseMimeType": "application/json"} if as_json else {})}}
    if not getattr(self, "_models", None):
        self._models = _gemini_models(self)
    last = None
    order = list(self._models)
    for n, model in enumerate(order + order[:1]):  # after a full pass, one more try of the preferred model
        r = None
        if n == len(order):
            time.sleep(20)  # every model was busy or rate-limited: give the per-minute limit a moment
        try:
            r = requests.post(f"{self.base}/models/{model}:generateContent", json=body, timeout=30,
                              headers={"x-goog-api-key": str(self.key), "Content-Type": "application/json"})
            if r.status_code in (401, 403):
                break  # a key problem: no other model will help
            if r.status_code >= 400:
                raise requests.HTTPError(f"gemini {r.status_code}")
            text = "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"]).strip()
            parsed = json.loads(_json_part(text)) if as_json else text
            if self.model != model:
                detail(f"gemini model in use: {model}")
            self.model = model
            self._models = [model] + [m for m in self._models if m != model]  # stay with what works
            self.failures = 0
            self.answered += 1
            return parsed
        except Exception as e:  # noqa: BLE001
            last = e
            detail(f"gemini {model}: {type(e).__name__} {getattr(r, 'status_code', '')}")
    err = requests.HTTPError("no gemini model answered")
    err.response = r if r is not None else getattr(last, "response", None)
    raise err


LLM._gemini = _gemini_call


def _json_part(text):
    """Models sometimes wrap JSON in ``` fences or add a sentence around it."""
    m = re.search(r"\{.*\}", text, re.S)
    return m.group(0) if m else text
