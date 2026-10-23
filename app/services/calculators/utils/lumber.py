"""
lumber.py
Reference values, section properties, and CF lookup for multiple species and grades.
"""

import re

ACTUAL_DIMENSION_IN = {
    2: 1.5,
    3: 2.5,
    4: 3.5,
    5: 4.5,
    6: 5.5,
    8: 7.25,
    10: 9.25,
    12: 11.25,
    14: 13.25,
    16: 15.25,
}

EWP_RECTANGULAR_PRODUCTS = {
    "lvl": {
        "aliases": ("lvl", "microllam", "microllam lvl"),
        "widths": (1.75, 3.5, 5.25, 7.0),
        "depths": (5.5, 7.25, 9.25, 9.5, 11.25, 11.875, 14.0, 16.0, 18.0, 20.0, 22.0, 24.0),
    },
    "lsl": {
        "aliases": ("lsl", "timberstrand", "timberstrand lsl"),
        "widths": (1.25, 1.5, 1.75, 3.5, 5.25, 7.0),
        "depths": (5.5, 7.25, 9.25, 9.5, 11.25, 11.875, 14.0, 16.0, 18.0, 20.0, 22.0, 24.0),
    },
    "psl": {
        "aliases": ("psl", "parallam", "parallam psl"),
        "widths": (1.75, 3.5, 5.25, 7.0),
        "depths": (3.5, 5.25, 7.0, 9.25, 9.5, 11.25, 11.875, 14.0, 16.0, 18.0, 20.0, 22.0, 24.0),
    },
    "glulam": {
        "aliases": ("glulam", "glb", "glue laminated", "glue-laminated"),
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


def _rect_section_props(b: float, d: float) -> dict[str, float]:
    return {
        "b": b,
        "d": d,
        "A": b * d,
        "S": (b * (d**2)) / 6,
        "I": (b * (d**3)) / 12,
    }


def _format_dimension(value: float) -> str:
    return f"{value:g}"


def _add_engineered_rectangular_props(props: dict[str, dict[str, float]]) -> None:
    """Add rectangular EWP section geometries; material values stay product-specific."""
    for product in EWP_RECTANGULAR_PRODUCTS.values():
        widths: tuple[float, ...] = product["widths"]  # type: ignore[assignment]
        depths: tuple[float, ...] = product["depths"]  # type: ignore[assignment]
        for width in widths:
            for depth in depths:
                if depth < width:
                    continue
                key = f"{_format_dimension(width)}x{_format_dimension(depth)}"
                props.setdefault(key, _rect_section_props(width, depth))
                for alias in product["aliases"]:
                    props.setdefault(f"{alias} {key}", props[key])


def _build_section_props() -> dict[str, dict[str, float]]:
    """Build nominal sawn-lumber lookup using dressed dimensions."""
    props: dict[str, dict[str, float]] = {}
    nominal_widths = (2, 3, 4, 5, 6, 8, 10, 12)
    nominal_depths = (2, 3, 4, 5, 6, 8, 10, 12, 14, 16)

    for nominal_width in nominal_widths:
        for nominal_depth in nominal_depths:
            if nominal_depth < nominal_width:
                continue
            b = ACTUAL_DIMENSION_IN[nominal_width]
            d = ACTUAL_DIMENSION_IN[nominal_depth]
            props[f"{nominal_width}x{nominal_depth}"] = _rect_section_props(b, d)
    _add_engineered_rectangular_props(props)
    return props


# Section properties for sawn lumber (b, d, A, S, I).
# Existing rounded values are preserved for legacy calculation stability.
SECTION_PROPS = _build_section_props()
SECTION_PROPS.update({
    "2x4": {"b": 1.5, "d": 3.5, "A": 5.25, "S": 3.063, "I": 5.359},
    "2x6": {"b": 1.5, "d": 5.5, "A": 8.25, "S": 7.563, "I": 20.80},
    "2x8": {"b": 1.5, "d": 7.25, "A": 10.875, "S": 13.14, "I": 47.63},
    "2x10": {"b": 1.5, "d": 9.25, "A": 13.875, "S": 21.39, "I": 98.93},
    "2x12": {"b": 1.5, "d": 11.25, "A": 16.875, "S": 31.64, "I": 177.98},
    "4x4": {"b": 3.5, "d": 3.5, "A": 12.25, "S": 7.146, "I": 12.505},
})

# Base reference values (psi) for 2x4 sizes
# ADDED Fc_perp (Fc_p)
REF_VALUES = {
    "DF-L": {
        "Select Structural": {"Fb": 1500, "Fv": 180, "Fc": 1700, "Fc_p": 625, "E": 1900000, "Emin": 690000, "Density": 36.7},
        "No. 1": {"Fb": 1000, "Fv": 180, "Fc": 1500, "Fc_p": 625, "E": 1700000, "Emin": 620000, "Density": 36.7},
        "No. 2": {"Fb": 900, "Fv": 180, "Fc": 1350, "Fc_p": 625, "E": 1600000, "Emin": 580000, "Density": 36.7},
        "Stud": {"Fb": 700, "Fv": 180, "Fc": 850, "Fc_p": 625, "E": 1400000, "Emin": 510000, "Density": 36.7},
    },
    "SP": { 
        "Select Structural": {"Fb": 2400, "Fv": 175, "Fc": 1800, "Fc_p": 565, "E": 1800000, "Emin": 660000, "Density": 37.6},
        "No. 1": {"Fb": 1500, "Fv": 175, "Fc": 1550, "Fc_p": 565, "E": 1600000, "Emin": 580000, "Density": 37.6},
        "No. 2": {"Fb": 1100, "Fv": 175, "Fc": 1450, "Fc_p": 565, "E": 1400000, "Emin": 510000, "Density": 37.6},
        "Stud": {"Fb": 750, "Fv": 175, "Fc": 900, "Fc_p": 565, "E": 1200000, "Emin": 440000, "Density": 37.6},
    },
    "SPF": { 
        "Select Structural": {"Fb": 1250, "Fv": 135, "Fc": 1400, "Fc_p": 425, "E": 1500000, "Emin": 550000, "Density": 29.3},
        "No. 1": {"Fb": 875, "Fv": 135, "Fc": 1150, "Fc_p": 425, "E": 1400000, "Emin": 510000, "Density": 29.3},
        "No. 2": {"Fb": 875, "Fv": 135, "Fc": 1150, "Fc_p": 425, "E": 1400000, "Emin": 510000, "Density": 29.3},
        "Stud": {"Fb": 675, "Fv": 135, "Fc": 725, "Fc_p": 425, "E": 1200000, "Emin": 440000, "Density": 29.3},
    },
    "HF": { 
        "Select Structural": {"Fb": 1300, "Fv": 150, "Fc": 1500, "Fc_p": 405, "E": 1600000, "Emin": 580000, "Density": 29.4},
        "No. 1": {"Fb": 975, "Fv": 150, "Fc": 1350, "Fc_p": 405, "E": 1500000, "Emin": 550000, "Density": 29.4},
        "No. 2": {"Fb": 850, "Fv": 150, "Fc": 1300, "Fc_p": 405, "E": 1300000, "Emin": 470000, "Density": 29.4},
        "Stud": {"Fb": 675, "Fv": 150, "Fc": 850, "Fc_p": 405, "E": 1200000, "Emin": 440000, "Density": 29.4},
    },
    "1.55E LSL": {
        "1.55E": {"Fb": 2325, "Fv": 310, "Fc": 1750, "Fc_p": 675, "E": 1550000, "Emin": 787000, "Density": 45.0}
    },
    "2.0E LVL": {
        "2.0E": {"Fb": 2600, "Fv": 285, "Fc": 2500, "Fc_p": 750, "E": 2000000, "Emin": 1016000, "Density": 42.0}
    },
    "2.0E PSL": {
        "2.0E": {"Fb": 2900, "Fv": 290, "Fc": 2900, "Fc_p": 625, "E": 2000000, "Emin": 1016000, "Density": 45.0}
    },
    "2.2E PSL": {
        "2.2E": {"Fb": 2900, "Fv": 290, "Fc": 2900, "Fc_p": 625, "E": 2200000, "Emin": 1118190, "Density": 45.0}
    },
    "24F-V4 Glulam": {
        "24F-V4": {"Fb": 2400, "Fv": 265, "Fc": 1850, "Fc_p": 650, "E": 1800000, "Emin": 950000, "Density": 35.0}
    }
}

def normalize_species(species: str) -> str:
    s = str(species).lower().strip()
    if s in ["df", "dfl", "df-l", "doug fir"]: return "DF-L"
    if s in ["sp", "southern pine"]: return "SP"
    if s in ["spf", "spruce pine fir"]: return "SPF"
    if s in ["hf", "hem fir", "hem-fir"]: return "HF"
    if s in ["lvl", "microllam", "microllam lvl", "2.0e lvl"]: return "2.0E LVL"
    if s in ["lsl", "timberstrand", "timberstrand lsl", "1.55e lsl"]: return "1.55E LSL"
    if s in ["psl", "parallam", "parallam psl", "2.0e psl", "2.0e parallam psl"]: return "2.0E PSL"
    if s in ["2.2e psl", "2.2e parallam psl"]: return "2.2E PSL"
    if s in ["glulam", "glb", "glue laminated", "glue-laminated", "24f-v4", "24f-v4 glulam"]: return "24F-V4 Glulam"
    return species

def normalize_grade(grade: str) -> str:
    g = str(grade).lower().strip()
    if g in ["1", "no1", "no 1", "no. 1", "#1"]: return "No. 1"
    if g in ["2", "no2", "no 2", "no. 2", "#2"]: return "No. 2"
    if g in ["ss", "select", "select structural"]: return "Select Structural"
    if g in ["stud"]: return "Stud"
    if "1.55" in g: return "1.55E"
    if "2.0" in g: return "2.0E"
    if "2.2" in g: return "2.2E"
    if "24f" in g: return "24F-V4"
    return grade

def normalize_member_size(size: str | None) -> str | None:
    """Normalize extracted member dimensions to the calculator lookup format."""
    if size is None:
        return None
    s = str(size).strip()
    if not s:
        return s
    s = re.sub(r"\s*[xX]\s*", "x", s)
    return re.sub(r"\s+", " ", s)


def _dimension_text_for_lookup(size: str) -> str:
    s = size.lower()
    for symbol, fraction in UNICODE_FRACTIONS.items():
        s = s.replace(symbol, f" {fraction}")
    s = s.replace("\u00d7", "x").replace("'", "").replace('"', "")
    s = re.sub(r"\b(?:inches|inch|in)\.?\b", "", s)
    s = re.sub(r"\s*[xX]\s*", "x", s)
    return re.sub(r"\s+", " ", s).strip()


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


def get_CF(size: str, stress_type: str, species: str, grade: str) -> float:
    """Size Factor (CF) per NDS Supplement Tables."""
    norm_size = normalize_member_size(size)
    if not norm_size:
        return 1.0
    species = normalize_species(species)
    grade = normalize_grade(grade)
    
    if grade == "Stud":
        return 1.0

    if any(product in species for product in ("LSL", "LVL", "PSL", "Glulam")):
        return 1.0
        
    if species == "SP":
        sp_cf_fb = {"2x4": 1.0, "2x6": 0.90, "2x8": 0.84, "2x10": 0.68, "2x12": 0.65, "4x4": 1.0}
        sp_cf_fc = {"2x4": 1.0, "2x6": 0.95, "2x8": 0.93, "2x10": 0.90, "2x12": 0.86, "4x4": 1.0}
        if stress_type == "Fb": return sp_cf_fb.get(norm_size, 1.0)
        elif stress_type == "Fc": return sp_cf_fc.get(norm_size, 1.0)
        return 1.0
    else:
        cf_fb = {"2x4": 1.5, "2x6": 1.3, "2x8": 1.2, "2x10": 1.1, "2x12": 1.0, "4x4": 1.5}
        cf_fc = {"2x4": 1.15, "2x6": 1.1, "2x8": 1.05, "2x10": 1.0, "2x12": 0.9, "4x4": 1.15}
        if stress_type == "Fb": return cf_fb.get(norm_size, 1.0)
        elif stress_type == "Fc": return cf_fc.get(norm_size, 1.0)
        return 1.0

def get_section_props(size: str, plies: int = 1):
    norm_size = normalize_member_size(size)
    if not norm_size:
        raise ValueError(f"Size '{size}' not found and not a valid format.")
    lookup_keys = [norm_size]
    stripped = _dimension_text_for_lookup(norm_size)
    lookup_keys.extend([norm_size.lower(), stripped])

    base = next((SECTION_PROPS[key] for key in lookup_keys if key in SECTION_PROPS), None)
    if base is None:
        dims = _extract_rectangular_dims(norm_size)
        if dims is None:
            raise ValueError(f"Size '{norm_size}' not found and not a valid format.")
        b_float, d_float = dims
        base = _rect_section_props(b_float, d_float)

    return {
        "b": base["b"] * plies,
        "d": base["d"],
        "A": base["A"] * plies,
        "S": base["S"] * plies,
        "I": base["I"] * plies
    }

def get_ref_values(species: str, grade: str):
    species = normalize_species(species)
    grade = normalize_grade(grade)
    if species in REF_VALUES and grade not in REF_VALUES[species] and len(REF_VALUES[species]) == 1:
        grade = next(iter(REF_VALUES[species]))
    if species not in REF_VALUES or grade not in REF_VALUES[species]:
        raise ValueError(f"Material '{species}' grade '{grade}' not found in library.")
    return REF_VALUES[species][grade].copy()