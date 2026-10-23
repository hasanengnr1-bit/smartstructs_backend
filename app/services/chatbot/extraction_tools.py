"""Plan-extraction tools for the chatbot.

Wraps the six specialist extraction system prompts (the same ones the PDF
pipeline uses) as agent tools. They are only registered when the user has
attached a plan image, and the top-layer agent picks which one(s) to call
after classifying the plan — so a floor framing plan costs one extraction
call, not six.

Token efficiency:
- The heavy extraction prompts run in a SEPARATE vision-model call; the
  top-layer agent only ever sees the short tool descriptions.
- Results are compacted (None/empty fields dropped) before returning.
"""

import os
import json
import base64
import asyncio
from typing import Any, Callable, Optional

import httpx
from pydantic import BaseModel, ValidationError

from services.utils import get_logger, report_and_raise_500
from core.schema import (
    FloorSystemData,
    RoofSystemData,
    WallSystemData,
    PostData,
    FootingSystemData,
    ShearWallData,
)
from core.llm.prompts import (
    SYS_PROMPT_FLOOR,
    SYS_PROMPT_ROOF,
    SYS_PROMPT_WALL,
    SYS_PROMPT_POST,
    SYS_PROMPT_FOOTING,
    SYS_PROMPT_SHEAR_WALL,
)

logger = get_logger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# system_key -> (extraction prompt, response schema, short description)
EXTRACTORS = {
    "floor_system": (
        SYS_PROMPT_FLOOR,
        FloorSystemData,
        "floor framing: floor joists, drop beams, flush beams",
    ),
    "roof_system": (
        SYS_PROMPT_ROOF,
        RoofSystemData,
        "roof framing: rafters, ceiling joists, ridge beams, hips/valleys, roof beams",
    ),
    "wall_system": (
        SYS_PROMPT_WALL,
        WallSystemData,
        "walls: stud walls, headers, masonry walls",
    ),
    "post_system": (
        SYS_PROMPT_POST,
        PostData,
        "standalone posts/columns",
    ),
    "footing_system": (
        SYS_PROMPT_FOOTING,
        FootingSystemData,
        "foundations: strip footings, pad footings, slab, soil/concrete notes",
    ),
    "shear_wall_system": (
        SYS_PROMPT_SHEAR_WALL,
        ShearWallData,
        "lateral system: shear walls, holdowns, sheathing/nailing schedules",
    ),
}


def _compact(value):
    """Recursively drop None, empty strings/lists/dicts to keep results lean."""
    if isinstance(value, dict):
        out = {k: _compact(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        out = [_compact(v) for v in value]
        return [v for v in out if v not in (None, "", [], {})]
    return value


def _parse_json_text(text: str) -> dict:
    """Extract the JSON object from a model reply (tolerates code fences)."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise json.JSONDecodeError("No JSON object found in reply", text, 0)
    return json.loads(text[start : end + 1])


def _decode_data_url(data_url: str) -> tuple[bytes, str]:
    """data:image/png;base64,... -> (bytes, mime_type)"""
    header, _, b64 = data_url.partition(",")
    mime = header.split(":", 1)[1].split(";", 1)[0] if ":" in header else "image/png"
    return base64.b64decode(b64), mime


async def _call_gemini_extractor(
    sys_prompt: str, schema_model, image_data_url: str
) -> dict:
    """Native Gemini extraction — response_schema gives validated data directly."""
    from google.genai import types
    from core.llm.clients import google_client_async

    model = os.environ.get("GEMINI_MODEL_NAME") or "gemini-2.5-flash"
    image_bytes, mime = _decode_data_url(image_data_url)

    config = types.GenerateContentConfig(
        system_instruction=sys_prompt,
        response_schema=schema_model,
        response_mime_type="application/json",
        temperature=0,
    )
    contents_list: list[Any] = [
        types.Part.from_bytes(data=image_bytes, mime_type=mime),
        "Analyze the given structural plan based on the provided instructions",
    ]
    response = await google_client_async.models.generate_content(
        model=model,
        config=config,
        contents=contents_list,
    )
    # Extract usage if available
    usage_info = {}
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        usage_info = {
            "prompt_tokens": response.usage_metadata.prompt_token_count,
            "completion_tokens": response.usage_metadata.candidates_token_count,
            "total_tokens": response.usage_metadata.total_token_count,
        }

    parsed = response.parsed
    if parsed is not None:
        extracted_data: Any
        if isinstance(parsed, BaseModel):
            extracted_data = parsed.model_dump()
        elif isinstance(parsed, dict):
            extracted_data = parsed
        else:
            extracted_data = parsed.value if hasattr(parsed, "value") else parsed
        return {
            "extracted_data": _compact(extracted_data),
            "validation": "passed",
            "_usage": usage_info,
        }
    # Schema parse failed on the SDK side — fall back to raw JSON text.
    raw = _parse_json_text(response.text or "")
    try:
        validated = schema_model.model_validate(raw)
        return {
            "extracted_data": _compact(validated.model_dump()),
            "validation": "passed",
            "_usage": usage_info,
        }
    except ValidationError as e:
        return {
            "extracted_data": _compact(raw),
            "validation": "failed",
            "_usage": usage_info,
            "validation_errors": [
                f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}"
                for err in e.errors()[:10]
            ],
        }


async def _call_vision_extractor(
    sys_prompt: str, schema_model, image_data_url: str
) -> dict:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    model = "google/gemma-4-31B-it"
    if not api_key:
        return {"error": "OPENROUTER_API_KEY is not set"}

    user_content = [
        {"type": "image_url", "image_url": {"url": image_data_url}},
        {
            "type": "text",
            "text": "Analyze the given structural plan based on the provided instructions."
            " Output valid JSON only — escape any double quotes inside string values"
            ' (write 16\\" O.C., not 16" O.C.).',
        },
    ]
    schema = schema_model.model_json_schema()
    payload: dict[str, Any] = {
        "model": model,
        "temperature": 0,
        "reasoning": {"effort": "high"},
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_content},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "extraction", "strict": False, "schema": schema},
        },
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    text = ""
    parsed = None
    # Normal extraction is ~7s. httpx read-timeouts don't fire while
    # OpenRouter sends keepalive bytes for a stuck provider, so enforce an
    # absolute deadline with asyncio.wait_for.
    async with httpx.AsyncClient(timeout=60.0) as client:
        # Retry once: covers both a stalled provider and JSON with unescaped
        # quotes from plan callouts (16" O.C.).
        for attempt in range(2):
            try:
                response = await asyncio.wait_for(
                    client.post(OPENROUTER_URL, headers=headers, json=payload),
                    timeout=75.0,
                )
                if response.status_code == 400 and "response_format" in payload:
                    # Provider may not support structured outputs — retry with
                    # the schema embedded in the prompt instead.
                    payload.pop("response_format")
                    payload["messages"][0]["content"] = (
                        sys_prompt
                        + "\n\nReturn ONLY a single JSON object that conforms to this JSON Schema"
                        " (no markdown, no commentary):\n" + json.dumps(schema)
                    )
                    response = await asyncio.wait_for(
                        client.post(OPENROUTER_URL, headers=headers, json=payload),
                        timeout=75.0,
                    )
            except (asyncio.TimeoutError, httpx.TimeoutException):
                logger.warning(f"Extractor call timed out (attempt {attempt + 1})")
                continue
            response.raise_for_status()
            data = response.json()
            if "choices" not in data or not data["choices"]:
                # upstream aborts arrive as HTTP 200 with an error body — retry
                logger.warning(
                    f"Extractor body error (attempt {attempt + 1}): {data.get('error') or data}"
                )
                continue
            text = data["choices"][0]["message"].get("content") or ""
            try:
                parsed = _parse_json_text(text)
                break
            except json.JSONDecodeError as e:
                logger.warning(
                    f"Extractor JSON parse failed (attempt {attempt + 1}): {e}"
                )

    usage_info = data.get("usage", {})
    if parsed is None:
        # Give the agent the raw text — it can still read values out of it.
        return {
            "error": "Extractor output was not valid JSON. Raw output follows — "
            "verify values directly against the image.",
            "raw_text": text[:6000],
            "_usage": usage_info,
        }

    try:
        validated = schema_model.model_validate(parsed)
        return {
            "extracted_data": _compact(validated.model_dump()),
            "validation": "passed",
            "_usage": usage_info,
        }
    except ValidationError as e:
        # Return raw data anyway — the top-layer agent verifies and fixes it.
        return {
            "extracted_data": _compact(parsed),
            "validation": "failed",
            "_usage": usage_info,
            "validation_errors": [
                f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}"
                for err in e.errors()[:10]
            ],
        }


def build_extraction_tools(
    get_image: Callable[[], Optional[str]],
    provider: str = "openrouter",
) -> list[Callable]:
    """Create the six extract_* tools bound to the current plan image.

    provider: "gemini" uses the native Gemini API (GOOGLE_API_KEY, schema-
    enforced output); "openrouter" uses OPENROUTER_VISION_MODEL."""
    extractor_fn = (
        _call_gemini_extractor if provider == "gemini" else _call_vision_extractor
    )
    tools = []
    for system_key, (sys_prompt, schema_model, description) in EXTRACTORS.items():

        def make_tool(
            key=system_key, prompt=sys_prompt, model=schema_model, desc=description
        ):
            async def extract_tool() -> dict:
                image = get_image()
                if not image:
                    return {"error": "No plan image attached to this conversation."}
                try:
                    return await extractor_fn(prompt, model, image)
                except json.JSONDecodeError as e:
                    return {"error": f"Extractor returned unparseable JSON: {e}"}
                except Exception as e:
                    logger.error(f"extract_{key} failed: {e}")
                    await report_and_raise_500(e, context=f"extract_{key}", raise_error=False)
                    return {"error": f"Extraction failed: {e}"}

            extract_tool.__name__ = f"extract_{key}"
            extract_tool.__doc__ = (
                f"Run the specialist extraction prompt for {desc} on the plan image "
                f"the user attached. Call this ONLY if the plan actually shows this "
                f"system. Returns structured member data (schema-validated when "
                f"possible). You MUST verify the returned values against the image "
                f"yourself and correct any misreads before using them."
            )
            return extract_tool

        tools.append(make_tool())
    return tools
