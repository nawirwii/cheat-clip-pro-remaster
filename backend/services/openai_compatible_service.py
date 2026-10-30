"""OpenAI-compatible AI gateway used by 9Router, custom APIs, Ollama and LM Studio."""
import json
import logging
import re
from typing import Any, Dict, List, Optional

import httpx

from backend.schemas.analyze import VideoAnalysis

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINTS = {
    "custom": "http://127.0.0.1:1234/v1",
    "9router": "http://localhost:20128/v1",
    "ninerouter": "http://localhost:20128/v1",
    "ollama": "http://127.0.0.1:11434/v1",
    "lmstudio": "http://127.0.0.1:1234/v1",
}


def normalize_base_url(base_url: str) -> str:
    value = (base_url or "").strip().rstrip("/")
    if not value:
        raise ValueError("Base URL wajib diisi (contoh: http://192.168.0.3:20128/v1).")
    # buang suffix umum kalau user paste full endpoint
    for suffix in ("/chat/completions", "/models", "/models/info"):
        if value.endswith(suffix):
            value = value[: -len(suffix)].rstrip("/")
    return value


def _candidate_bases(normalized: str) -> List[str]:
    """Kembalikan [normalized, +/v1 atau tanpa /v1] agar user yg lupa /v1 tetap konek."""
    cands = [normalized]
    if normalized.endswith("/v1"):
        cands.append(normalized[: -len("/v1")].rstrip("/") or normalized)
    else:
        cands.append(normalized.rstrip("/") + "/v1")
    # dedup, preserve order
    seen = set()
    out = []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


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


def _to_float(value: Any, default: float = 0.0) -> float:
    """Toleran: '12.5s', '00:12', '12,5' -> float. Gagal -> default."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        text = str(value).strip().lower().replace(",", ".")
        # ambil angka pertama: 12.5 dari "12.5s" / "[12.5]" / "start: 12.5"
        import re as _re

        match = _re.search(r"-?\d+(?:\.\d+)?", text)
        if not match:
            return default
        # dukung mm:ss -> detik
        if ":" in text:
            parts = text.split(":")
            try:
                nums = [float(_re.search(r"\d+(?:\.\d+)?", p).group(0)) for p in parts if _re.search(r"\d", p)]
                if len(nums) == 2:
                    return nums[0] * 60 + nums[1]
                if len(nums) == 3:
                    return nums[0] * 3600 + nums[1] * 60 + nums[2]
            except Exception:
                pass
        return float(match.group(0))
    except Exception:
        return default


def _to_int(value: Any, default: int = 75) -> int:
    try:
        if value is None:
            return default
        if isinstance(value, bool):
            return default
        if isinstance(value, (int, float)):
            return int(value)
        import re as _re

        match = _re.search(r"\d+", str(value))
        return int(match.group(0)) if match else default
    except Exception:
        return default


def _to_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x) for x in value if str(x).strip()]
    text = str(value).strip()
    return [text] if text else []


def lenient_video_analysis(data: Any) -> VideoAnalysis:
    """Coerce model JSON yg berantakan jadi VideoAnalysis valid.

    Menangani: summary hilang, clips bukan list, field angka berupa string,
    hook_time hilang, key_quotes berupa string tunggal, dsb.
    """
    if not isinstance(data, dict):
        raise ValueError("AI tidak mengembalikan JSON object (summary/clips).")
    summary = str(data.get("summary") or data.get("overview") or "")
    raw_clips = data.get("clips") or data.get("viral_clips") or data.get("segments") or []
    if isinstance(raw_clips, dict):
        raw_clips = [raw_clips]
    if not isinstance(raw_clips, list):
        raw_clips = []
    clips = []
    for item in raw_clips:
        if not isinstance(item, dict):
            continue
        start = _to_float(item.get("start_time", item.get("start", 0.0)), 0.0)
        end = _to_float(item.get("end_time", item.get("end", 0.0)), 0.0)
        hook = item.get("hook_time", item.get("hook"))
        hook_f = _to_float(hook, start) if hook is not None else start
        # hook harus di dalam [start, end], kalau tidak -> snap ke start
        if not (min(start, end) <= hook_f <= max(start, end)):
            hook_f = start
        clips.append(
            {
                "title": str(item.get("title") or "Momen Menarik")[:120],
                "start_time": start,
                "end_time": end,
                "hook_time": hook_f,
                "virality_score": max(1, min(100, _to_int(item.get("virality_score", item.get("score", 75)), 75))),
                "key_quotes": _to_str_list(item.get("key_quotes") or item.get("quotes") or item.get("quote"))[:2],
                "title_suggestion": str(item.get("title_suggestion") or item.get("title") or "")[:150],
                "caption_suggestion": str(item.get("caption_suggestion") or "")[:500],
                "hashtag_suggestion": str(item.get("hashtag_suggestion") or "")[:300],
            }
        )
    # buang klip sampah (start==end==0 tanpa judul berarti)
    clips = [c for c in clips if c["end_time"] > c["start_time"] or c["title"].strip()]
    return VideoAnalysis.model_validate({"summary": summary, "clips": clips})


def _extract_content_from_choices(data: Dict[str, Any]) -> str:
    """Ambil teks jawaban dari berbagai bentuk respons OpenAI-compatible."""
    choices = data.get("choices", []) if isinstance(data, dict) else []
    if not choices or not isinstance(choices[0], dict):
        return ""
    first = choices[0]
    msg = first.get("message", {}) if isinstance(first.get("message"), dict) else {}
    content = msg.get("content", "")
    if isinstance(content, list):
        # sebagian provider kirim [{type:text, text:...}]
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text", "") or ""))
            else:
                parts.append(str(part))
        content = "".join(parts)
    text = str(content or "").strip()
    if not text:
        # fallback: reasoning_content / text langsung (DeepSeek-R1, dsb)
        text = str(msg.get("reasoning_content", "") or first.get("text", "") or "").strip()
    return text


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
    normalized = normalize_base_url(base_url)
    last_err: Optional[str] = None
    # Coba kandidat base: user sering lupa /v1 atau malah paste full endpoint (9Router :20128)
    for cand in _candidate_bases(normalized):
        # Coba dua bentuk path: /models dan /v1/models (beberapa gateway custom hanya serve salah satu)
        paths = [f"{cand}/models"]
        if cand.endswith("/v1"):
            paths.append(f"{cand[: -len('/v1')].rstrip('/')}/v1/models")
        else:
            paths.append(f"{cand.rstrip('/')}/v1/models")
        # dedup preserve order
        seen_p = set()
        upaths = []
        for p in paths:
            if p not in seen_p:
                seen_p.add(p)
                upaths.append(p)
        for url in upaths:
            try:
                async with httpx.AsyncClient(timeout=12.0) as client:
                    response = await client.get(url, headers=headers)
                    if response.status_code == 404:
                        last_err = f"404 di {url}"
                        continue
                    response.raise_for_status()
                    try:
                        data = response.json()
                    except Exception as exc:
                        last_err = f"non-JSON dari {url}: {exc}"
                        continue
                    if isinstance(data, dict):
                        items = data.get("data", data.get("models", []))
                    elif isinstance(data, list):
                        items = data
                    else:
                        items = []
                    if not items:
                        last_err = f"kosong dari {url}"
                        continue
                    return items if isinstance(items, list) else []
            except httpx.TimeoutException as exc:
                last_err = f"timeout ke {url}: {exc}"
                continue
            except httpx.ConnectError as exc:
                last_err = f"tidak bisa konek ke {url}: {exc}"
                continue
            except httpx.HTTPStatusError as exc:
                last_err = f"HTTP {exc.response.status_code} dari {url}: {exc.response.text[:300]}"
                continue
            except Exception as exc:
                last_err = f"gagal ambil {url}: {exc}"
                continue
    raise RuntimeError(
        "Tidak bisa ambil daftar model. Cek Base URL (contoh 9Router: http://localhost:20128/v1), "
        f"pastikan gateway jalan & key benar. Detail: {last_err}"
    )


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
    base_payloads = [
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
    tried_url = normalized
    auth_error_response = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        # Toleran: coba base apa adanya + varian ±/v1 (user 9Router sering lupa /v1)
        # Hanya 404 / network error yg lanjut ke kandidat berikutnya.
        # 401/429/400-model = error asli, langsung lapor tanpa dioverwrite kandidat lain.
        for cand in _candidate_bases(normalized):
            for payload in base_payloads:
                try:
                    tried_url = f"{cand}/chat/completions"
                    response = await client.post(
                        tried_url,
                        headers=headers,
                        json=payload,
                    )
                except Exception as exc:
                    last_err = str(exc)[:500]
                    response = None
                    continue  # network blip — coba payload berikutnya
                if response.status_code == 404:
                    last_err = response.text[:500]
                    response = None
                    break  # base salah — langsung coba kandidat berikutnya
                if response.status_code == 400 and "response_format" in response.text.lower():
                    last_err = response.text[:500]
                    continue  # retry without response_format
                break  # usable response atau real error (401/429/dll) — stop inner
            if response is None:
                continue  # semua payload gagal konek / 404 — coba kandidat berikutnya
            if response.status_code < 400:
                normalized = cand
                break
            if response.status_code in (401, 403, 429):
                auth_error_response = response
                normalized = cand
                break
            if response.status_code == 400:
                # 400 model-not-found tetap error asli — jangan dioverwrite kandidat lain
                normalized = cand
                break
            # status lain >=400 (500 dsb): coba kandidat berikutnya dulu, simpan sbg fallback
            last_err = response.text[:500]
            response = None
            continue
        if auth_error_response is not None:
            response = auth_error_response
    if response is None:
        raise RuntimeError(f"OpenAI-compatible API error: tidak dapat konek ke {normalized}. Coba tambah/hapus /v1 (cth 9Router: http://localhost:20128/v1). Detail: {last_err}")
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
    try:
        data = response.json()
    except Exception as exc:
        raise ValueError(
            f"Server tidak return JSON valid (cek Base URL harus OpenAI-compatible /v1). Detail: {exc}"
        ) from exc
    content = _extract_content_from_choices(data)
    if not content:
        raise ValueError(
            "Model return kosong (tidak ada message.content). "
            "Coba model lain / tambah max_tokens / matikan reasoning-only mode."
        )
    try:
        raw = _extract_json(content)
    except Exception as exc:
        preview = content[:400].replace("\n", " ")
        raise ValueError(
            "Model tidak return JSON valid. "
            "Coba model yg support JSON / ulangi Analyze. "
            f"Awalan jawaban model: {preview} Detail: {exc}"
        ) from exc
    try:
        return lenient_video_analysis(raw)
    except Exception as exc:
        preview = content[:400].replace("\n", " ")
        logger.warning("Lenient validation failed: %s | preview: %s", exc, preview[:200])
        raise ValueError(
            "Hasil AI tidak lengkap (summary/clips kosong atau timestamp invalid). "
            "Ulangi Analyze — kadang model free ngaco di percobaan pertama. "
            f"Detail: {exc}"
        ) from exc


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
    response = None
    last_err: Optional[str] = None
    used_base = normalized
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Toleran ±/v1: coba base user + varian lawannya (9Router sering lupa /v1)
            for cand in _candidate_bases(normalized):
                try:
                    used_base = cand
                    response = await client.post(
                        f"{cand}/chat/completions",
                        headers=headers,
                        json=payload,
                    )
                except httpx.ConnectError as exc:
                    last_err = str(exc)[:300]
                    response = None
                    continue
                except httpx.TimeoutException as exc:
                    raise RuntimeError(
                        f"Timeout 30 dtk ke {cand}. Server lambat / model lagi loading. Coba Test ulang. Detail: {exc}"
                    ) from exc
                except Exception as exc:
                    last_err = str(exc)[:300]
                    response = None
                    continue
                if response.status_code == 404:
                    last_err = response.text[:300]
                    response = None
                    continue  # coba kandidat base berikutnya
                break
    except RuntimeError:
        raise
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
    if response is None:
        raise RuntimeError(
            f"Tidak bisa konek ke Base URL ({normalized}). Coba tambah/hapus /v1 "
            f"(cth 9Router: http://localhost:20128/v1). Detail: {last_err}"
        )
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
