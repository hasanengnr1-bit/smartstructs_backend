"""In-memory store for chat-initiated calc-sheet data.

Each entry holds the tool call results from a single conversation turn,
reshaped into the format that `build_html_report()` expects. Entries expire
after 2 hours and are cleaned up lazily on access.
"""

import time
import uuid
from typing import Any, Optional

from services.chatbot.tools import WIND_DETAIL_CACHE
from services.utils import get_logger, report_and_raise_500

logger = get_logger(__name__)

TTL_SECONDS = 2 * 60 * 60  # 2 hours
MAX_ENTRIES = 200

# {calc_sheet_id: {"data": {...}, "created_at": float}}
_store: dict[str, dict[str, Any]] = {}


def _cleanup() -> None:
    """Remove expired entries lazily."""
    now = time.time()
    expired = [k for k, v in _store.items() if now - v["created_at"] > TTL_SECONDS]
    for k in expired:
        _store.pop(k, None)
    # Hard cap
    while len(_store) > MAX_ENTRIES:
        _store.pop(next(iter(_store)))


_TOOL_SOURCE_TYPE = {
    "check_beam_or_joist": "floor_joist",  # renders the timber beam sheet
    "check_i_joist": "i_joist",
    "check_wall_stud": "stud_wall",
    "check_wood_post": "post",
    "check_wood_shear_wall": "shear_wall",
    "check_cmu_masonry_wall": "masonry_wall",
    "check_cmu_shear_wall": "masonry_wall",
    "check_strip_or_pad_footing": "continuous_footing",
}


def _reshape_tool_result(
    tool_name: str, args: dict, result: dict, index: int = 1
) -> Optional[dict]:
    """Convert a single tool call result into the 'item' format the report builder expects.

    Returns None for tools that don't produce member-level calc sheet entries
    (e.g., hazard lookups).
    """
    if tool_name not in _TOOL_SOURCE_TYPE:
        return None
    if "error" in result:
        return None

    from services.analysis.analysis import _normalize_result

    source_type = _TOOL_SOURCE_TYPE[tool_name]
    if tool_name == "check_strip_or_pad_footing" and str(
        args.get("footing_type", "")
    ).lower().startswith("pad"):
        source_type = "pad_footing"

    label = args.get("name") or tool_name.replace("_", " ").title()
    try:
        return _normalize_result(
            result,
            source_system=source_type,
            source_collection=source_type,
            source_type=source_type,
            label=label,
            index=index,
        )
    except Exception as exc:
        logger.warning(f"Failed to normalize result for {tool_name}: {exc}")
        return None


def store_calc_sheet(
    tool_results: list[dict[str, Any]],
) -> Optional[tuple[str, dict[str, Any]]]:
    """Store calculation data from a chat turn in memory and return (calc_sheet_id, data).

    tool_results: list of {"tool": name, "args": {...}, "result": {...}}
    Returns None if no calculable results to store.
    """
    _cleanup()

    items = []
    hazard_data: dict[str, Any] = {}
    lateral_data: dict[str, Any] = {}

    for tr in tool_results:
        tool_name = tr["tool"]
        args = tr.get("args", {})
        result = tr.get("result", {})

        if "error" in result:
            continue

        # Hazard lookups
        if tool_name == "get_wind_speed":
            hazard_data.setdefault("wind", {}).update(result)
        elif tool_name == "get_ground_snow_load":
            hazard_data.setdefault("snow", {}).update(result)
        elif tool_name == "get_seismic_parameters":
            hazard_data.setdefault("seismic", {}).update(result)

        # Lateral loads
        elif tool_name == "compute_wind_and_seismic_loads":
            lateral_data["computed"] = result
            # Resolve wind detail ref if present
            meta = (result.get("meta") or {}).get("wind") or {}
            ref = meta.get("directional_ref")
            if ref and ref in WIND_DETAIL_CACHE:
                lateral_data["wind_detail"] = WIND_DETAIL_CACHE[ref]
        elif tool_name == "compute_component_wind_pressure":
            lateral_data.setdefault("component_pressures", []).append(result)

        # Member checks
        else:
            item = _reshape_tool_result(tool_name, args, result)
            if item is not None:
                items.append(item)

    if not items and not hazard_data and not lateral_data:
        return None

    data = {
        "items": items,
        "hazard": hazard_data,
        "lateral": lateral_data,
    }

    calc_sheet_id = uuid.uuid4().hex[:16]
    _store[calc_sheet_id] = {
        "data": data,
        "created_at": time.time(),
    }
    return calc_sheet_id, data


async def store_calc_sheet_db(
    db: Any,
    calc_sheet_id: str,
    session_id: str,
    user_email: str,
    data: dict[str, Any],
) -> None:
    """Persist a calc sheet to the database so it survives server restarts.

    This is called after store_calc_sheet() to durably save the data alongside
    the in-memory TTL entry. On retrieval, get_calc_sheet_data() checks memory
    first and falls back to the DB.
    """
    from database import CalcSheet  # local import to avoid circular deps
    from sqlalchemy import select

    try:
        result = await db.execute(
            select(CalcSheet).where(CalcSheet.calc_sheet_id == calc_sheet_id)
        )
        existing = result.scalar_one_or_none()
        if existing is None:
            row = CalcSheet(
                calc_sheet_id=calc_sheet_id,
                session_id=session_id,
                user_email=user_email,
                data=data,
            )
            db.add(row)
            await db.commit()
    except Exception as exc:
        logger.error(f"Failed to persist calc sheet {calc_sheet_id} to DB: {exc}")
        await report_and_raise_500(exc, context="calc_sheet_store persist", raise_error=False)


async def get_calc_sheet_data(
    calc_sheet_id: str, db: Any = None
) -> Optional[dict[str, Any]]:
    """Retrieve stored calc-sheet data.

    Checks the in-memory TTL store first; if not found (expired or server
    restarted) falls back to the database if a db session is provided.
    """
    _cleanup()
    entry = _store.get(calc_sheet_id)
    if entry is not None:
        return entry["data"]

    if db is None:
        return None

    # DB fallback
    from database import CalcSheet
    from sqlalchemy import select

    try:
        result = await db.execute(
            select(CalcSheet).where(CalcSheet.calc_sheet_id == calc_sheet_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        # Repopulate memory for fast subsequent access
        _store[calc_sheet_id] = {"data": row.data, "created_at": time.time()}
        return row.data
    except Exception as exc:
        logger.error(f"DB fallback for calc sheet {calc_sheet_id} failed: {exc}")
        await report_and_raise_500(exc, context="calc_sheet_store fallback", raise_error=False)
        return None


def build_report_inputs(data: dict[str, Any]) -> tuple[dict, dict]:
    """Convert stored calc-sheet data into the (extracted_data, analysis_result)
    tuple that build_html_report() expects."""
    extracted_data: dict[str, Any] = {
        "project_data": {
            "project_name": "SmartStructs Copilot",
            "design_code": "IBC 2024",
            "design_method": "ASD",
        },
    }
    if data.get("hazard"):
        extracted_data["hazard_lookup_result"] = data["hazard"]

    items = data.get("items", [])
    passing = sum(1 for i in items if i.get("status") == "PASS")
    failing = sum(1 for i in items if i.get("status") == "FAIL")

    analysis_result: dict[str, Any] = {
        "summary": {
            "total_items": len(items),
            "passing": passing,
            "failing": failing,
            "errors": len(items) - passing - failing,
            "overall": "PASS" if passing == len(items) and len(items) > 0 else "FAIL",
        },
        "items": items,
    }

    raw_lateral = data.get("lateral")
    if raw_lateral:
        # Reconstruct the lateral structure that build_html_report expects
        computed = raw_lateral.get("computed", {})
        meta = computed.get("meta") or {}
        wind_detail = raw_lateral.get("wind_detail")
        if wind_detail:
            wind_meta = meta.get("wind") or {}
            meta = {**meta, "wind": {**wind_meta, "directional": wind_detail}}

        lateral = {
            "computed": meta,
            "story_shears": computed.get("story_shears"),
            "line_shears": {},
        }
        if raw_lateral.get("component_pressures"):
            lateral["component_pressures"] = [
                {**p, "label": (p.get("args") or {}).get("name") or "Component"}
                for p in raw_lateral["component_pressures"]
            ]
        analysis_result["lateral"] = lateral

    return extracted_data, analysis_result
