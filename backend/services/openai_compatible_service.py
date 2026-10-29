"""OpenAI-compatible AI gateway used by 9Router, custom APIs, Ollama and LM Studio."""
import json
import re
from typing import Any, Dict, List

import httpx

from backend.schemas.analyze import VideoAnalysis

DEFAULT_ENDPOINTS = {
    "custom": "http://127.0.0.1:1234/v1",
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
    key = (api_key or "").strip()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    normalized = normalize_base_url(base_url)
    # Try strict json_object first, fallback to plain JSON prompt
    # (Ollama / LM Studio / 9Router free models often reject response_format)
    attempts = [
        {
            "model": model_name,
            "messages": [
                {"role": "system", "content": _system_instruction()},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        },
        {
            "model": model_name,
            "messages": [
                {"role": "system", "content": _system_instruction()},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        },
    ]
    last_err = None
    response = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for payload in attempts:
            try:
                response = await client.post(
                    f"{normalized}/chat/completions",
                    headers=headers,
                    json=payload,
                )
            except Exception as exc:
                last_err = str(exc)[:500]
                continue  # network blip — try next attempt
            if response.status_code == 400 and "response_format" in response.text.lower():
                last_err = response.text[:500]
                continue  # retry without response_format
            break  # got a usable response (or a real error to report)
    if response is None:
        raise RuntimeError(f"OpenAI-compatible API error: tidak dapat konek ke {normalized}. Detail: {last_err}")
    if response.status_code >= 400:
        detail = response.text[:500]
        try:
            err = response.json().get("error", {})
            detail = err.get("message", detail) if isinstance(err, dict) else str(err) or detail
        except Exception:
            pass
        if response.status_code == 401:
            hint = "API key kosong/salah" if not key else "API key ditolak server"
            raise RuntimeError(f"OpenAI-compatible API error (401): {hint}. Isi Custom API Key yg benar di AI Engine Settings. Detail: {detail}")
        raise RuntimeError(f"OpenAI-compatible API error ({response.status_code}): {detail}")
    choices = response.json().get("choices", [])
    if not choices:
        raise ValueError("OpenAI-compatible API returned no choices.")
    content = choices[0].get("message", {}).get("content", "")
    return VideoAnalysis.model_validate(_extract_json(content))


async def test_compatible_connection(base_url: str, api_key: str, model_name: str) -> Dict[str, Any]:
    """Lightweight connectivity test — lenient, accepts any non-empty reply."""
    key = (api_key or "").strip()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    normalized = normalize_base_url(base_url)
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
        "max_tokens": 50,
        "temperature": 0,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{normalized}/chat/completions",
                headers=headers,
                json=payload,
            )
    except httpx.ConnectError as exc:
        raise RuntimeError(
            f"Tidak bisa konek ke Base URL ({normalized}). "
            f"Pastikan URL benar, server AI jalan (Ollama/LM Studio), dan tidak kena firewall. Detail: {exc}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError(
            f"Timeout 30 dtk ke {normalized}. Server lambat / model lagi loading. Coba Test ulang. Detail: {exc}"
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"Gagal konek ke {normalized}: {exc}") from exc
    if response.status_code >= 400:
        detail = response.text[:800]
        try:
            err = response.json().get("error", {})
            detail = err.get("message", detail) if isinstance(err, dict) else str(err) or detail
        except Exception:
            pass
        if response.status_code == 401:
            hint = "API key kosong/salah" if not key else "API key ditolak server"
            raise RuntimeError(f"401 Unauthorized — {hint}. Isi Custom API Key yg benar lalu Test ulang. Detail: {detail}")
        if response.status_code == 404:
            raise RuntimeError(f"404 Not Found — Base URL / Model salah. Cek Base URL & Model ID. Detail: {detail}")
        if response.status_code == 429:
            raise RuntimeError(f"429 Rate limit — kuota habis. Ganti key / tunggu sebentar. Detail: {detail}")
        if response.status_code == 400 and "model" in detail.lower():
            raise RuntimeError(f"400 Bad Request — Model ID '{model_name}' tidak dikenal server. Klik Load Models / cek /models. Detail: {detail}")
        raise RuntimeError(f"API returned {response.status_code}: {detail}")
    try:
        data = response.json()
    except Exception as exc:
        raise ValueError(f"Server tidak return JSON valid. Cek Base URL (harus OpenAI-compatible /v1). Detail: {exc}") from exc
    choices = data.get("choices", []) if isinstance(data, dict) else []
    if not choices:
        raise ValueError(f"API returned no choices. Respon: {str(data)[:300]}")
    msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
    content = str(msg.get("content", "") or "").strip()
    # Some providers wrap in reasoning_content; fallback to it
    if not content and isinstance(choices[0], dict):
        content = str(choices[0].get("text", "") or "").strip()
    if not content:
        raise ValueError("API return kosong. Model tidak balas chat. Coba model lain / tambah max_tokens.")
    # Lenient: any non-empty reply = OK (dulu wajib persis 'ok' → sering false-failed)
    return {"ok": True, "model": model_name, "summary": "Connection OK", "reply": content[:200]}


def default_base_url(provider: str) -> str:
    return DEFAULT_ENDPOINTS.get(provider, DEFAULT_ENDPOINTS["custom"])


def sanitize_models(models: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {"id": item.get("id"), "name": item.get("name", item.get("id"))}
        for item in models
        if item.get("id")
    ]
