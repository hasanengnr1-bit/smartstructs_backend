"""Canonical member-size enums used by extraction schemas.

The schema stores nominal sawn-lumber callouts such as ``2x10`` and canonical
rectangular engineered-wood dimensions such as ``1.75x11.875``. The calculator
turns those labels into actual section properties.
"""

import re
from typing import Annotated, Literal

from pydantic import BeforeValidator
from typing_extensions import TypeAliasType


SAWN_MEMBER_SIZE_VALUES = tuple(
    f"{width}x{depth}"
    for width in (2, 3, 4, 5, 6, 8, 10, 12)
    for depth in (2, 3, 4, 5, 6, 8, 10, 12, 14, 16)
    if depth >= width
)

ENGINEERED_RECTANGULAR_DIMENSIONS = {
    "lvl": {
        "widths": (1.75,),
        "depths": (5.5, 7.25, 9.25, 9.5, 11.25, 11.875, 14.0, 16.0, 18.0, 20.0, 22.0, 24.0),
    },
    "lsl": {
        "widths": (1.25, 1.5, 1.75, 3.5, 5.25, 7.0),
        "depths": (5.5, 7.25, 9.25, 9.5, 11.25, 11.875, 14.0, 16.0, 18.0, 20.0, 22.0, 24.0),
    },
    "psl": {
        "widths": (3.5, 5.25, 7.0),
        "depths": (3.5, 5.25, 7.0, 9.25, 9.5, 11.25, 11.875, 14.0, 16.0, 18.0),
    },
    "glulam": {
        "widths": (3.125, 3.5, 5.125, 5.5, 6.75, 8.75),
        "depths": (6.0, 7.5, 9.0, 10.5, 12.0, 13.5, 15.0, 16.5, 18.0, 19.5, 21.0, 22.5, 24.0, 27.0, 30.0),
    },
}

UNICODE_FRACTIONS = {
    "\u00bc": "1/4",
    "\u00bd": "1/2",
    "\u00be": "3/4",
    "\u215b": "1/8",
    "\u215c": "3/8",
    "\u215d": "5/8",
    "\u215e": "7/8",
}


def _format_dimension(value: float) -> str:
    return f"{value:g}"


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


ENGINEERED_MEMBER_SIZE_VALUES = _unique(
    tuple(
        f"{_format_dimension(width)}x{_format_dimension(depth)}"
        for product in ENGINEERED_RECTANGULAR_DIMENSIONS.values()
        for width in product["widths"]
        for depth in product["depths"]
        if depth >= width
    )
)

MEMBER_SIZE_VALUES = _unique(SAWN_MEMBER_SIZE_VALUES + ENGINEERED_MEMBER_SIZE_VALUES)
MEMBER_SIZE_VALUE_SET = set(MEMBER_SIZE_VALUES)

MemberSizeLiteral = TypeAliasType("MemberSizeLiteral", Literal[*MEMBER_SIZE_VALUES])  # type: ignore[valid-type]
EWPProductType = TypeAliasType("EWPProductType", Literal["LVL", "LSL", "PSL", "glulam"])


def _dimension_text_for_lookup(size: str) -> str:
    text = size.lower()
    for symbol, fraction in UNICODE_FRACTIONS.items():
        text = text.replace(symbol, f" {fraction}")
    text = text.replace("\u00d7", "x").replace("'", "").replace('"', "")
    text = re.sub(r"\b(?:inches|inch|in)\.?\b", "", text)
    text = re.sub(r"\s*[xX]\s*", "x", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_dimension(value: str) -> float:
    text = value.strip().replace('"', "").replace("'", "")
    for symbol, fraction in UNICODE_FRACTIONS.items():
        text = text.replace(symbol, f" {fraction}")
    text = re.sub(r"\b(?:inches|inch|in)\.?\b", "", text.lower())
    text = re.sub(r"(?<=\d)\s*-\s*(?=\d)", " ", text)
    total = 0.0
    for part in text.split():
        if "/" in part:
            numerator, denominator = part.split("/", 1)
            total += float(numerator) / float(denominator)
        else:
            total += float(part)
    return total


def _extract_rectangular_dims(size: str) -> tuple[float, float] | None:
    text = _dimension_text_for_lookup(size)
    dim = r"\d+(?:\.\d+)?(?:\s*-\s*\d+/\d+|\s+\d+/\d+)?|\d+/\d+"
    match = re.search(rf"(?P<b>{dim})\s*x\s*(?P<d>{dim})", text)
    if not match:
        return None
    return _parse_dimension(match.group("b")), _parse_dimension(match.group("d"))


def canonicalize_member_size(value: object) -> object:
    if value is None or not isinstance(value, str):
        return value

    raw = value.strip()
    if not raw:
        return raw

    lookup = _dimension_text_for_lookup(raw)
    if lookup in MEMBER_SIZE_VALUE_SET:
        return lookup

    dims = _extract_rectangular_dims(raw)
    if dims is None:
        return lookup

    canonical = f"{_format_dimension(dims[0])}x{_format_dimension(dims[1])}"
    return canonical if canonical in MEMBER_SIZE_VALUE_SET else lookup


CanonicalMemberSize = TypeAliasType(
    "CanonicalMemberSize",
    Annotated[MemberSizeLiteral, BeforeValidator(canonicalize_member_size)],
)