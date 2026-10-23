"""
shear_wall_calc.py
Shear Wall Calculator — SDPWS 2021 / NDS 2024 / ASCE 7-22.

Whole-wall analysis matching Tedds-level rigor:
  - SEGMENT — Segmented equal-deflection method per SDPWS 2021 §4.3.5.1
    with stiffness-based shear distribution across all full-height piers
  - PERF (Perforated) — per SDPWS 2021 §4.3.5.3, with Co adjustment
  - FTAO — Force Transfer Around Openings per APA T555

Checks:
  - Unit shear capacity (wind & seismic, combined both-sides per §4.3.5.4.1)
  - Per-chord tension, compression, bearing at every pier boundary (NDS)
  - Wind deflection per segment (3-term with Ga, h/600 serviceability limit)
  - Seismic deflection per segment (Cd/Ie amplified, 0.020h drift limit)
  - Collector force and peak collector stress
  - Holdown forces at each chord
  - Anchor bolt and sill plate design
  - ASD load combinations per ASCE 7-22 §2.4.5
"""

import re
import math
from dataclasses import dataclass, field
from typing import Optional, List, Any

from services.calculators.utils.lumber import normalize_member_size


# ============================================================
# §1 — Lookup Tables
# ============================================================

# SDPWS 2021 Table 4.3A — Nominal unit shear capacity, seismic (plf)
# key: (panel thickness, nail size) -> {edge nail spacing in: vs nominal}
TABLE_4_3A_SEISMIC = {
    ("3/8", "8d"): {6: 440, 4: 640, 3: 820, 2: 1060},
    ("7/16", "8d"): {6: 480, 4: 700, 3: 900, 2: 1170},
    ("15/32", "8d"): {6: 520, 4: 760, 3: 980, 2: 1280},
    ("15/32", "10d"): {6: 680, 4: 1020, 3: 1330, 2: 1740},
}
WIND_FACTOR = 1.4  # SDPWS Table 4.3A: tabulated wind = 1.4 × seismic

# SDPWS 2021 Table 4.3A — Apparent shear wall shear stiffness Ga (kips/in)
TABLE_4_3A_GA = {
    ("3/8", "8d"): {6: 17, 4: 25, 3: 31, 2: 45},
    ("7/16", "8d"): {6: 15, 4: 22, 3: 28, 2: 42},
    ("15/32", "8d"): {6: 13, 4: 19, 3: 25, 2: 39},
    ("15/32", "10d"): {6: 22, 4: 30, 3: 37, 2: 52},
}


def lookup_unit_shear(thickness: str, nail: str, spacing) -> tuple:
    """Return (seismic nominal, wind nominal, note) per SDPWS 2021 Table 4.3A."""
    nail_key = "10d" if "10" in str(nail) else "8d"
    spacing = int(spacing)
    note = None
    row = TABLE_4_3A_SEISMIC.get((thickness, nail_key))
    if row is None:
        row = TABLE_4_3A_SEISMIC.get((thickness, "8d"))
        if row is not None:
            note = (
                f'SDPWS Table 4.3A does not tabulate {thickness}" panels with '
                f"{nail_key} nails — 8d values used (conservative)."
            )
    if row is None:
        return 0, 0, f'No SDPWS Table 4.3A entry for {thickness}" panels.'
    vs = row.get(spacing)
    if vs is None:
        vs = row.get(6, 0)
        note = f'Edge nail spacing {spacing}" not tabulated — 6" values used (conservative).'
    return vs, round(vs * WIND_FACTOR), note


def lookup_Ga(thickness: str, nail: str, spacing) -> float:
    """Return Ga (kips/in) per SDPWS 2021 Table 4.3A."""
    nail_key = "10d" if "10" in str(nail) else "8d"
    spacing = int(spacing)
    row = TABLE_4_3A_GA.get((thickness, nail_key))
    if row is None:
        row = TABLE_4_3A_GA.get((thickness, "8d"))
    if row is None:
        return 15.0
    ga = row.get(spacing)
    if ga is None:
        ga = row.get(6, 15)
    return float(ga)


# Anchor bolt Z-parallel (lbs) — key: (ab_dia, sill_type)
AB_Z_PARA = {
    0.500: [530, 680, 680, 690],
    0.625: [780, 940, 940, 1070],
    0.750: [1100, 1240, 1240, 1470],
    0.875: [1280, 1600, 1600, 1840],
    1.000: [1460, 2030, 2030, 2260],
}
AB_Z_PERP = {
    0.500: [290, 340, 340, 400],
    0.625: [320, 460, 460, 520],
    0.750: [350, 580, 580, 660],
    0.875: [370, 610, 610, 820],
    1.000: [410, 680, 680, 950],
}
SILL_TYPE_MAP = {"(1)-2x": 0, "(1)-3x": 1, "(2)-2x": 2, "(3)-2x": 3}

# Chord cross-sectional area A (in²)
CHORD_AREA = {
    "(1) 2x6": 8.25,
    "(2) 2x6": 16.5,
    "(3) 2x6": 24.75,
    "(4) 2x6": 33.0,
    "4x4": 12.25,
    "4x6": 19.25,
    "6x6": 30.25,
    "6x8": 39.875,
    "(1) 2x4": 5.25,
    "(2) 2x4": 10.5,
    "(3) 2x4": 15.75,
    "(1) 2x8": 10.875,
    "(2) 2x8": 21.75,
    "(3) 2x8": 32.625,
}

# Holdown deflection da (in)
DA_COLUMNS = [
    "(1) 2x6",
    "(2) 2x6",
    "(3) 2x6",
    "(4) 2x6",
    "4x4",
    "4x6",
    "6x6",
    "6x8",
    "(1) 2x4",
    "(2) 2x4",
    "(3) 2x4",
    "(1) 2x8",
    "(2) 2x8",
    "(3) 2x8",
]
DA_TABLE: dict[str, list[float | None]] = {
    "DTT2Z": [
        0.105,
        0.128,
        0.128,
        0.128,
        0.128,
        0.128,
        0.128,
        0.128,
        0.105,
        0.128,
        0.128,
        0.105,
        0.128,
        0.128,
    ],
    "HDU2": [
        None,
        0.088,
        0.088,
        0.088,
        0.088,
        0.088,
        0.088,
        0.088,
        None,
        0.088,
        0.088,
        None,
        0.088,
        0.088,
    ],
    "HDU4": [
        None,
        0.114,
        0.114,
        0.114,
        0.114,
        0.114,
        0.114,
        0.114,
        None,
        0.114,
        0.114,
        None,
        0.114,
        0.114,
    ],
    "HDU5": [
        None,
        0.115,
        0.115,
        0.115,
        0.115,
        0.115,
        0.115,
        0.115,
        None,
        0.115,
        0.115,
        None,
        0.115,
        0.115,
    ],
    "HDU8": [
        None,
        0.084,
        0.113,
        0.113,
        0.116,
        0.116,
        0.113,
        0.113,
        None,
        0.084,
        0.113,
        None,
        0.084,
        0.113,
    ],
    "HDU11": [
        None,
        None,
        None,
        0.137,
        None,
        0.137,
        0.137,
        0.137,
        None,
        None,
        None,
        None,
        None,
        None,
    ],
    "HDU14": [
        None,
        None,
        None,
        0.177,
        None,
        0.122,
        0.177,
        0.177,
        None,
        None,
        None,
        None,
        None,
        None,
    ],
    "STHD10": [
        None,
        0.146,
        0.146,
        0.146,
        0.146,
        0.146,
        0.146,
        0.146,
        None,
        0.146,
        0.146,
        None,
        0.146,
        0.146,
    ],
    "STHD10RJ": [
        None,
        0.146,
        0.146,
        0.146,
        0.146,
        0.146,
        0.146,
        0.146,
        None,
        0.146,
        0.146,
        None,
        0.146,
        0.146,
    ],
    "STHD14": [
        None,
        0.164,
        0.164,
        0.164,
        0.164,
        0.164,
        0.164,
        0.164,
        None,
        0.164,
        0.164,
        None,
        0.164,
        0.164,
    ],
    "STHD14RJ": [
        None,
        0.164,
        0.164,
        0.164,
        0.164,
        0.164,
        0.164,
        0.164,
        None,
        0.164,
        0.164,
        None,
        0.164,
        0.164,
    ],
}

# Holdown allowable tension (lbs)
HOLDOWN_ALLOWABLE = {
    "DTT2Z": (0.500, 3, 1835, 2145, 3),
    "HDU2": (0.625, 3, 2215, 3075, 3),
    "HDU4": (0.625, 3, 3285, 4565, 3),
    "HDU5": (0.625, 3, 4065, 5645, 3),
    "HDU8": (0.875, 4.5, 5665, 7870, 4.5),
    "HDU11": (1.000, 5.5, 6865, 9535, 5.5),
    "HDU14": (1.000, 5.5, 10350, 14445, 5.5),
}

# Framing clips
CLIPS: dict[str, dict[str, Any]] = {
    "A35": {
        "allowable": 600,
        "direction": "F1",
        "note": "Provide full depth blocking with A35 clips to top plt. per plan.",
    },
    "A34": {
        "allowable": 445,
        "direction": "F1",
        "note": "Provide full depth blocking with A34 clips to top plt. per plan.",
    },
    "LTP4": {
        "allowable": 575,
        "direction": "G",
        "note": "Provide positive connection btw. sheathing with LTP4 clips per plan.",
    },
    "LTP5": {
        "allowable": 535,
        "direction": "G",
        "note": "Provide positive connection btw. sheathing with LTP5 clips per plan.",
    },
    "H1": {
        "allowable": 415,
        "direction": "F1",
        "note": "Provide full depth blocking with H1 ties to top plt. per plan.",
    },
}

# Plate bearing Fc_perp (psi) — by species abbreviation
PLATE_FC_PERP = {"DF": 625, "HF": 405, "SP": 565, "SPF": 425}

# ── Species / grade reference design values (NDS 2024 Supplement) ──

# Size factor CF — NDS Supplement Table 4A footnotes
# Indexed by nominal depth; applies to all species EXCEPT Southern Pine
_CF_TABLE = {
    4: (1.5, 1.15),
    6: (1.3, 1.1),
    8: (1.2, 1.05),
    10: (1.1, 1.0),
    12: (1.0, 1.0),
}

# Base design values BEFORE size factor — NDS Supplement Table 4A
# species+grade: (Ft, Fc, E, Emin)
_SPECIES_BASE = {
    "DF No. 1": (1000, 1500, 1700000, 620000),
    "DF No. 2": (575, 1350, 1600000, 580000),
    "DF Stud": (450, 850, 1400000, 510000),
    "HF No. 1": (725, 1350, 1500000, 550000),
    "HF No. 2": (500, 1300, 1300000, 470000),
    "HF Stud": (350, 725, 1200000, 440000),
    "SPF No. 1": (500, 1150, 1500000, 550000),
    "SPF No. 2": (400, 1050, 1400000, 510000),
    "SPF Stud": (275, 650, 1200000, 440000),
}

# Southern Pine — NDS Supplement Table 4B (CF already included)
# (grade, nominal_depth) -> (Ft, Fc)
_SP_VALUES = {
    ("SP No. 1", 4): (800, 1850),
    ("SP No. 1", 6): (750, 1650),
    ("SP No. 1", 8): (700, 1500),
    ("SP No. 1", 10): (650, 1350),
    ("SP No. 1", 12): (550, 1100),
    ("SP No. 2", 4): (550, 1650),
    ("SP No. 2", 6): (500, 1400),
    ("SP No. 2", 8): (450, 1200),
    ("SP No. 2", 10): (400, 1050),
    ("SP No. 2", 12): (375, 850),
    ("SP Stud", 4): (400, 850),
    ("SP Stud", 6): (350, 750),
}
_SP_MODULI = {
    "SP No. 1": (1700000, 620000),
    "SP No. 2": (1600000, 580000),
    "SP Stud": (1300000, 470000),
}

# Chord geometry (species-independent) — size: (dx_in, lb_in)
_CHORD_GEOM = {
    "(1) 2x4": (3.5, 1.5),
    "(2) 2x4": (3.5, 3.0),
    "(3) 2x4": (3.5, 4.5),
    "(1) 2x6": (5.5, 1.5),
    "(2) 2x6": (5.5, 3.0),
    "(3) 2x6": (5.5, 4.5),
    "(4) 2x6": (5.5, 6.0),
    "(1) 2x8": (7.25, 1.5),
    "(2) 2x8": (7.25, 3.0),
    "(3) 2x8": (7.25, 4.5),
    "4x4": (3.5, 3.5),
    "4x6": (5.5, 3.5),
    "6x6": (5.5, 5.5),
    "6x8": (5.5, 7.25),
}


def _get_chord_props(species_grade, chord_studs):
    """Look up chord material + geometry for species/grade and stud config.

    Returns (Ft, Fc, CFt, CFc, Emin, E, dx, lb).
    """
    geom = _CHORD_GEOM.get(chord_studs, (5.5, 3.0))
    dx, lb = geom

    m = re.match(r"(?:\(\d+\)\s*)?(\d+)x(\d+)", chord_studs)
    nom_thick = int(m.group(1)) if m else 2
    nom_depth = int(m.group(2)) if m else 6

    # Timber sizes (6x+) — DF P&T No. 2 values as default
    if nom_thick >= 6:
        return (425, 700, 1.0, 1.0, 470000, 1300000, dx, lb)

    is_sp = species_grade.startswith("SP") and not species_grade.startswith("SPF")

    if is_sp:
        moduli = _SP_MODULI.get(species_grade, _SP_MODULI["SP No. 2"])
        E, Emin = moduli
        sp_depth = 6 if nom_depth == 5 else nom_depth
        sp_vals = _SP_VALUES.get((species_grade, sp_depth))
        if sp_vals is None:
            sp_vals = _SP_VALUES.get((species_grade, 6), (500, 1400))
        Ft, Fc = sp_vals
        return (Ft, Fc, 1.0, 1.0, Emin, E, dx, lb)

    base = _SPECIES_BASE.get(species_grade, _SPECIES_BASE["DF No. 2"])
    Ft, Fc, E, Emin = base
    CFt, CFc = _CF_TABLE.get(nom_depth, (1.0, 1.0))
    return (Ft, Fc, CFt, CFc, Emin, E, dx, lb)


# Deflection constants (legacy 4-term method)
EN_BASE = 616
EN_EXP = 3.018
STRUCT_I_FACTOR = 1.2
GT_STIFFNESS = 83500  # plf, SDPWS Table C4.2.2A
E_CHORD = 1600000  # psi


# ============================================================
# §2 — Utility Helpers
# ============================================================


def _shear_status(ok, ok_str, fail_str):
    """Map a tri-state shear check (None = not applicable / governed by aspect
    ratio, True = pass, False = fail) to a status string."""
    if ok is None:
        return "N/A"
    return ok_str if ok else fail_str


def _cb(lb):
    """Bearing area factor Cb per NDS §3.10.4."""
    if lb < 6:
        return (lb + 0.375) / lb
    return 1.0


def _normalize_chord_studs(chord_studs: str) -> str:
    chord = normalize_member_size(chord_studs) or ""
    return re.sub(r"^\((\d+)\)\s*", r"(\1) ", chord)


def _parse_end_post(chord_studs: str):
    """Parse end post string -> (n_plies, stud_depth_in, stud_width_in)."""
    m = re.match(r"\((\d+)\)\s*(\d+)x(\d+)", chord_studs)
    if m:
        n = int(m.group(1))
        nom_w = int(m.group(2))
        nom_d = int(m.group(3))
        width = 1.5 if nom_w <= 3 else 3.5
        depth_map = {3: 2.5, 4: 3.5, 6: 5.5, 8: 7.25, 10: 9.25, 12: 11.25}
        return n, depth_map.get(nom_d, nom_d - 0.5), width
    if "4x" in chord_studs:
        d = int(chord_studs.split("x")[1]) if "x" in chord_studs else 4
        return 1, {4: 3.5, 6: 5.5, 8: 7.25}.get(d, 3.5), 3.5
    if "6x" in chord_studs:
        d = int(chord_studs.split("x")[1]) if "x" in chord_studs else 6
        return 1, {6: 5.5, 8: 7.25}.get(d, 5.5), 5.5
    return 2, 5.5, 1.5


def _run_ftao(inp: "ShearWallInputs") -> dict:
    """Bridge ShearWallInputs -> ftao_calc.check_ftao (APA T555 method)."""
    from services.calculators.ftao_calc import check_ftao, FTAOInputs, FTAOOpening

    openings = [
        FTAOOpening(
            width_ft=float(o.get("width_ft") or 0),
            ho_ft=float(o.get("ho_ft") or 0),
            ha_ft=float(o.get("ha_ft") or 0),
            hb_ft=float(o.get("hb_ft") or 0),
        )
        for o in (inp.openings or [])
    ]
    piers = [float(p) for p in (inp.pier_lengths or [])]
    if len(piers) != len(openings) + 1:
        raise ValueError(
            f"FTAO wall needs {len(openings) + 1} pier lengths for "
            f"{len(openings)} openings, got {len(piers)}."
        )

    chord = _normalize_chord_studs(inp.chord_studs)
    m = re.match(r"\((\d+)\)\s*(\S+)", chord)
    chord_qty, chord_size = (int(m.group(1)), m.group(2)) if m else (2, "2x6")

    hd = HOLDOWN_ALLOWABLE.get(inp.holdown_model)
    da_row = DA_TABLE.get(inp.holdown_model)
    da = 0.128
    if da_row and chord in DA_COLUMNS:
        v = da_row[DA_COLUMNS.index(chord)]
        if v is not None:
            da = v

    nail_size = "10d" if "10" in str(inp.fastener_type) else "8d"

    def _ftao_run(V, load_type):
        hd_capacity = (hd[3] if load_type == "wind" else hd[2]) if hd else 2145
        fin = FTAOInputs(
            V_lbs=V,
            pier_lengths_ft=piers,
            openings=openings,
            load_type=load_type,
            panel_material=inp.panel_material,
            panel_grade=inp.panel_grade,
            panel_thickness=inp.panel_thickness,
            nail_size=nail_size,
            nail_spacing_in=int(inp.nail_edge_spacing),
            sheathing_both_sides=inp.sheathing_both_sides,
            hd_capacity_lbs=hd_capacity,
            hd_deflection_in=da,
            chord_qty=chord_qty,
            chord_stud_size=chord_size,
        )
        return check_ftao(fin)

    results = []
    if inp.Vw > 0:
        results.append(("wind", _ftao_run(inp.Vw, "wind")))
    if inp.Vs > 0:
        results.append(("seismic", _ftao_run(inp.Vs, "seismic")))
    if not results:
        return _ftao_run(0, "wind")

    def _max_ratio(r):
        res = r.get("results", {})
        ratios = [v.get("Result", 0) for v in res.values() if isinstance(v, dict)]
        return max(ratios) if ratios else 0

    return max(results, key=lambda x: _max_ratio(x[1]))[1]


def compute_perforated_co(
    total_length_ft: float,
    wall_height_ft: float,
    sum_li_ft: float,
    opening_area_sf: float,
) -> float:
    """Co per SDPWS 2021 §4.3.5.3 (Eq. 4.3-6).

    Equivalent to Tedds: Co = Awall / (3*Ao + Afhs)
    """
    if wall_height_ft <= 0 or sum_li_ft <= 0 or total_length_ft <= 0:
        return 1.0
    r = 1.0 / (1.0 + opening_area_sf / (wall_height_ft * sum_li_ft))
    co = (r / (3.0 - 2.0 * r)) * (total_length_ft / sum_li_ft)
    return min(co, 1.0)


# ============================================================
# §3 — Inputs
# ============================================================
@dataclass
class ShearWallInputs:
    # Loads & geometry
    Vs: float = 2295  # ASD seismic shear (0.7E) at wall (lbs)
    Vw: float = 2815  # ASD wind shear (0.6W) at wall (lbs)
    wall_length_total: float = 46  # Total wall length including openings (ft)
    wall_height: float = 12  # Wall height (ft)
    wall_type: str = "PERF"  # "PERF" / "FTAO" / "SEGMENT"
    Co: float = 0.97  # Perforated adjustment factor
    sum_Li: float = 19  # Sum of full-height segment lengths (ft)
    bs_perf: float = 9.75  # Min panel length (ft, PERF)

    # Sheathing & nailing
    nail_edge_spacing: int = 4
    sheathing_both_sides: bool = False
    panel_thickness: str = "15/32"
    fastener_type: str = "10d"
    panel_material: str = "OSB"
    panel_grade: str = "APA Rated Sheathing"

    # FTAO geometry
    openings: Optional[List[dict]] = None
    pier_lengths: Optional[List[float]] = None

    # Holdown & chord
    holdown_model: str = "HDU2"
    chord_studs: str = "(2) 2x6"
    chord_species: str = "DF No. 2"

    # Sill plate
    sill_plate_type: str = "(1)-2x"
    ab_diameter: float = 0.625

    # Out-of-plane
    WDL: float = 12
    SDS: float = 0.625
    Ie: float = 1.0
    ka: float = 1.0
    rho: float = 1.0
    Ww: float = 18.54
    Ltrib: float = 4.5

    # Collector / clips
    La: float = 8
    clip_model: str = "A35"
    Lac: float = 28
    clip_spacing: float = 24

    # Panels (pier segment lengths, up to any number)
    panels: List[float] = field(default_factory=lambda: [9.75, 7.14, 7.5, 2.0, 5.0])

    # Bearing / chord
    top_sill_species: str = "HF"
    WDL_plf: float = 95
    WLL_plf: float = 40
    WSL_plf: float = 0
    PDL: float = 0
    PLL: float = 0
    PSL: float = 0
    PW: float = 0
    PS: float = 0
    stud_spacing: int = 16

    # --- Whole-wall analysis fields (with backward-compat defaults) ---
    D_head_plf: float = 0  # Dead load at head (0 = use WDL_plf)
    Swt_psf: float = 12  # Wall self weight (psf)
    anchor_stiffness: float = 30000  # Holdown stiffness ka (lb/in)
    bolt_hole_dia: float = 1.0  # Bolt hole in end post (in)
    Cd_defl: float = 4.0  # Deflection amplification factor
    Ie_defl: float = 1.0  # Seismic importance factor
    f_Wserv: float = 0.60  # Wind serviceability factor
    collector_seismic_factor: float = 1.0  # Collector demand amplification

    # Opening geometry (for whole-wall analysis; empty = use panels only)
    opening_widths_ft: List[float] = field(default_factory=list)
    opening_heights_ft: List[float] = field(default_factory=list)
    opening_positions_ft: List[float] = field(default_factory=list)


# ============================================================
# §4 — Whole-Wall Analysis Helpers
# ============================================================


def _ar_factor_perf(h, b):
    """Perforated AR effective length factor: 2b/h for h/b > 2 (SDPWS §4.3.5.6)."""
    if b <= 0:
        return 0
    ar = h / b
    if ar > 3.5:
        return 0
    if ar > 2:
        return 2.0 * b / h
    return 1.0


def _ar_factor_seg(h, b):
    """Segmented AR capacity factor: 1.25 - 0.125(h/b) per SDPWS 2021 §4.3.4.2."""
    if b <= 0:
        return 0
    ar = h / b
    if ar > 3.5:
        return 0
    if ar > 2:
        return 1.25 - 0.125 * ar
    return 1.0


def _segment_stiffness(h_in, E, Ae, Ga_lb, b_in, b_eff_in, ka_anchor, Fc_perp):
    """Segment stiffness k (lb/in) from SDPWS 3-term deflection equation.

    k = 1 / [2h³/(3EAb²) + h/(Ga·b) + h·δa1/b]
    δa1 = (h/b)·(1/ka + 0.04/(Ae·Fc_perp)·(b/b_eff))
    """
    if b_in <= 0 or E <= 0 or Ae <= 0 or Ga_lb <= 0:
        return 0.0

    b_ratio = b_in / b_eff_in if b_eff_in > 0 else 1.0
    fc_safe = max(Fc_perp, 1.0)
    da1 = (h_in / b_in) * (1.0 / max(ka_anchor, 1.0) + 0.04 / (Ae * fc_safe) * b_ratio)

    bending = 2.0 * h_in**3 / (3.0 * E * Ae * b_in**2)
    shear = h_in / (Ga_lb * b_in)
    anchor = h_in * da1 / b_in

    compliance = bending + shear + anchor
    return 1.0 / compliance if compliance > 0 else 0.0


def _three_term_defl_parts(v_pli, h_in, E, Ae, Ga_lb, b_in, da_in):
    """3-term wall deflection per SDPWS Eq. 4.3-1, returned term-by-term (in).

    δ = 2vh³/(3EAb) + vh/Ga + h·da/b
    v in lb/in, h in in, b in in, Ga in lb/in, da in in.
    """
    if b_in <= 0:
        return {"bend": 0.0, "shear_slip": 0.0, "anchor": 0.0, "total": 0.0}
    bend = 2.0 * v_pli * h_in**3 / (3.0 * E * Ae * b_in) if E > 0 and Ae > 0 else 0
    shear = v_pli * h_in / Ga_lb if Ga_lb > 0 else 0
    anch = h_in * da_in / b_in
    return {
        "bend": bend,
        "shear_slip": shear,
        "anchor": anch,
        "total": bend + shear + anch,
    }


def _three_term_defl(v_pli, h_in, E, Ae, Ga_lb, b_in, da_in):
    """3-term wall deflection δ (in) per SDPWS Eq. 4.3-1 (all-inch units)."""
    return _three_term_defl_parts(v_pli, h_in, E, Ae, Ga_lb, b_in, da_in)["total"]


# ============================================================
# §5 — Main Entry Point
# ============================================================
def check_shear_wall(inp: ShearWallInputs):
    """Run whole-wall shear wall analysis. Returns structured dict."""

    # ---- FTAO route ----
    if inp.wall_type == "FTAO" and inp.openings:
        return _run_ftao(inp)

    inp.chord_studs = _normalize_chord_studs(inp.chord_studs)
    h = inp.wall_height
    h_in = h * 12.0
    is_perf = inp.wall_type == "PERF"
    CD = 1.6

    # ---- Local helpers ----
    def _lookup_ab(table):
        idx = SILL_TYPE_MAP.get(inp.sill_plate_type, 0)
        row = table.get(inp.ab_diameter)
        if row is None:
            dias = sorted(table.keys())
            for i in range(len(dias) - 1):
                if dias[i] <= inp.ab_diameter <= dias[i + 1]:
                    lo = table[dias[i]][idx]
                    hi = table[dias[i + 1]][idx]
                    return lo + (hi - lo) * (inp.ab_diameter - dias[i]) / (
                        dias[i + 1] - dias[i]
                    )
            for d in dias:
                if d >= inp.ab_diameter:
                    return table[d][idx]
            return table[dias[-1]][idx]
        return row[idx]

    def _da_lookup(holdown, chord):
        row = DA_TABLE.get(holdown)
        if row is None:
            return None
        try:
            idx = DA_COLUMNS.index(chord)
        except ValueError:
            return None
        return row[idx]

    # ================================================================
    # MATERIAL PROPERTIES
    # ================================================================
    Ft, Fc, CFT, CFC, Emin, E, dx, lb_bearing = _get_chord_props(
        inp.chord_species, inp.chord_studs
    )
    Ae = CHORD_AREA.get(inp.chord_studs, 16.5)

    n_plies, _, stud_width = _parse_end_post(inp.chord_studs)
    Aen = max(Ae - n_plies * inp.bolt_hole_dia * stud_width, Ae * 0.5)

    # NDS adjustment factors (dry, normal temp: CM=Ct=Ci=CT=1.0)
    Emin_prime = Emin
    le_d = h_in / dx if dx > 0 else 1.0
    FcE = 0.822 * Emin_prime / le_d**2 if le_d > 0 else 0
    Fc_star = Fc * CD * CFC
    c_nds = (
        0.9
        if any(x in inp.chord_species for x in ["LSL", "LVL", "PSL", "EWP", "Glulam"])
        else 0.8
    )
    if Fc_star > 0 and FcE > 0:
        ratio_nds = FcE / Fc_star
        term_nds = (1.0 + ratio_nds) / (2.0 * c_nds)
        disc = term_nds**2 - ratio_nds / c_nds
        Cp = term_nds - math.sqrt(disc) if disc > 0 else 0
    else:
        Cp = 0

    Ft_prime = Ft * CD * CFT
    Fc_prime = Fc_star * Cp
    sill_sp = inp.top_sill_species.split()[0]
    Fc_perp_base = PLATE_FC_PERP.get(sill_sp, 405)
    Cb_val = _cb(lb_bearing)
    Fc_perp_prime = Fc_perp_base * Cb_val

    # ================================================================
    # SHEATHING CAPACITY & STIFFNESS
    # ================================================================
    v_seis_nom, v_wind_nom, table_note = lookup_unit_shear(
        inp.panel_thickness, inp.fastener_type, inp.nail_edge_spacing
    )

    Ga_kips = lookup_Ga(inp.panel_thickness, inp.fastener_type, inp.nail_edge_spacing)
    Ga_lb = Ga_kips * 1000.0

    if inp.sheathing_both_sides:
        v_seis_nom *= 2
        v_wind_nom *= 2
        Ga_lb *= 2

    # ASD allowable unit shear (nominal / Ω, Ω = 2.0)
    v_asd_seis = v_seis_nom / 2.0
    v_asd_wind = v_wind_nom / 2.0

    # ================================================================
    # SEGMENTS
    # ================================================================
    segments = [b for b in inp.panels if b > 0]
    n_seg = len(segments)
    sum_bi = sum(segments)

    # Effective length per segment (centroid-to-centroid of chord packs)
    stud_thickness_in = 1.5
    ep_deduct_in = n_plies * stud_thickness_in
    b_effs_in = [max(b * 12.0 - ep_deduct_in, b * 12.0 * 0.9) for b in segments]

    # Unfactored loads (back-calculated from ASD demands)
    W_unfactored = inp.Vw / 0.6 if inp.Vw > 0 else 0
    Eq_unfactored = inp.Vs / 0.7 if inp.Vs > 0 else 0

    D_head = inp.D_head_plf if inp.D_head_plf > 0 else inp.WDL_plf
    ka_anchor = inp.anchor_stiffness

    governs = "Wind" if inp.Vw > inp.Vs else "Seismic"
    shear_design = max(inp.Vs, inp.Vw)
    unit_shear_v = shear_design / sum_bi if sum_bi > 0 else 0

    # ================================================================
    # SHEAR CAPACITY — PERFORATED
    # ================================================================
    seg_details = []
    seg_stiffnesses = []
    sum_k = 0.0

    if is_perf:
        Co = inp.Co if inp.Co > 0 else 1.0
        bi_adj = [b * _ar_factor_perf(h, b) for b in segments]
        sum_bi_adj = sum(bi_adj)

        wall_Vw_cap = v_asd_wind * Co * sum_bi_adj
        wall_Vs_cap = v_asd_seis * Co * sum_bi_adj

        for i, b in enumerate(segments):
            ar = h / b if b > 0 else 99
            seg_details.append(
                {
                    "id": i + 1,
                    "b_ft": b,
                    "b_adj_ft": round(bi_adj[i], 3),
                    "AR": round(ar, 3),
                    "AR_ok": ar <= 3.5,
                    "AR_factor": round(_ar_factor_perf(h, b), 4),
                }
            )

        vs_unit = inp.Vs / (Co * sum_bi_adj) if (Co * sum_bi_adj) > 0 else 0
        vw_unit = inp.Vw / (Co * sum_bi_adj) if (Co * sum_bi_adj) > 0 else 0

    # ================================================================
    # SHEAR CAPACITY — SEGMENTED EQUAL-DEFLECTION
    # ================================================================
    else:
        for i, b in enumerate(segments):
            b_in = b * 12.0
            b_eff = b_effs_in[i]
            k_i = _segment_stiffness(
                h_in, E, Ae, Ga_lb, b_in, b_eff, ka_anchor, Fc_perp_base
            )
            seg_stiffnesses.append(k_i)

        sum_k = sum(seg_stiffnesses)

        ar_factors = [_ar_factor_seg(h, b) for b in segments]
        v_cap_seis = [v_asd_seis * ar_factors[i] for i in range(n_seg)]
        v_cap_wind = [v_asd_wind * ar_factors[i] for i in range(n_seg)]

        for i, b in enumerate(segments):
            k_i = seg_stiffnesses[i]
            frac = k_i / sum_k if sum_k > 0 else 1.0 / max(n_seg, 1)
            ar = h / b if b > 0 else 99

            Vw_i = inp.Vw * frac
            vw_i = Vw_i / b if b > 0 else 0
            Vs_i = inp.Vs * frac
            vs_i = Vs_i / b if b > 0 else 0

            seg_details.append(
                {
                    "id": i + 1,
                    "b_ft": b,
                    "b_eff_ft": round(b_effs_in[i] / 12.0, 3),
                    "AR": round(ar, 3),
                    "AR_ok": ar <= 3.5,
                    "AR_factor": round(ar_factors[i], 4),
                    "k_kips_per_in": round(k_i / 1000.0, 3),
                    "frac": round(frac, 4),
                    "Vw_lbs": round(Vw_i, 1),
                    "vw_plf": round(vw_i, 2),
                    "vw_cap_plf": round(v_cap_wind[i], 1),
                    "vw_ratio": (
                        round(vw_i / v_cap_wind[i], 4) if v_cap_wind[i] > 0 else 999
                    ),
                    "Vs_lbs": round(Vs_i, 1),
                    "vs_plf": round(vs_i, 2),
                    "vs_cap_plf": round(v_cap_seis[i], 1),
                    "vs_ratio": (
                        round(vs_i / v_cap_seis[i], 4) if v_cap_seis[i] > 0 else 999
                    ),
                }
            )

        wall_Vw_cap = sum(v_cap_wind[i] * segments[i] for i in range(n_seg))
        wall_Vs_cap = sum(v_cap_seis[i] * segments[i] for i in range(n_seg))

        vs_unit = inp.Vs / sum_bi if sum_bi > 0 else 0
        vw_unit = inp.Vw / sum_bi if sum_bi > 0 else 0

    AR_max = max((h / b for b in segments if b > 0), default=0)
    AR_check_ok = AR_max <= 3.5

    # Overall shear checks. When aspect ratio disqualifies the wall (h/bs > 3.5)
    # the shear capacity is zero not because demand exceeds capacity but because
    # the segment is not a permitted shear wall — report the unit-shear check as
    # not-applicable (governed by aspect ratio) rather than a spurious shear FAIL.
    ar_disqualified = not AR_check_ok
    if ar_disqualified:
        Vw_ratio = Vs_ratio = None
        wind_shear_ok = seismic_shear_ok = None
    else:
        Vw_ratio = inp.Vw / wall_Vw_cap if wall_Vw_cap > 0 else 999
        Vs_ratio = inp.Vs / wall_Vs_cap if wall_Vs_cap > 0 else 999
        wind_shear_ok = Vw_ratio <= 1.0
        seismic_shear_ok = Vs_ratio <= 1.0

    # ================================================================
    # CHORD CHECKS — per-segment tension / compression / bearing
    # ================================================================
    chord_checks: list[dict[str, Any]] = []
    max_T = 0.0
    max_C = 0.0

    for i, b in enumerate(segments):
        b_in = b * 12.0
        b_eff = b_effs_in[i]
        b_eff_ft = b_eff / 12.0

        if is_perf:
            Co = inp.Co if inp.Co > 0 else 1.0
            sum_ba = sum(bb * _ar_factor_perf(h, bb) for bb in segments)
            overturning_arm = Co * sum_ba
            OT_seis = inp.Vs * h / overturning_arm if overturning_arm > 0 else 0
            OT_wind = inp.Vw * h / overturning_arm if overturning_arm > 0 else 0
        else:
            frac = seg_stiffnesses[i] / sum_k if sum_k > 0 else 1.0 / max(n_seg, 1)
            V_seg_s = inp.Vs * frac
            V_seg_w = inp.Vw * frac
            OT_seis = V_seg_s * h / b_eff_ft if b_eff_ft > 0 else 0
            OT_wind = V_seg_w * h / b_eff_ft if b_eff_ft > 0 else 0

        # Gravity stabilizing — tributary width is half stud spacing (chord bay)
        trib = inp.stud_spacing / (2.0 * 12.0)
        P_DL = (D_head + inp.Swt_psf * h) * trib + inp.PDL
        P_LL = inp.WLL_plf * trib + inp.PLL
        P_SL = inp.WSL_plf * trib + inp.PSL
        Ev = 0.2 * inp.SDS * P_DL

        # ASCE 7-22 ASD combos for TENSION (minimum gravity):
        # Combo 8: 0.6D + 0.7E → P_stab = 0.6D - 0.7Ev
        P_stab_8 = 0.6 * P_DL - 0.7 * Ev
        T_seis = max(OT_seis - max(P_stab_8, 0), 0)
        # Combo 6a: 0.6D + 0.6W
        P_stab_6a = 0.6 * P_DL
        T_wind = max(OT_wind - max(P_stab_6a, 0), 0)
        T_chord_i = max(T_seis, T_wind)

        # ASCE 7-22 ASD combos for COMPRESSION (maximum gravity):
        # Combo 2: D + 0.7E + 0.7Ev
        C_seis = OT_seis + P_DL + 0.7 * Ev
        # Combo 6: D + 0.6W
        C_wind = OT_wind + P_DL
        # Combo 4: D + 0.75L + 0.75(0.6W) + 0.75Lr
        C_combo4 = P_DL + 0.75 * P_LL + 0.75 * OT_wind + 0.75 * P_SL
        C_chord_i = max(C_seis, C_wind, C_combo4)

        max_T = max(max_T, T_chord_i)
        max_C = max(max_C, C_chord_i)

        ft = T_chord_i / Aen if Aen > 0 else 0
        fc = C_chord_i / Ae if Ae > 0 else 0
        fc_brg = C_chord_i / Ae if Ae > 0 else 0

        ft_ratio = ft / Ft_prime if Ft_prime > 0 else 999
        fc_ratio = fc / Fc_prime if Fc_prime > 0 else 999
        brg_ratio = fc_brg / Fc_perp_prime if Fc_perp_prime > 0 else 999

        chord_checks.append(
            {
                "segment": i + 1,
                "b_ft": b,
                "T_lbs": round(T_chord_i, 1),
                "C_lbs": round(C_chord_i, 1),
                "tension": {
                    "ft_psi": round(ft, 1),
                    "Ft_prime_psi": round(Ft_prime, 1),
                    "ratio": round(ft_ratio, 4),
                    "status": "PASS" if ft_ratio <= 1.0 else "FAIL",
                },
                "compression": {
                    "fc_psi": round(fc, 1),
                    "Fc_prime_psi": round(Fc_prime, 1),
                    "ratio": round(fc_ratio, 4),
                    "status": "PASS" if fc_ratio <= 1.0 else "FAIL",
                },
                "bearing": {
                    "fc_psi": round(fc_brg, 1),
                    "Fc_perp_prime_psi": round(Fc_perp_prime, 1),
                    "ratio": round(brg_ratio, 4),
                    "status": "PASS" if brg_ratio <= 1.0 else "FAIL",
                },
            }
        )

    all_chords_ok = (
        all(
            c["tension"]["ratio"] <= 1.0
            and c["compression"]["ratio"] <= 1.0
            and c["bearing"]["ratio"] <= 1.0
            for c in chord_checks
        )
        if chord_checks
        else True
    )

    # ================================================================
    # COLLECTOR FORCE
    # ================================================================
    V_coll = max(inp.collector_seismic_factor * inp.Vs, inp.Vw)
    va_uniform = V_coll / inp.wall_length_total if inp.wall_length_total > 0 else 0

    # Peak collector force from opening spans
    opening_widths = inp.opening_widths_ft
    if opening_widths:
        Pcoll_max = max(va_uniform * w for w in opening_widths) if opening_widths else 0
    elif inp.wall_length_total > sum_bi and n_seg > 1:
        gap = inp.wall_length_total - sum_bi
        Pcoll_max = va_uniform * gap / max(n_seg - 1, 1)
    else:
        Pcoll_max = 0

    # Collector stress on a single stud (2x4 or 2x6)
    stud_size_str = inp.chord_studs.split(") ")[-1] if ")" in inp.chord_studs else "2x6"
    m_stud = re.match(r"(\d+)x(\d+)", stud_size_str)
    if m_stud:
        d_stud = int(m_stud.group(2))
        stud_area = 1.5 * {4: 3.5, 6: 5.5, 8: 7.25}.get(d_stud, 3.5)
    else:
        stud_area = 5.25
    ft_coll = Pcoll_max / stud_area if stud_area > 0 else 0
    fc_coll = Pcoll_max / stud_area if stud_area > 0 else 0
    coll_t_ratio = ft_coll / Ft_prime if Ft_prime > 0 else 0
    coll_c_ratio = fc_coll / Fc_prime if Fc_prime > 0 else 0
    collector_ok = max(coll_t_ratio, coll_c_ratio) <= 1.0

    # ================================================================
    # CLIP DESIGN
    # ================================================================
    clip_data = CLIPS.get(inp.clip_model, CLIPS["A35"])
    F_allow_clip = clip_data["allowable"]
    unit_shear_coll = shear_design / inp.Lac if inp.Lac > 0 else 0
    required_spacing_ft = F_allow_clip / unit_shear_coll if unit_shear_coll > 0 else 999
    clip_spacing_ok = inp.clip_spacing <= required_spacing_ft * 12

    # ================================================================
    # WIND DEFLECTION — 3-term with Ga, h/600 limit
    # Perforated: whole-wall using v = V/(Co*sum_bi_adj)
    # Segmented:  per-segment with stiffness distribution
    # ================================================================
    V_wind_serv = inp.f_Wserv * W_unfactored
    wind_defl_limit = h_in / 600.0
    wind_defl_details = []
    wind_defl_max = 0.0

    if is_perf:
        Co_d = inp.Co if inp.Co > 0 else 1.0
        bi_adj_in = sum_bi_adj * 12.0
        bi_raw_in = sum_bi * 12.0
        bi_eff_in = max(bi_raw_in - ep_deduct_in, bi_raw_in * 0.9)

        v_w_pli = V_wind_serv / (Co_d * bi_adj_in) if (Co_d * bi_adj_in) > 0 else 0

        trib_w = inp.stud_spacing / (2.0 * 12.0)
        P_stab_w = 0.6 * (D_head + inp.Swt_psf * h) * trib_w
        V_ovt_w = v_w_pli * h_in
        T_net = max(V_ovt_w - P_stab_w, 0)
        C_net = V_ovt_w + P_stab_w
        da_T = T_net / ka_anchor if ka_anchor > 0 else 0
        da_C = 0.04 * C_net / (Ae * max(Fc_perp_base, 1)) if Ae > 0 else 0
        b_beff = bi_raw_in / bi_eff_in if bi_eff_in > 0 else 1.0
        da_total = (da_T + da_C) * b_beff

        parts_w = _three_term_defl_parts(
            v_w_pli, h_in, E, Ae, Ga_lb, bi_adj_in, da_total
        )
        delta_w = parts_w["total"]
        wind_defl_max = delta_w

        wind_defl_details.append(
            {
                "segment": "wall",
                "b_ft": round(sum_bi_adj, 3),
                "v_plf": round(v_w_pli * 12, 2),
                "T_lbs": round(T_net, 1),
                "C_lbs": round(C_net, 1),
                "da_in": round(da_total, 4),
                "bend_in": round(parts_w["bend"], 4),
                "shear_slip_in": round(parts_w["shear_slip"], 4),
                "anchor_in": round(parts_w["anchor"], 4),
                "delta_in": round(delta_w, 4),
                "limit_in": round(wind_defl_limit, 4),
                "ratio": (
                    round(delta_w / wind_defl_limit, 4) if wind_defl_limit > 0 else 0
                ),
                "status": "PASS" if delta_w <= wind_defl_limit else "FAIL",
            }
        )
    else:
        for i, b in enumerate(segments):
            b_in = b * 12.0
            b_eff = b_effs_in[i]
            frac = seg_stiffnesses[i] / sum_k if sum_k > 0 else 1.0 / max(n_seg, 1)

            V_w_i = V_wind_serv * frac
            v_w_pli = V_w_i / b_in if b_in > 0 else 0

            trib_w = inp.stud_spacing / (2.0 * 12.0)
            P_stab_w = 0.6 * (D_head + inp.Swt_psf * h) * trib_w
            V_ovt = V_w_i * h_in / b_eff if b_eff > 0 else 0
            T_net = max(V_ovt - P_stab_w, 0)
            C_net = V_ovt + P_stab_w
            da_T = T_net / ka_anchor if ka_anchor > 0 else 0
            da_C = 0.04 * C_net / (Ae * max(Fc_perp_base, 1)) if Ae > 0 else 0
            b_beff = b_in / b_eff if b_eff > 0 else 1.0
            da_total = (da_T + da_C) * b_beff

            parts_w = _three_term_defl_parts(
                v_w_pli, h_in, E, Ae, Ga_lb, b_in, da_total
            )
            delta_w = parts_w["total"]
            wind_defl_max = max(wind_defl_max, delta_w)

            wind_defl_details.append(
                {
                    "segment": i + 1,
                    "b_ft": b,
                    "v_plf": round(V_w_i / b if b > 0 else 0, 2),
                    "T_lbs": round(T_net, 1),
                    "C_lbs": round(C_net, 1),
                    "da_in": round(da_total, 4),
                    "bend_in": round(parts_w["bend"], 4),
                    "shear_slip_in": round(parts_w["shear_slip"], 4),
                    "anchor_in": round(parts_w["anchor"], 4),
                    "delta_in": round(delta_w, 4),
                    "limit_in": round(wind_defl_limit, 4),
                    "ratio": (
                        round(delta_w / wind_defl_limit, 4)
                        if wind_defl_limit > 0
                        else 0
                    ),
                    "status": "PASS" if delta_w <= wind_defl_limit else "FAIL",
                }
            )

    wind_defl_ok = wind_defl_max <= wind_defl_limit

    # ================================================================
    # SEISMIC DEFLECTION — Cd/Ie amplified, 0.02h limit
    # Perforated: whole-wall using v = Eq/(Co*sum_bi_adj)
    # Segmented:  per-segment with stiffness distribution
    # ================================================================
    seis_defl_limit = 0.02 * h_in
    seis_defl_details = []
    seis_defl_max = 0.0
    V_seis_elastic = Eq_unfactored

    if is_perf:
        Co_d = inp.Co if inp.Co > 0 else 1.0
        bi_adj_in = sum_bi_adj * 12.0
        bi_raw_in = sum_bi * 12.0
        bi_eff_in = max(bi_raw_in - ep_deduct_in, bi_raw_in * 0.9)

        v_s_pli = V_seis_elastic / (Co_d * bi_adj_in) if (Co_d * bi_adj_in) > 0 else 0

        trib_s = inp.stud_spacing / (2.0 * 12.0)
        P_stab_s = max((0.6 - 0.2 * inp.SDS), 0) * (D_head + inp.Swt_psf * h) * trib_s
        V_ovt_s = v_s_pli * h_in
        T_net = max(V_ovt_s - P_stab_s, 0)
        C_net = V_ovt_s + P_stab_s
        da_T = T_net / ka_anchor if ka_anchor > 0 else 0
        da_C = 0.04 * C_net / (Ae * max(Fc_perp_base, 1)) if Ae > 0 else 0
        b_beff = bi_raw_in / bi_eff_in if bi_eff_in > 0 else 1.0
        da_total = (da_T + da_C) * b_beff

        parts_s = _three_term_defl_parts(
            v_s_pli, h_in, E, Ae, Ga_lb, bi_adj_in, da_total
        )
        delta_e = parts_s["total"]
        Ie_safe = inp.Ie_defl if inp.Ie_defl > 0 else 1.0
        delta_amp = delta_e * inp.Cd_defl / Ie_safe
        seis_defl_max = delta_amp

        seis_defl_details.append(
            {
                "segment": "wall",
                "b_ft": round(sum_bi_adj, 3),
                "v_plf": round(v_s_pli * 12, 2),
                "bend_in": round(parts_s["bend"], 4),
                "shear_slip_in": round(parts_s["shear_slip"], 4),
                "anchor_in": round(parts_s["anchor"], 4),
                "T_lbs": round(T_net, 1),
                "C_lbs": round(C_net, 1),
                "da_in": round(da_total, 4),
                "delta_e_in": round(delta_e, 4),
                "delta_amp_in": round(delta_amp, 4),
                "limit_in": round(seis_defl_limit, 3),
                "ratio": (
                    round(delta_amp / seis_defl_limit, 4) if seis_defl_limit > 0 else 0
                ),
                "status": "PASS" if delta_amp <= seis_defl_limit else "FAIL",
            }
        )
    else:
        for i, b in enumerate(segments):
            b_in = b * 12.0
            b_eff = b_effs_in[i]
            frac = seg_stiffnesses[i] / sum_k if sum_k > 0 else 1.0 / max(n_seg, 1)

            V_s_i = V_seis_elastic * frac
            v_s_pli = V_s_i / b_in if b_in > 0 else 0

            trib_s = inp.stud_spacing / (2.0 * 12.0)
            P_stab_s = (
                max((0.6 - 0.2 * inp.SDS), 0) * (D_head + inp.Swt_psf * h) * trib_s
            )
            V_ovt = V_s_i * h_in / b_eff if b_eff > 0 else 0
            T_net = max(V_ovt - P_stab_s, 0)
            C_net = V_ovt + P_stab_s
            da_T = T_net / ka_anchor if ka_anchor > 0 else 0
            da_C = 0.04 * C_net / (Ae * max(Fc_perp_base, 1)) if Ae > 0 else 0
            b_beff = b_in / b_eff if b_eff > 0 else 1.0
            da_total = (da_T + da_C) * b_beff

            parts_s = _three_term_defl_parts(
                v_s_pli, h_in, E, Ae, Ga_lb, b_in, da_total
            )
            delta_e = parts_s["total"]
            Ie_safe = inp.Ie_defl if inp.Ie_defl > 0 else 1.0
            delta_amp = delta_e * inp.Cd_defl / Ie_safe
            seis_defl_max = max(seis_defl_max, delta_amp)

            seis_defl_details.append(
                {
                    "segment": i + 1,
                    "b_ft": b,
                    "v_plf": round(V_s_i / b if b > 0 else 0, 2),
                    "bend_in": round(parts_s["bend"], 4),
                    "shear_slip_in": round(parts_s["shear_slip"], 4),
                    "anchor_in": round(parts_s["anchor"], 4),
                    "T_lbs": round(T_net, 1),
                    "C_lbs": round(C_net, 1),
                    "da_in": round(da_total, 4),
                    "delta_e_in": round(delta_e, 4),
                    "delta_amp_in": round(delta_amp, 4),
                    "limit_in": round(seis_defl_limit, 3),
                    "ratio": (
                        round(delta_amp / seis_defl_limit, 4)
                        if seis_defl_limit > 0
                        else 0
                    ),
                    "status": "PASS" if delta_amp <= seis_defl_limit else "FAIL",
                }
            )

    seis_defl_ok = seis_defl_max <= seis_defl_limit

    # ================================================================
    # ANCHOR BOLTS & SILL PLATE
    # ================================================================
    Z_para = _lookup_ab(AB_Z_PARA)
    Z_perp = _lookup_ab(AB_Z_PERP)
    Z_para_adj = Z_para * CD
    Z_perp_adj = Z_perp * CD

    Fp_coef = max(0.4 * inp.SDS * inp.ka * inp.Ie, 0.1)
    Vs_perp = 0.7 * inp.rho * Fp_coef * inp.WDL * h * inp.wall_length_total * 0.5
    Vw_perp = 0.6 * inp.Ww * inp.Ltrib * inp.wall_length_total
    V_perp = max(Vs_perp, Vw_perp)
    perp_governs = "Wind" if Vs_perp < Vw_perp else "Seismic"

    num_bolts_perp = V_perp / Z_perp_adj if Z_perp_adj > 0 else 0
    spacing_perp = inp.La / num_bolts_perp if num_bolts_perp > 0 else 999
    num_bolts_para = shear_design / Z_para_adj if Z_para_adj > 0 else 0
    spacing_para = inp.La / num_bolts_para if num_bolts_para > 0 else 999

    # ================================================================
    # BOTTOM PLATE NAILING
    # ================================================================
    Z_nail = 141
    Z_nail_adj = Z_nail * CD
    nail_spacing_in = 12.0 * Z_nail_adj / unit_shear_v if unit_shear_v > 0 else 999

    # ================================================================
    # HOLDOWN
    # ================================================================
    da_val = _da_lookup(inp.holdown_model, inp.chord_studs)
    holdown_error = None
    if da_val is None:
        holdown_error = (
            "Studs do not meet min. wood member thickness for selected holdown"
        )

    # A wall may have no holdown specified. Do not fabricate a product capacity
    # in that case — either no uplift exists (holdown not required) or one is
    # required and missing (a real deficiency), decided per chord below.
    _hd_model = str(inp.holdown_model or "").strip()
    has_holdown = bool(_hd_model) and _hd_model.lower() not in ("none", "n/a", "na", "-")
    T_UPLIFT_TOL = 1.0  # lbs — below this, treat as no net uplift
    hd_info = HOLDOWN_ALLOWABLE.get(inp.holdown_model) if has_holdown else None
    hd_cap_seis = hd_info[2] if hd_info else 2145
    hd_cap_wind = hd_info[3] if hd_info else 2145
    hd_capacity = hd_cap_wind if governs == "Wind" else hd_cap_seis
    if has_holdown:
        holdown_ok = max_T <= hd_capacity
    else:
        # no holdown specified — adequate only if there is no net uplift to resist
        holdown_ok = max_T <= T_UPLIFT_TOL

    # ================================================================
    # NOTES
    # ================================================================
    apa_grade = (
        "(APA Grade 24/16)" if inp.panel_thickness == "7/16" else "(APA Grade 32/16)"
    )
    both_str = " Sheath both sides of shearwall." if inp.sheathing_both_sides else ""
    thick_note = (
        (
            ' Members and blocking at adjoining panel edges shall be min. 3" nominal '
            'or double 2" nominal with staggered nailing at all panel edges.'
        )
        if v_wind_nom > 700
        else ""
    )
    sheathing_callout = (
        f'Use {inp.panel_thickness}" OSB/PLY {apa_grade} w/ {inp.fastener_type} nails @ '
        f'{inp.nail_edge_spacing}" o/c edges, 12" o/c field, blocking required.{both_str}{thick_note}'
    )
    sill_text = "plate" if inp.sill_plate_type in ("(1)-2x", "(1)-3x") else "plates"
    sill_callout = f"Use {inp.sill_plate_type} HF No. 2 pressure treated {sill_text} at foundation."

    # ================================================================
    # RESULTS — backward-compatible format + new detailed outputs
    # ================================================================
    def _util(actual, allowed):
        return round(actual / allowed * 100, 1) if allowed else None

    # Backward-compat v_allow values. The reduction must respect the same
    # h/bs > 3.5 disqualification as the segment AR factor — otherwise the
    # summary shows a nonzero reduction (e.g. 0.703) while the segment table
    # shows AR factor 0 for the same wall.
    bs_min = min(segments) if segments else 1
    _ar = h / bs_min
    if _ar > 3.5:
        AR_reduction = 0.0
    elif _ar > 2:
        AR_reduction = 1.25 - 0.125 * _ar
    else:
        AR_reduction = None
    v_allow_seis_compat = v_asd_seis * (AR_reduction if AR_reduction else 1.0)
    v_allow_wind_compat = v_asd_wind * (AR_reduction if AR_reduction else 1.0)

    # Governing chord for backward-compat stress reporting
    worst_chord = (
        max(
            chord_checks,
            key=lambda c: max(
                c["tension"]["ratio"], c["compression"]["ratio"], c["bearing"]["ratio"]
            ),
        )
        if chord_checks
        else None
    )

    T_chord = max_T
    C_chord = max_C
    ft_actual = worst_chord["tension"]["ft_psi"] if worst_chord else 0
    fc_actual = worst_chord["compression"]["fc_psi"] if worst_chord else 0
    fc_perp_actual = worst_chord["bearing"]["fc_psi"] if worst_chord else 0
    CSI_tension = worst_chord["tension"]["ratio"] if worst_chord else 0
    CSI_compression = worst_chord["compression"]["ratio"] if worst_chord else 0
    CSI_bearing = worst_chord["bearing"]["ratio"] if worst_chord else 0
    tension_ok = CSI_tension <= 1.0
    compression_ok = CSI_compression <= 1.0
    bearing_ok = CSI_bearing <= 1.0

    results_block = {
        "Unit Shear Seismic (plf)": {
            "Actual": round(vs_unit, 1),
            "Allowed": round(v_allow_seis_compat, 1),
            "Result": _util(vs_unit, v_allow_seis_compat),
            "LDF": "1.6",
            "Combo": "0.7E",
        },
        "Unit Shear Wind (plf)": {
            "Actual": round(vw_unit, 1),
            "Allowed": round(v_allow_wind_compat, 1),
            "Result": _util(vw_unit, v_allow_wind_compat),
            "LDF": "1.6",
            "Combo": "0.6W",
        },
        "Aspect Ratio (h/bs)": {
            "Actual": round(AR_max, 2),
            "Allowed": 3.5,
            "Result": _util(AR_max, 3.5),
            "LDF": "-",
            "Combo": "-",
        },
        "Wind Deflection (in)": {
            "Actual": round(wind_defl_max, 4),
            "Allowed": round(wind_defl_limit, 4),
            "Result": _util(wind_defl_max, wind_defl_limit),
            "LDF": "-",
            "Combo": "0.6W serv",
        },
        "Seismic Drift (in)": {
            "Actual": round(seis_defl_max, 3),
            "Allowed": round(seis_defl_limit, 3),
            "Result": _util(seis_defl_max, seis_defl_limit),
            "LDF": "-",
            "Combo": "Cd·δe/Ie",
        },
        "Chord Tension (psi)": {
            "Actual": round(ft_actual, 1),
            "Allowed": round(Ft_prime, 0),
            "Result": round(CSI_tension * 100, 1),
            "LDF": "1.6",
            "Combo": governs,
        },
        "Chord Compression (psi)": {
            "Actual": round(fc_actual, 1),
            "Allowed": round(Fc_prime, 1),
            "Result": round(min(CSI_compression, 9.99) * 100, 1),
            "LDF": "1.6",
            "Combo": governs,
        },
        "Sill Bearing (psi)": {
            "Actual": round(fc_perp_actual, 1),
            "Allowed": round(Fc_perp_prime, 1),
            "Result": round(CSI_bearing * 100, 1),
            "LDF": "-",
            "Combo": governs,
        },
    }

    return {
        "governs": governs,
        "results": results_block,
        "method": "perforated" if is_perf else "segmented",
        # Wall geometry
        "wall": {
            "height_ft": h,
            "length_ft": inp.wall_length_total,
            "n_segments": n_seg,
            "sum_bi_ft": round(sum_bi, 3),
            "segments": seg_details,
        },
        "shear_design_lbs": round(shear_design, 1),
        "unit_shear_v_plf": round(unit_shear_v, 4),
        "L_eff_ft": round(inp.Co * sum_bi if is_perf else sum_bi, 3),
        "Co": round(inp.Co, 4) if is_perf else None,
        # Shear capacity (new detailed)
        "shear_capacity": {
            "wind": {
                "Vw_demand_lbs": round(inp.Vw, 1),
                "Vw_capacity_lbs": round(wall_Vw_cap, 1),
                "ratio": round(Vw_ratio, 4) if Vw_ratio is not None else None,
                "status": _shear_status(wind_shear_ok, "PASS", "FAIL"),
            },
            "seismic": {
                "Vs_demand_lbs": round(inp.Vs, 1),
                "Vs_capacity_lbs": round(wall_Vs_cap, 1),
                "ratio": round(Vs_ratio, 4) if Vs_ratio is not None else None,
                "status": _shear_status(seismic_shear_ok, "PASS", "FAIL"),
            },
        },
        # Backward-compat blocks
        "seismic_shear": {
            "vs_plf": round(vs_unit, 4),
            "v_allow_seismic_plf": round(v_allow_seis_compat, 1),
            "v_nominal_plf": v_seis_nom,
            "aspect_ratio": round(AR_max, 4),
            "ar_reduction": round(AR_reduction, 4) if AR_reduction else None,
            "ar_check": "OK" if AR_check_ok else "NG",
            "shear_check": _shear_status(seismic_shear_ok, "OK", "NG"),
        },
        "wind_shear": {
            "vw_plf": round(vw_unit, 4),
            "v_allow_wind_plf": round(v_allow_wind_compat, 1),
            "v_nominal_plf": v_wind_nom,
            "shear_check": _shear_status(wind_shear_ok, "OK", "NG"),
        },
        # Material
        "material": {
            "chord_studs": inp.chord_studs,
            "chord_species": inp.chord_species,
            "Ae_in2": Ae,
            "Aen_in2": round(Aen, 2),
            "Ft_prime_psi": round(Ft_prime, 1),
            "Fc_prime_psi": round(Fc_prime, 1),
            "Fc_perp_prime_psi": round(Fc_perp_prime, 1),
            "FcE_psi": round(FcE, 1),
            "Fc_star_psi": round(Fc_star, 1),
            "Cp": round(Cp, 4),
            "le_d": round(le_d, 2),
            "Ga_kips_per_in": round(Ga_lb / 1000.0, 1),
        },
        # Per-chord checks (new)
        "chord_checks": chord_checks,
        # Backward-compat chord summary
        "chord": {
            "T_chord_lbs": round(T_chord, 1),
            "C_chord_lbs": round(C_chord, 1),
            "Ab_in2": Ae,
            "tension": {
                "ft_actual_psi": round(ft_actual, 1),
                "Ft_adj_psi": round(Ft_prime, 0),
                "CSI": round(CSI_tension, 4),
                "check": "OK" if tension_ok else "NG",
            },
            "bearing": {
                "fc_perp_psi": round(fc_perp_actual, 2),
                "Fc_perp_adj_psi": round(Fc_perp_prime, 1),
                "CSI": round(CSI_bearing, 4),
                "check": "OK" if bearing_ok else "NG",
            },
            "compression": {
                "fc_actual_psi": round(fc_actual, 2),
                "Fc_adj_psi": round(Fc_prime, 2),
                "le_over_d": round(le_d, 2),
                "Cp": round(Cp, 4),
                "CSI": round(CSI_compression, 4),
                "check": "OK" if compression_ok else "NG",
            },
        },
        # Collector (new + backward-compat)
        "collector": {
            "V_demand_lbs": round(V_coll, 1),
            "Pcoll_max_lbs": round(Pcoll_max, 1),
            "tension_ratio": round(coll_t_ratio, 4),
            "compression_ratio": round(coll_c_ratio, 4),
            "collector_ok": collector_ok,
            "clip_model": inp.clip_model,
            "F_allow_lbs": F_allow_clip,
            "unit_shear_plf": round(unit_shear_coll, 2),
            "req_spacing_ft": round(required_spacing_ft, 3),
            "provided_spacing_in": inp.clip_spacing,
            "check": (
                "OK" if clip_spacing_ok else "Spacing of ties or angles is inadequate."
            ),
        },
        # Deflection (new detailed + backward-compat)
        "deflection": {
            "wind": {
                "V_serv_lbs": round(V_wind_serv, 1),
                "limit_in": round(wind_defl_limit, 4),
                "max_in": round(wind_defl_max, 4),
                "segments": wind_defl_details,
                "status": "PASS" if wind_defl_ok else "FAIL",
            },
            "seismic": {
                "V_lbs": round(V_seis_elastic, 1),
                "Cd": inp.Cd_defl,
                "Ie": inp.Ie_defl,
                "limit_in": round(seis_defl_limit, 3),
                "max_in": round(seis_defl_max, 4),
                "segments": seis_defl_details,
                "status": "PASS" if seis_defl_ok else "FAIL",
            },
            "delta_max_in": round(seis_defl_max, 4),
            "delta_amplified_in": round(seis_defl_max, 4),
            "delta_limit_in": round(seis_defl_limit, 3),
            "drift_check": "OK" if seis_defl_ok else "NG",
            "da_in": da_val if da_val else "NS",
            "holdown_error": holdown_error,
        },
        # Per-chord holdowns (new)
        "holdowns": [
            {
                "chord": i + 1,
                "T_lbs": round(c["T_lbs"], 1),
                "model": inp.holdown_model if has_holdown else None,
                "capacity_lbs": hd_capacity if has_holdown else None,
                "ratio": (
                    round(c["T_lbs"] / hd_capacity, 4)
                    if (has_holdown and hd_capacity > 0) else None
                ),
                "status": (
                    ("PASS" if c["T_lbs"] <= hd_capacity else "FAIL")
                    if has_holdown
                    else ("N/A" if c["T_lbs"] <= T_UPLIFT_TOL else "REQUIRED")
                ),
            }
            for i, c in enumerate(chord_checks)
        ],
        "anchor_bolts": {
            "Z_para_lbs": round(Z_para, 1),
            "Z_perp_lbs": round(Z_perp, 1),
            "Z_para_adj_lbs": round(Z_para_adj, 1),
            "Z_perp_adj_lbs": round(Z_perp_adj, 1),
            "V_perp_lbs": round(V_perp, 1),
            "perp_governs": perp_governs,
            "num_para": round(num_bolts_para, 2),
            "spacing_para_ft": round(spacing_para, 3),
            "num_perp": round(num_bolts_perp, 2),
            "spacing_perp_ft": round(spacing_perp, 3),
        },
        "bottom_plate_nailing": {
            "Z_per_nail_lbs": round(Z_nail_adj, 1),
            "spacing_calc_in": round(nail_spacing_in, 3),
        },
        "notes": {
            "sheathing_callout": sheathing_callout,
            "sill_callout": sill_callout,
            "governs": governs,
            "table_note": table_note,
        },
        "summary": {
            "overall": (
                "PASS"
                if all(
                    [
                        # None = N/A (governed by aspect ratio, which is checked
                        # separately below) — only an explicit False is a failure.
                        seismic_shear_ok is not False,
                        wind_shear_ok is not False,
                        AR_check_ok,
                        seis_defl_ok,
                        wind_defl_ok,
                        all_chords_ok,
                        collector_ok,
                        clip_spacing_ok,
                        holdown_ok,
                    ]
                )
                else "FAIL"
            ),
        },
    }


# ============================================================
# Legacy convenience wrapper (backward-compatible with old API)
# ============================================================
def check_shear_wall_legacy(
    name, method, wall_height_ft, wall_length_ft, openings, v_demand_lbs
):
    """Legacy wrapper for backward compatibility with old test code."""
    panels = [wall_length_ft]
    if openings:
        panels = _segments_from_openings(wall_length_ft, openings)

    is_perf = method.lower() == "perforated"
    l_sum_segments = sum(panels)
    bs_min_val = min(panels) if panels else wall_length_ft

    co_val = 1.0
    if is_perf:
        Ao = sum(o.get("w", 0) * o.get("h", 0) for o in openings)
        co_val = compute_perforated_co(
            wall_length_ft, wall_height_ft, l_sum_segments, Ao
        )

    inp = ShearWallInputs(
        Vs=v_demand_lbs,
        Vw=v_demand_lbs,
        wall_length_total=wall_length_ft,
        wall_height=wall_height_ft,
        wall_type="PERF" if is_perf else "SEGMENT",
        Co=co_val,
        sum_Li=l_sum_segments,
        bs_perf=bs_min_val,
        panels=panels,
        panel_thickness="7/16",
        nail_edge_spacing=6,
        sheathing_both_sides=False,
        holdown_model="HDU2",
        chord_studs="(2) 2x6",
        sill_plate_type="(1)-2x",
        ab_diameter=0.625,
        Ww=30,
        Ltrib=5,
        La=wall_length_ft,
        Lac=wall_length_ft,
        top_sill_species="DF",
        WDL_plf=100,
        WLL_plf=100,
    )
    result = check_shear_wall(inp)
    return {
        "name": name,
        "method": method,
        "results": {
            "Unit Shear (plf)": {
                "Actual": round(result["unit_shear_v_plf"], 1),
                "Allowed": round(result["seismic_shear"]["v_allow_seismic_plf"], 1),
                "Result": (
                    round(
                        result["unit_shear_v_plf"]
                        / result["seismic_shear"]["v_allow_seismic_plf"]
                        * 100,
                        1,
                    )
                    if result["seismic_shear"]["v_allow_seismic_plf"] > 0
                    else 0
                ),
                "LDF": "-",
                "Combo": result["governs"],
            },
            "Hold-down Tension (lbs)": {
                "Actual": round(result["chord"]["T_chord_lbs"], 1),
                "Allowed": 5000,
                "Result": round(result["chord"]["T_chord_lbs"] / 5000 * 100, 1),
                "LDF": "-",
                "Combo": result["governs"],
            },
            "Lateral Defl. (in)": {
                "Actual": round(result["deflection"]["delta_amplified_in"], 3),
                "Allowed": round(result["deflection"]["delta_limit_in"], 3),
                "Result": (
                    round(
                        result["deflection"]["delta_amplified_in"]
                        / result["deflection"]["delta_limit_in"]
                        * 100,
                        1,
                    )
                    if result["deflection"]["delta_limit_in"] > 0
                    else 0
                ),
                "LDF": "-",
                "Combo": "Seismic",
            },
        },
        "result": result,
    }


def _segments_from_openings(total_len, openings):
    """Compute segment lengths from opening positions (ordered x, w, h)."""
    sorted_ops = sorted(openings, key=lambda o: o.get("x", 0))
    segments = []
    cursor = 0
    for op in sorted_ops:
        x = op.get("x", 0)
        w = op.get("w", 0)
        if x > cursor:
            segments.append(x - cursor)
        cursor = x + w
    if cursor < total_len:
        segments.append(total_len - cursor)
    return [s for s in segments if s > 0]


# ============================================================
# Test
# ============================================================
if __name__ == "__main__":
    # Segmented multi-pier test (like Tedds Example 2)
    inp = ShearWallInputs(
        Vs=3920,
        Vw=3720,
        wall_length_total=30,
        wall_height=10,
        wall_type="SEGMENT",
        panels=[7.0, 7.0, 8.5],
        panel_thickness="3/8",
        fastener_type="8d",
        nail_edge_spacing=4,
        sheathing_both_sides=True,
        chord_studs="(2) 2x4",
        holdown_model="HDU4",
        sill_plate_type="(1)-2x",
        ab_diameter=0.625,
        top_sill_species="DF",
        WDL_plf=35,
        WLL_plf=150,
        WSL_plf=20,
        D_head_plf=35,
        Swt_psf=12,
        SDS=1.0,
        Ie=1.0,
        anchor_stiffness=25000,
        Cd_defl=4.0,
        Ie_defl=1.25,
        La=30,
        Lac=30,
        Ww=20,
        Ltrib=5,
        WDL=12,
        opening_widths_ft=[4.0, 3.5],
    )
    result = check_shear_wall(inp)

    print("=" * 70)
    print("SHEAR WALL CALCULATOR — Whole-Wall Analysis")
    print("=" * 70)

    g = result["governs"]
    sc = result["shear_capacity"]
    w = result["wall"]

    print(f"\n  Method: {result['method']} | {g} governs")
    print(
        f"  Wall: {w['height_ft']}ft × {w['length_ft']}ft, {w['n_segments']} segments"
    )
    print(f"  Ga: {result['material']['Ga_kips_per_in']} kips/in")

    print(f"\n  SHEAR CAPACITY:")
    print(
        f"    Wind:    {sc['wind']['Vw_demand_lbs']}lbs / {sc['wind']['Vw_capacity_lbs']}lbs = {sc['wind']['ratio']:.3f} [{sc['wind']['status']}]"
    )
    print(
        f"    Seismic: {sc['seismic']['Vs_demand_lbs']}lbs / {sc['seismic']['Vs_capacity_lbs']}lbs = {sc['seismic']['ratio']:.3f} [{sc['seismic']['status']}]"
    )

    if "k_kips_per_in" in (w["segments"][0] if w["segments"] else {}):
        print(f"\n  STIFFNESS DISTRIBUTION:")
        for s in w["segments"]:
            print(
                f"    Seg {s['id']}: b={s['b_ft']}ft, k={s['k_kips_per_in']}k/in, "
                f"frac={s['frac']:.3f}, vs={s['vs_plf']:.1f}/{s['vs_cap_plf']:.1f}plf"
            )

    print(f"\n  CHORD CHECKS:")
    for c in result["chord_checks"]:
        t = c["tension"]
        comp = c["compression"]
        brg = c["bearing"]
        print(
            f"    Seg {c['segment']}: T={c['T_lbs']}lbs C={c['C_lbs']}lbs | "
            f"ft/Ft'={t['ratio']:.3f}[{t['status']}] fc/Fc'={comp['ratio']:.3f}[{comp['status']}] "
            f"brg={brg['ratio']:.3f}[{brg['status']}]"
        )

    print(f"\n  HOLDOWNS:")
    for hd in result["holdowns"]:
        print(
            f"    Chord {hd['chord']}: T={hd['T_lbs']}lbs / {hd['capacity_lbs']}lbs [{hd['status']}]"
        )

    coll = result["collector"]
    print(
        f"\n  COLLECTOR: Pcoll={coll['Pcoll_max_lbs']}lbs, "
        f"T ratio={coll['tension_ratio']:.3f}, C ratio={coll['compression_ratio']:.3f}"
    )

    wd = result["deflection"]["wind"]
    sd = result["deflection"]["seismic"]
    print(
        f"\n  WIND DEFLECTION: max={wd['max_in']}in / {wd['limit_in']}in [{wd['status']}]"
    )
    for s in wd["segments"]:
        print(
            f"    Seg {s['segment']}: d={s['delta_in']}in, ratio={s['ratio']:.3f} [{s['status']}]"
        )

    print(
        f"\n  SEISMIC DEFLECTION: max={sd['max_in']}in / {sd['limit_in']}in [{sd['status']}]"
    )
    for s in sd["segments"]:
        print(
            f"    Seg {s['segment']}: de={s['delta_e_in']}in, damp={s['delta_amp_in']}in, ratio={s['ratio']:.3f} [{s['status']}]"
        )

    print(f"\n  OVERALL: {result['summary']['overall']}")
