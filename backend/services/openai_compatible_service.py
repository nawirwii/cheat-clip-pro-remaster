"""OpenAI-compatible AI gateway used by 9Router, custom APIs, Ollama and LM Studio."""
import json
import re
from typing import Any, Dict, List

import httpx

from backend.schemas.analyze import VideoAnalysis

DEFAULT_ENDPOINTS = {
    "9router": "https://9router.com/v1",
    "custom": "http://127.0.0.1:1234/v1",
    "ollama": "http://127.0.0.1:11434/v1",
    "lmstudio": "http://127.0.0.1:1234/v1",
}


def normalize_base_url(base_url: str) -> str:
    value = (base_url or "").strip().rstrip("/")
    if not value:
        raise ValueError("An OpenAI-compatible base URL is required.")
    return value[:-len("/chat/completions")] if value.endswith("/chat/completions") else value


def _extract_json(content: str) -> Dict[str, Any]:
    text = (content or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.I)
        candidate = fenced.group(1) if fenced else None
        if candidate is None:
            match = re.search(r"\{[\s\S]*\}", text)
            candidate = match.group(0) if match else None
        if not candidate:
            raise ValueError("AI response did not contain a JSON object.")
        return json.loads(candidate)


def _system_instruction() -> str:
    return (
        "You are an expert viral video clipper and retention analyst. "
        "Return ONLY valid JSON matching this schema: "
        '{"summary":"string","clips":[{"title":"string","start_time":0,"end_time":0,'
        '"hook_time":0,"virality_score":0,"key_quotes":["string"],"title_suggestion":"string",'
        '"caption_suggestion":"string","hashtag_suggestion":"string"}]}.'
        " Keep all output in the transcript language. Never use first-person pronouns in titles."
    )


async def fetch_compatible_models(base_url: str, api_key: str = "") -> List[Dict[str, Any]]:
    headers = {"Accept": "application/json"}
    if api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    async with httpx.AsyncClient(timeout=8.0) as client:
        response = await client.get(f"{normalize_base_url(base_url)}/models", headers=headers)
        response.raise_for_status()
        return response.json().get("data", [])


async def analyze_with_compatible(
    base_url: str,
    api_key: str,
    model_name: str,
    prompt: str,
    timeout: float = 120.0,
) -> VideoAnalysis:
    headers = {"Content-Type": "application/json"}
    if api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": _system_instruction()},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{normalize_base_url(base_url)}/chat/completions",
            headers=headers,
            json=payload,
        )
    if response.status_code >= 400:
        detail = response.text[:500]
        try:
            detail = response.json().get("error", {}).get("message", detail)
        except Exception:
            pass
        raise RuntimeError(f"OpenAI-compatible API error ({response.status_code}): {detail}")
    choices = response.json().get("choices", [])
    if not choices:
        raise ValueError("OpenAI-compatible API returned no choices.")
    content = choices[0].get("message", {}).get("content", "")
    return VideoAnalysis.model_validate(_extract_json(content))


async def test_compatible_connection(base_url: str, api_key: str, model_name: str) -> Dict[str, Any]:
    """Lightweight connectivity test — no JSON schema requirement."""
    headers = {"Content-Type": "application/json"}
    if api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
        "max_tokens": 5,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{normalize_base_url(base_url)}/chat/completions",
            headers=headers,
            json=payload,
        )
    if response.status_code >= 400:
        detail = response.text[:500]
        try:
            detail = response.json().get("error", {}).get("message", detail)
        except Exception:
            pass
        raise RuntimeError(f"API returned {response.status_code}: {detail}")
    choices = response.json().get("choices", [])
    if not choices:
        raise ValueError("API returned no choices.")
    content = choices[0].get("message", {}).get("content", "").strip().lower()
    if content != "ok":
        raise ValueError(f"Unexpected response: {content!r}")
    return {"ok": True, "model": model_name, "summary": "Connection OK"}


def default_base_url(provider: str) -> str:
    return DEFAULT_ENDPOINTS.get(provider, DEFAULT_ENDPOINTS["custom"])


def sanitize_models(models: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {"id": item.get("id"), "name": item.get("name", item.get("id"))}
        for item in models
        if item.get("id")
    ]
