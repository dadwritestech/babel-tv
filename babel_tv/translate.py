"""Translate subtitle lines with a local OpenAI-compatible server (llama-server)."""
from __future__ import annotations

import json
import os
import re
import urllib.request
from collections import deque

LANGS = {
    "te": "Telugu", "hi": "Hindi", "ta": "Tamil", "kn": "Kannada", "ml": "Malayalam",
    "bn": "Bengali", "mr": "Marathi", "gu": "Gujarati", "pa": "Punjabi", "ur": "Urdu",
    "en": "English", "es": "Spanish", "pt": "Portuguese", "fr": "French", "de": "German",
    "it": "Italian", "ru": "Russian", "ja": "Japanese", "ko": "Korean", "zh": "Chinese",
    "ar": "Arabic", "tr": "Turkish", "id": "Indonesian", "vi": "Vietnamese", "th": "Thai",
    "pl": "Polish", "nl": "Dutch", "uk": "Ukrainian", "fa": "Persian", "sw": "Swahili",
}

SYSTEM = (
    "You are a live TV subtitle translator into {lang}. The user sends the previous lines "
    "(already subtitled, for context only) and then the NEW line. Translate only the NEW "
    "line; never repeat or re-translate the previous lines. The source is raw "
    "speech recognition from a {src} broadcast: it may be cut mid-sentence and contain "
    "misheard words, so translate what was most likely said. Keep names, numbers and "
    "places accurate. Output only the {lang} subtitle text on one line, no quotes, no notes."
)


def lang_name(code: str | None) -> str:
    return LANGS.get((code or "").lower(), code or "unknown-language")


class Translator:
    def __init__(self, target: str, base_url: str = "http://127.0.0.1:8080",
                 model: str = "gemma-4-12b", api_key: str | None = None,
                 context_lines: int = 2, timeout: float = 20.0):
        self.target = target
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key if api_key is not None else os.environ.get("LLAMA_API_KEY")
        self.timeout = timeout
        self.context: deque[str] = deque(maxlen=context_lines)
        self.recent: deque[str] = deque(maxlen=context_lines)

    def build_messages(self, text: str, src: str | None, with_context: bool = True) -> list[dict]:
        sys_msg = SYSTEM.format(lang=lang_name(self.target), src=lang_name(src))
        user = f"NEW: {text}"
        if self.context and with_context:
            user = "PREVIOUS:\n" + "\n".join(self.context) + "\n\n" + user
        return [{"role": "system", "content": sys_msg},
                {"role": "user", "content": user}]

    def translate(self, text: str, src: str | None = None) -> str:
        if not text.strip():
            return ""
        if src and src.lower() == self.target.lower():
            self.context.append(text)
            return text
        body = {
            "model": self.model,
            "messages": self.build_messages(text, src),
            "temperature": 0.2,
            "max_tokens": 256,
            # gemma-4 and qwen think by default; that alone is ~8x the latency.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        out = clean(self._post(body))
        if out and out in self.recent:
            # It re-translated a context line instead of the new one; retry without context.
            body["messages"] = self.build_messages(text, src, with_context=False)
            out = clean(self._post(body))
        self.context.append(text)
        self.recent.append(out)
        return out

    def _post(self, body: dict) -> str:
        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)["choices"][0]["message"]["content"] or ""


def clean(out: str) -> str:
    # Small models sometimes echo the "NEW:" label.
    out = re.sub(r"^\s*(NEW|PREVIOUS)\s*:\s*", "", out.strip(), flags=re.I)
    out = out.strip('"“”「」').strip()
    lines = [l for l in out.splitlines() if l.strip()]
    return " ".join(lines).strip()
