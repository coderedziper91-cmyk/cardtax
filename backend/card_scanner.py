"""
CardTax card scanner.

Sends a card photo to a vision LLM (default: OpenAI GPT-4o) and returns
structured identification: name, player, year, set, card number, parallel,
sport/game, estimated market value, and a confidence score.

Falls back gracefully when no API key is configured — the UI shows a manual
entry form instead. Never fails the request.

Environment:
  OPENAI_API_KEY  — required for AI-powered identification
  CARD_SCANNER_MODEL — optional override, defaults to "gpt-4o-mini"
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Optional


OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = os.environ.get("CARD_SCANNER_MODEL", "gpt-4o-mini")
UPLOADS_DIR = Path(__file__).parent.parent / "data" / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


SYSTEM_PROMPT = """You are a sports card and TCG identification expert. \
Analyze the image and identify the card. Return ONLY a JSON object — no \
prose, no markdown fences. Schema:

{
  "card_name": "string — e.g. '2018 Topps Chrome Update Ronald Acuna Jr. RC'",
  "player_name": "string — primary subject (player, character, or card name)",
  "year": "integer or null — release year",
  "set_name": "string — e.g. 'Topps Chrome Update'",
  "card_number": "string — e.g. 'HMT5' or '150'",
  "parallel_variant": "string or null — e.g. 'Refractor', 'Sapphire', 'Holo'",
  "sport_or_game": "one of: baseball, basketball, football, hockey, soccer, pokemon, mtg, yugioh, one-piece, other",
  "estimated_value_usd": "float or null — your best estimate of current raw FMV",
  "confidence": "float between 0 and 1",
  "notes": "string — anything noteworthy: grading slab visible, condition, autograph, patch, jersey number"
}

If you cannot identify the card clearly, return your best guess with a low \
confidence score and explain in notes."""


def is_available() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def save_upload(image_bytes: bytes, filename: str) -> str:
    """Persist the uploaded image. Returns a relative path under data/uploads/."""
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)[-120:] or "card.jpg"
    # Avoid collisions
    target = UPLOADS_DIR / safe
    n = 1
    while target.exists():
        stem = target.stem.rstrip(f"_{n-1}")
        target = UPLOADS_DIR / f"{stem}_{n}{target.suffix}"
        n += 1
    target.write_bytes(image_bytes)
    return str(target.relative_to(Path(__file__).parent.parent))


def _extract_json(text: str) -> dict:
    """Pull a JSON object out of the model's response, tolerating markdown."""
    # Strip code fences if present
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError(f"No JSON object in response: {text[:300]}")
    return json.loads(m.group(0))


def identify_card(image_bytes: bytes, content_type: str = "image/jpeg") -> dict:
    """
    Send the image to the vision model and return parsed JSON.

    Returns a dict with the schema above plus:
      - 'source': 'openai' or 'fallback'
      - 'error': set if the call failed
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return {
            "source": "fallback",
            "error": "No OPENAI_API_KEY set — fill in the card details manually.",
            "card_name": "",
            "player_name": "",
            "year": None,
            "set_name": "",
            "card_number": "",
            "parallel_variant": None,
            "sport_or_game": "other",
            "estimated_value_usd": None,
            "confidence": 0.0,
            "notes": "Set OPENAI_API_KEY environment variable to enable AI identification.",
        }

    # Lazy-import httpx so the module loads even without httpx installed
    try:
        import httpx
    except ImportError:
        return {
            "source": "fallback",
            "error": "httpx not installed — run `pip install httpx`.",
            "card_name": "", "player_name": "", "year": None, "set_name": "",
            "card_number": "", "parallel_variant": None, "sport_or_game": "other",
            "estimated_value_usd": None, "confidence": 0.0, "notes": "",
        }

    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{content_type};base64,{b64}"

    payload = {
        "model": DEFAULT_MODEL,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Identify this card. Return JSON per the schema."},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        "max_tokens": 600,
        "temperature": 0.1,
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.post(
                OPENAI_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        return {
            "source": "fallback",
            "error": f"Vision API call failed: {e}",
            "card_name": "", "player_name": "", "year": None, "set_name": "",
            "card_number": "", "parallel_variant": None, "sport_or_game": "other",
            "estimated_value_usd": None, "confidence": 0.0, "notes": "",
        }

    try:
        content = data["choices"][0]["message"]["content"]
        parsed = _extract_json(content)
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        return {
            "source": "fallback",
            "error": f"Could not parse model response: {e}",
            "card_name": "", "player_name": "", "year": None, "set_name": "",
            "card_number": "", "parallel_variant": None, "sport_or_game": "other",
            "estimated_value_usd": None, "confidence": 0.0, "notes": "",
        }

    parsed["source"] = "openai"
    parsed["model"] = DEFAULT_MODEL
    # Coerce types defensively
    if isinstance(parsed.get("year"), str) and parsed["year"].isdigit():
        parsed["year"] = int(parsed["year"])
    if isinstance(parsed.get("confidence"), str):
        try: parsed["confidence"] = float(parsed["confidence"])
        except ValueError: parsed["confidence"] = 0.0
    if isinstance(parsed.get("estimated_value_usd"), str):
        cleaned = parsed["estimated_value_usd"].replace("$", "").replace(",", "")
        try: parsed["estimated_value_usd"] = float(cleaned)
        except ValueError: parsed["estimated_value_usd"] = None
    return parsed
