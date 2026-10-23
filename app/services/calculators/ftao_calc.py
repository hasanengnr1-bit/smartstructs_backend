"""
ftao_calc.py
Force Transfer Around Openings (FTAO) shear wall analysis.

Implements the rational analysis method published in APA Technical Note
T555 "Design for Force Transfer Around Openings" (Diekmann technique),
as implemented in the APA FTAO Calculator v2.5.2025, generalized to any
number of openings (the APA tool covers 1-3).

Code basis: SDPWS 2021 (ASCE 7-22 / 2024 IBC companion):
  - Aspect ratio limits per SDPWS 4.3.5 / Table 4.3.3 (max 3.5:1)
  - Aspect ratio factor per SDPWS 4.3.4.2: 1.25 - 0.125(h/bs),
    applicable to BOTH wind and seismic (changed from 2bs/h seismic-only
    in earlier editions). The legacy "2bs/h" method is retained as an
    option for comparison with older calcs.
  - Deflection per SDPWS C4.3.2 (4-term) and SDPWS 4.3.2 (3-term),
    evaluated at each pier edge and averaged across the wall, per the
    APA tool.

Walls and piers:
  n openings => n+1 full-height piers.  Pier i (0-based) sits left of
  opening i; opening j is bounded by piers j and j+1.

Verified against the worked example in the APA calculator
("Design Example (APA T555)" sheet): V=3750 lbs, piers 4/4/3.5 ft,
openings 6x2.67 ft and 2x2.67 ft, ha=1.33 ft, hb=4 ft.
"""

import math
from dataclasses import dataclass, field
from typing import List, Optional


# ============================================================
# Lookup tables — APA FTAO calculator "LookUp" sheet
# (values originate from SDPWS Tables 4.3A, C4.2.2A-D)
# ============================================================

# Panel rigidity through-the-thickness Gvtv (lbf/in of panel width)
# key: (thickness, material) -> {grade: Gt}
GT_TABLE = {
    ("3/8", "OSB"):      {"APA Rated Sheathing": 77500, "APA Structural 1": 77500},
    ("7/16", "OSB"):     {"APA Rated Sheathing": 83500, "APA Structural 1": 83500},
    ("15/32", "OSB"):    {"APA Rated Sheathing": 83500, "APA Structural 1": 83500},
    ("19/32", "OSB"):    {"APA Rated Sheathing": 83500, "APA Structural 1": 83500},
    ("3/8", "Plywood"):  {"APA Rated Sheathing": 25000, "APA Structural 1": 32500},
    ("15/32", "Plywood"): {"APA Rated Sheathing": 27000, "APA Structural 1": 44500},
    ("19/32", "Plywood"): {"APA Rated Sheathing": 28500, "APA Structural 1": 47500},
}

# Fastener slip en = factor * (Vn / base) ^ exp   (SDPWS Table C4.2.2D,
# 1.2 factor for panels other than Structural I per table footnote)
# key: (nail, grade) -> (base, exp, factor)
FASTENER_SLIP = {
    ("8d", "APA Structural 1"):       (616, 3.018, 1.0),
    ("10d", "APA Structural 1"):      (769, 3.276, 1.0),
    ("8d", "APA Rated Sheathing"):    (616, 3.018, 1.2),
    ("10d", "APA Rated Sheathing"):   (769, 3.276, 1.2),
}

# Apparent shear stiffness Ga (kips/in) — SDPWS Table 4.3A
# key: (thickness, material, grade, nail) -> {edge spacing in: Ga}
GA_TABLE: dict[tuple[str, str, str, str], dict[int, float]] = {
    ("3/8", "OSB", "APA Rated Sheathing", "8d"):    {2: 45, 3: 31, 4: 25, 6: 17},
    ("7/16", "OSB", "APA Rated Sheathing", "8d"):   {2: 42, 3: 28, 4: 22, 6: 15},
    ("15/32", "OSB", "APA Rated Sheathing", "8d"):  {2: 39, 3: 25, 4: 19, 6: 13},
    ("15/32", "OSB", "APA Rated Sheathing", "10d"): {2: 52, 3: 37, 4: 30, 6: 22},
    ("19/32", "OSB", "APA Rated Sheathing", "10d"): {2: 48, 3: 33, 4: 26, 6: 19},
    ("3/8", "OSB", "APA Structural 1", "8d"):       {2: 43, 3: 30, 4: 24, 6: 19},
    ("7/16", "OSB", "APA Structural 1", "8d"):      {2: 40, 3: 27, 4: 21, 6: 16},
    ("15/32", "OSB", "APA Structural 1", "8d"):     {2: 37, 3: 24, 4: 18, 6: 14},
    ("15/32", "OSB", "APA Structural 1", "10d"):    {2: 51, 3: 36, 4: 29, 6: 22},
    ("3/8", "Plywood", "APA Rated Sheathing", "8d"):    {2: 20, 3: 17, 4: 15, 6: 12},
    ("3/8", "Plywood", "APA Structural 1", "8d"):       {2: 24, 3: 20, 4: 17, 6: 14},
    ("15/32", "Plywood", "APA Rated Sheathing", "8d"):  {2: 20, 3: 15, 4: 13, 6: 10},
    ("15/32", "Plywood", "APA Rated Sheathing", "10d"): {2: 23, 3: 19, 4: 17, 6: 14},
    ("15/32", "Plywood", "APA Structural 1", "8d"):     {2: 27.6, 3: 20.4, 4: 16.8, 6: 13.2},
    ("15/32", "Plywood", "APA Structural 1", "10d"):    {2: 33.6, 3: 26.4, 4: 24, 6: 19.2},
    ("19/32", "Plywood", "APA Rated Sheathing", "10d"): {2: 22, 3: 18, 4: 16, 6: 13},
}

# Single-stud cross-section areas (in^2)
STUD_AREA = {
    "2x3": 3.75, "2x4": 5.25, "3x4": 8.75, "2x5": 6.75,
    "2x6": 8.25, "3x6": 13.75, "2x8": 10.875,
}

MAX_ASPECT_RATIO = 3.5


# ============================================================
# Inputs
# ============================================================
@dataclass
class FTAOOpening:
    width_ft: float          # Lo
    ho_ft: float             # opening height
    ha_ft: float             # sheathed depth above opening
    hb_ft: float             # sheathed depth below opening (sill)


@dataclass
class FTAOInputs:
    V_lbs: float                          # ASD in-plane shear at top of wall
    pier_lengths_ft: List[float]          # n+1 full-height pier lengths, left to right
    openings: List[FTAOOpening]           # n openings, left to right
    load_type: str = "seismic"            # "seismic" or "wind" (for Vunf default)

    # SDPWS 2021 4.3.4.2 default; "2bs/h" retained for legacy comparison
    adj_factor_method: str = "1.25-0.125h/bs"

    # Sheathing (capacity check + deflection stiffness)
    panel_material: str = "OSB"           # "OSB" / "Plywood"
    panel_grade: str = "APA Rated Sheathing"
    panel_thickness: str = "15/32"        # "3/8" / "7/16" / "15/32" / "19/32"
    nail_size: str = "8d"                 # "8d" / "10d" common
    nail_spacing_in: int = 4              # panel edge nailing
    sheathing_both_sides: bool = False

    # Deflection inputs
    V_unfactored_lbs: Optional[float] = None  # strength-level; default V/0.7 (seismic) or V (wind)
    E_psi: float = 1_600_000
    chord_qty: int = 2
    chord_stud_size: str = "2x6"
    A_override_in2: Optional[float] = None
    Gt_override: Optional[float] = None
    Ga_override: Optional[float] = None
    hd_capacity_lbs: float = 2145
    hd_deflection_in: float = 0.128
    Cd: float = 4.0
    drift_limit_ratio: float = 0.020      # ASCE 7-22 Table 12.12-1


# ============================================================
# Engine
# ============================================================
def check_ftao(inp: FTAOInputs) -> dict:
    n = len(inp.openings)
    L = list(inp.pier_lengths_ft)
    if n < 1:
        raise ValueError("FTAO requires at least one opening; use SEGMENT for solid walls.")
    if len(L) != n + 1:
        raise ValueError(f"FTAO needs {n + 1} pier lengths for {n} openings, got {len(L)}.")

    warnings: List[str] = []

    # ---- Geometry ----
    hwall = inp.openings[0].ha_ft + inp.openings[0].ho_ft + inp.openings[0].hb_ft
    for j, op in enumerate(inp.openings[1:], start=2):
        if abs((op.ha_ft + op.ho_ft + op.hb_ft) - hwall) > 0.01:
            warnings.append(f"Opening {j}: ha+ho+hb does not match wall height of opening 1.")
    Lo = [op.width_ft for op in inp.openings]
    Lwall = sum(L) + sum(Lo)

    for i, b in enumerate(L, start=1):
        if 0 < b < 1.5:
            warnings.append(f"Pier {i} length {b} ft is less than 1.50 ft — revise (APA limit).")
    for j, op in enumerate(inp.openings, start=1):
        if (op.ha_ft and op.ha_ft < 1.0) or (op.hb_ft and op.hb_ft < 1.0):
            warnings.append(f"Opening {j}: sheathing above/below is less than 1.0 ft — "
                            "FTAO strap forces become unreliable (APA note).")
    for j, op in enumerate(inp.openings, start=1):
        ratios = [op.width_ft / d for d in (op.ha_ft, op.hb_ft) if d > 0]
        if any(r > 6.5 for r in ratios):
            warnings.append(f"Opening {j}: width-to-depth ratio of sheathing above/below "
                            "exceeds 6.5 — FTAO method may not apply (APA note).")
            break

    V = inp.V_lbs

    # ---- 1. Hold-down force: H = V*hwall/Lwall ----
    H = V * hwall / Lwall if Lwall > 0 else 0.0

    # ---- 2. Unit shear above + below each opening: va = vb = H/(ha+hb) ----
    va = [H / (op.ha_ft + op.hb_ft) if (op.ha_ft + op.hb_ft) > 0 else 0.0
          for op in inp.openings]

    # ---- 3. Boundary force above + below each opening: O = va*Lo ----
    O = [va[j] * Lo[j] for j in range(n)]

    # ---- 4. Corner forces (strap forces), split to adjacent piers ----
    #      F_left[j] -> pier j, F_right[j] -> pier j+1
    F_left, F_right = [], []
    for j in range(n):
        Lsum = L[j] + L[j + 1]
        F_left.append(O[j] * L[j] / Lsum if Lsum > 0 else 0.0)
        F_right.append(O[j] * L[j + 1] / Lsum if Lsum > 0 else 0.0)

    # ---- 5. Tributary lengths of openings ----
    T_left, T_right = [], []
    for j in range(n):
        Lsum = L[j] + L[j + 1]
        T_left.append(L[j] * Lo[j] / Lsum if Lsum > 0 else 0.0)
        T_right.append(L[j + 1] * Lo[j] / Lsum if Lsum > 0 else 0.0)

    def _trib(i: int) -> float:
        t = 0.0
        if i > 0:
            t += T_right[i - 1]
        if i < n:
            t += T_left[i]
        return t

    # ---- 6. Unit shear beside openings (pier shears) ----
    v_pier = [(V / Lwall) * (L[i] + _trib(i)) / L[i] if L[i] > 0 else 0.0
              for i in range(n + 1)]

    # ---- 7./8./9. Corner zone shears ----
    R = [v_pier[i] * L[i] for i in range(n + 1)]
    F_into = [(F_right[i - 1] if i > 0 else 0.0) + (F_left[i] if i < n else 0.0)
              for i in range(n + 1)]
    vc = [(R[i] - F_into[i]) / L[i] if L[i] > 0 else 0.0 for i in range(n + 1)]

    # Statics closure: sum(v_i * L_i) must equal V
    statics_ok = abs(sum(R) - V) < max(1.0, 0.001 * abs(V))

    # ---- Aspect ratios & adjustment factors (SDPWS 2021 4.3.4) ----
    # Pier height for AR taken as the tallest adjacent opening (clear height).
    pier_AR: list[float] = []
    pier_adj: list[float | None] = []
    ar_ok = True
    for i in range(n + 1):
        ho_adj = []
        if i > 0:
            ho_adj.append(inp.openings[i - 1].ho_ft)
        if i < n:
            ho_adj.append(inp.openings[i].ho_ft)
        h_pier = max(ho_adj) if ho_adj else hwall
        AR = h_pier / L[i] if L[i] > 0 else float("inf")
        pier_AR.append(AR)
        if AR > MAX_ASPECT_RATIO + 0.004:
            ar_ok = False
            pier_adj.append(None)
        elif AR <= 2.0 or inp.adj_factor_method in ("None", "", None):
            pier_adj.append(None)  # no adjustment
        elif inp.adj_factor_method == "2bs/h":
            pier_adj.append((2.0 * L[i]) / h_pier)
        else:  # SDPWS 2021 4.3.4.2
            pier_adj.append(1.25 - 0.125 * AR)

    # ---- Design summary ----
    req_terms = []
    for i in range(n + 1):
        adj = pier_adj[i]
        req_terms.append(v_pier[i] / adj if adj else v_pier[i])
        req_terms.append(abs(vc[i]))
    req_terms.extend(va)
    req_sheathing_capacity = max(req_terms)

    req_strap_force = max(F_left + F_right)
    req_hd_force = H
    anchorage_unit_shear = V / Lwall if Lwall > 0 else 0.0

    # ---- Sheathing capacity check (SDPWS Table 4.3A, ASD) ----
    from services.calculators.shear_wall_calc import lookup_unit_shear
    v_nom_seismic, v_nom_wind, table_note = lookup_unit_shear(
        inp.panel_thickness, inp.nail_size, inp.nail_spacing_in)
    sides = 2.0 if inp.sheathing_both_sides else 1.0
    v_nom = v_nom_wind if inp.load_type == "wind" else v_nom_seismic
    v_allow_asd = sides * v_nom / 2.0
    sheathing_ok = v_allow_asd >= req_sheathing_capacity if v_nom else None
    if table_note:
        warnings.append(table_note)

    # ---- Deflection (per pier edge, averaged — APA tool method) ----
    Vunf = inp.V_unfactored_lbs
    if Vunf is None:
        Vunf = V / 0.7 if inp.load_type == "seismic" else V
    v_unf = [(Vunf / Lwall) * (L[i] + _trib(i)) / L[i] if L[i] > 0 else 0.0
             for i in range(n + 1)]

    A = inp.A_override_in2 or STUD_AREA.get(inp.chord_stud_size, 8.25) * inp.chord_qty
    Gt = inp.Gt_override or GT_TABLE.get(
        (inp.panel_thickness, inp.panel_material), {}).get(inp.panel_grade)
    Ga = inp.Ga_override or GA_TABLE.get(
        (inp.panel_thickness, inp.panel_material, inp.panel_grade, inp.nail_size),
        {}).get(inp.nail_spacing_in)
    slip = FASTENER_SLIP.get((inp.nail_size, inp.panel_grade))

    if Gt is None:
        warnings.append("No Gt for this sheathing — 4-term shear deformation skipped.")
    if Ga is None:
        warnings.append("SDPWS provides no Ga for this sheathing/nail combination — "
                        "3-term deflection skipped.")
    if slip is None:
        warnings.append("No fastener slip constants for this nail/grade — en skipped.")

    def _edge_heights(i: int):
        h_left = hwall if i == 0 else inp.openings[i - 1].ha_ft + inp.openings[i - 1].ho_ft
        h_right = hwall if i == n else inp.openings[i].ha_ft + inp.openings[i].ho_ft
        return h_left, h_right

    four_term, three_term = [], []
    for i in range(n + 1):
        b = L[i]
        if b <= 0:
            continue
        for h_e in _edge_heights(i):
            v = v_unf[i]
            t1 = 8.0 * v * h_e ** 3 / (inp.E_psi * A * b)
            # 4-term
            if Gt:
                t2_4 = v * h_e / Gt
                Vn = v / (12.0 / inp.nail_spacing_in)
                t3_4 = 0.75 * h_e * (slip[2] * (Vn / slip[0]) ** slip[1]) if slip else 0.0
                t4 = (inp.hd_deflection_in * v * h_e / inp.hd_capacity_lbs) * (h_e / b) \
                    if inp.hd_capacity_lbs > 0 else 0.0
                four_term.append(t1 + t2_4 + t3_4 + t4)
            # 3-term
            if Ga:
                t2_3 = v * h_e / (Ga * 1000.0)
                t3_3 = (inp.hd_deflection_in * v * h_e / inp.hd_capacity_lbs) * (h_e / b) \
                    if inp.hd_capacity_lbs > 0 else 0.0
                three_term.append(t1 + t2_3 + t3_3)

    defl_4term = sum(four_term) / len(four_term) if four_term else None
    defl_3term = sum(three_term) / len(three_term) if three_term else None
    drift_4term = inp.Cd * defl_4term / (hwall * 12.0) if defl_4term is not None else None
    drift_3term = inp.Cd * defl_3term / (hwall * 12.0) if defl_3term is not None else None
    drift_limit = inp.drift_limit_ratio
    drift_ok = (drift_4term <= drift_limit) if drift_4term is not None else None

    overall = all([
        statics_ok, ar_ok,
        sheathing_ok is not False,
        drift_ok is not False,
    ])

    max_AR = max(pier_AR)
    results_block = {
        "Sheathing Capacity (plf)": {
            "Actual": round(req_sheathing_capacity, 1),
            "Allowed": round(v_allow_asd, 1) if v_nom else None,
            "Result": round(req_sheathing_capacity / v_allow_asd * 100, 1) if v_nom else None,
            "LDF": "-", "Combo": inp.load_type,
        },
        "Pier Aspect Ratio (ho/L)": {
            "Actual": round(max_AR, 2), "Allowed": MAX_ASPECT_RATIO,
            "Result": round(max_AR / MAX_ASPECT_RATIO * 100, 1), "LDF": "-", "Combo": "-",
        },
        "Story Drift (Cd*d/h)": {
            "Actual": round(drift_4term, 5) if drift_4term is not None else None,
            "Allowed": drift_limit,
            "Result": round(drift_4term / drift_limit * 100, 1) if drift_4term is not None else None,
            "LDF": "-", "Combo": "4-term",
        },
        "Req. Strap Force (lbs)": {
            "Actual": round(req_strap_force, 1), "Allowed": None,
            "Result": None, "LDF": "-", "Combo": "size straps/clips",
        },
        "Req. Holdown Force (lbs)": {
            "Actual": round(req_hd_force, 1),
            "Allowed": inp.hd_capacity_lbs,
            "Result": round(req_hd_force / inp.hd_capacity_lbs * 100, 1)
            if inp.hd_capacity_lbs else None,
            "LDF": "-", "Combo": inp.load_type,
        },
    }

    return {
        "method": "FTAO (APA T555 / SDPWS 2021)",
        "results": results_block,
        "geometry": {
            "hwall_ft": round(hwall, 3),
            "Lwall_ft": round(Lwall, 3),
            "pier_lengths_ft": L,
            "opening_widths_ft": Lo,
            "pier_aspect_ratios": [round(a, 3) for a in pier_AR],
            "pier_adj_factors": [round(a, 4) if a else None for a in pier_adj],
            "ar_check": "OK" if ar_ok else "NG — aspect ratio exceeds 3.5",
        },
        "statics": {
            "H_holdown_lbs": round(H, 1),
            "va_above_below_plf": [round(x, 2) for x in va],
            "O_boundary_force_lbs": [round(x, 1) for x in O],
            "strap_forces_lbs": {
                "F_left": [round(x, 1) for x in F_left],
                "F_right": [round(x, 1) for x in F_right],
            },
            "tributary_ft": {
                "T_left": [round(x, 3) for x in T_left],
                "T_right": [round(x, 3) for x in T_right],
            },
            "pier_unit_shear_plf": [round(x, 2) for x in v_pier],
            "pier_resistance_lbs": [round(x, 1) for x in R],
            "corner_zone_shear_plf": [round(x, 2) for x in vc],
            "statics_check": "OK" if statics_ok else "NG — pier shears do not sum to V",
        },
        "design_summary": {
            "req_sheathing_capacity_plf": round(req_sheathing_capacity, 1),
            "req_strap_force_lbs": round(req_strap_force, 1),
            "req_holdown_force_lbs": round(req_hd_force, 1),
            "anchorage_unit_shear_plf": round(anchorage_unit_shear, 2),
        },
        "sheathing": {
            "v_nominal_plf": v_nom,
            "v_allow_asd_plf": round(v_allow_asd, 1) if v_nom else None,
            "demand_plf": round(req_sheathing_capacity, 1),
            "CSI": round(req_sheathing_capacity / v_allow_asd, 4) if v_nom else None,
            "check": ("OK" if sheathing_ok else "NG") if sheathing_ok is not None else "N/A",
        },
        "deflection": {
            "V_unfactored_lbs": round(Vunf, 1),
            "delta_4term_in": round(defl_4term, 4) if defl_4term is not None else None,
            "delta_3term_in": round(defl_3term, 4) if defl_3term is not None else None,
            "drift_4term": round(drift_4term, 5) if drift_4term is not None else None,
            "drift_3term": round(drift_3term, 5) if drift_3term is not None else None,
            "drift_limit": drift_limit,
            "drift_check": ("OK" if drift_ok else "NG") if drift_ok is not None else "N/A",
            "Gt": Gt, "Ga": Ga, "A_in2": A,
        },
        "warnings": warnings,
        "summary": {"overall": "PASS" if overall else "FAIL"},
    }
