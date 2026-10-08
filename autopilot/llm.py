"""Claude client with a deterministic mock.

All agent reasoning goes through `LLM.json(...)` (structured output) or
`LLM.text(...)`. The mock returns None for json/text so callers fall back to
their heuristic path: the whole program runs with no API key.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .settings import Settings

log = logging.getLogger(__name__)


class LLM:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.enabled = settings.llm_enabled
        self._client = None
        if self.enabled:
            try:
                import anthropic

                self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            except Exception as exc:  # noqa: BLE001
                log.warning("anthropic client unavailable (%s); running in mock mode", exc)
                self.enabled = False
        self.calls: list[dict[str, Any]] = []

    def text(self, system: str, user: str, max_tokens: int | None = None) -> str | None:
        if not self.enabled:
            return None
        resp = self._client.messages.create(
            model=self.settings.model,
            max_tokens=max_tokens or self.settings.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        out = "".join(getattr(b, "text", "") for b in resp.content)
        self.calls.append({"system": system[:80], "user": user[:200], "out": out[:200]})
        return out

    def json(self, system: str, user: str, max_tokens: int | None = None) -> Any | None:
        raw = self.text(system + "\n\nRespond with JSON only. No prose, no markdown fences.", user, max_tokens)
        if raw is None:
            return None
        return parse_json(raw)


def parse_json(raw: str) -> Any:
    raw = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = min([i for i in (raw.find("{"), raw.find("[")) if i >= 0] or [0])
        end = max(raw.rfind("}"), raw.rfind("]")) + 1
        return json.loads(raw[start:end])
