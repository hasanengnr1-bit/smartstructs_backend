"""
lateral_calc.py
Lateral load generation — ASCE 7-22 seismic ELF and wind MWFRS story shears
for regular, rectangular, low-rise light-frame buildings.

Seismic — Equivalent Lateral Force procedure (ASCE 7-22 §12.8):
    Ta = 0.02 * hn^0.75                          (Table 12.8-2, all other systems)
    Cs = SDS / (R/Ie), capped at SD1/(T*(R/Ie)) for T <= TL,
         floored at max(0.044*SDS*Ie, 0.01)
         (+ 0.5*S1/(R/Ie) floor where S1 >= 0.6g)
    V  = Cs * W;  E = rho * QE
    Vertical distribution Fx = V * wx*hx^k / sum(wi*hi^k), k = 1 (T <= 0.5 s)
    Story shear at story i = sum of level forces at and above level i.
    ASD level: 0.7 * E (§2.4.5). Same shear applies to each principal
    direction (same R both ways for light-frame WSP walls).

Wind — full Directional procedure for MWFRS (ASCE 7-22 Ch. 26/27):
    qz = 0.00256 * Kz(z) * Kzt * Ke * V^2                (Eq. 26.10-1)
         Kz per Table 26.10-1 interpolated by height and exposure;
         Ke = exp(-0.0000362 * z_ground) (Table 26.9-1); Kd applied in the
         pressure equation per the 7-22 format.
    G  = 0.925 * (1 + 1.7*gQ*Iz*Q) / (1 + 1.7*gv*Iz)     (Eq. 26.11-6)
         rigid-structure gust factor from Iz (Eq. 26.11-7), Lz (26.11-9)
         and Q (26.11-8) with Table 26.11-1 terrain constants.
    p  = Kd * qz * G * Cpe  -  Kd * qi * GCpi            (Eq. 27.3-1)
         per surface zone: walls A (windward, qz banded by story) /
         B (leeward, Cp from L/B) / C, D (side, -0.7); roof split into
         windward/leeward slopes (Fig. 27.3-1 Cp interpolated on theta and
         h/L) for wind normal to a gable ridge, or distance zones from the
         windward edge (0-h/2, h/2-h, h-2h, >2h) for wind parallel to the
         ridge / flat roofs.
    Four load cases: Wind 0/90 deg x GCpi +/- (Table 26.13-1 by enclosure).
    Overall horizontal loading per direction:
         F = max(F_windward - F_leeward + F_roof,h,
                 16 psf * A_wall + 8 psf * A_roof)       (§27.1.5 minimum)
    Story shear in story i collects the net wall pressure profile from the
    story mid-height up plus all above-eave (roof + gable) forces.
    ASD level: 0.6 * W.

Building weights default to light-frame values (roof 15 / floor 15 /
walls 12 psf) and can be overridden via extracted_data["design_loads"]
(roof_dead, floor_dead, wall_dead) or extracted_data["lateral_loads"]
["weights"].
"""

import math
import re
from typing import Any, Dict, List, Optional

# ASCE 7-22 Table 26.10-1 — velocity pressure exposure coefficient Kz
# at heights (ft); linear interpolation between rows, flat below 15 ft.
_KZ_HEIGHTS = [15, 20, 25, 30, 40, 50, 60, 70, 80, 90, 100]
_KZ = {
    "B": [0.57, 0.62, 0.66, 0.70, 0.76, 0.81, 0.85, 0.89, 0.93, 0.96, 0.99],
    "C": [0.85, 0.90, 0.94, 0.98, 1.04, 1.09, 1.13, 1.17, 1.21, 1.24, 1.26],
    "D": [1.03, 1.08, 1.12, 1.16, 1.22, 1.27, 1.31, 1.34, 1.38, 1.40, 1.43],
}

# ASCE 7-22 Table 26.11-1 — terrain exposure constants for the gust factor
_TERRAIN = {
    "B": {"c": 0.30, "l_ft": 320.0, "eps": 1.0 / 3.0, "zmin_ft": 30.0},
    "C": {"c": 0.20, "l_ft": 500.0, "eps": 1.0 / 5.0, "zmin_ft": 15.0},
    "D": {"c": 0.15, "l_ft": 650.0, "eps": 1.0 / 8.0, "zmin_ft": 7.0},
}

# ASCE 7-22 Table 26.13-1 — internal pressure coefficient by enclosure class
GCPI_BY_ENCLOSURE = {
    "enclosed": 0.18,
    "partially enclosed": 0.55,
    "partially_enclosed": 0.55,
    "partially open": 0.46,
    "partially_open": 0.46,
    "open": 0.0,
}

WIND_MIN_WALL_PSF = 16.0   # ASCE 7-22 §27.1.5 minimum on wall projection
WIND_MIN_ROOF_PSF = 8.0    # ASCE 7-22 §27.1.5 minimum on roof vertical projection
WIND_MIN_PSF = WIND_MIN_WALL_PSF   # back-compat alias
DEFAULT_STORY_HEIGHT_FT = 9.0
DEFAULT_WEIGHTS_PSF = {"roof_dead": 15.0, "floor_dead": 15.0, "wall_dead": 12.0}
IE_BY_RISK = {"I": 1.0, "II": 1.0, "III": 1.25, "IV": 1.5}


def _parse_ie(risk) -> float:
    """Robust Ie lookup tolerating integer, Roman numeral, or descriptive input."""
    s = str(risk or "II").strip().upper()
    if "IV" in s or s == "4":
        return 1.5
    if "III" in s or s == "3":
        return 1.25
    return 1.0


def _f(value: Any) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def kz_at(height_ft: float, exposure: str) -> float:
    col = _KZ.get(str(exposure or "B").upper(), _KZ["B"])
    h = max(height_ft, 15.0)
    if h >= _KZ_HEIGHTS[-1]:
        return col[-1]
    for i in range(len(_KZ_HEIGHTS) - 1):
        h0, h1 = _KZ_HEIGHTS[i], _KZ_HEIGHTS[i + 1]
        if h0 <= h <= h1:
            return col[i] + (col[i + 1] - col[i]) * (h - h0) / (h1 - h0)
    return col[-1]


def candc_wall_pressure(
    qh_psf: float,
    effective_area_sf: float,
    zone: int = 4,
    GCpi: float = 0.18,
    min_psf: float = 16.0,
) -> float:
    """Components & Cladding net wall pressure per ASCE 7-22 Fig. 30.3-1
    (enclosed buildings, h <= 60 ft).

    GCp is log-interpolated on effective wind area between 10 and 500 sf:
      Zone 4 (interior): +1.0/-1.1 at 10 sf  ->  +0.7/-0.8 at 500 sf
      Zone 5 (edges):    +1.0/-1.4 at 10 sf  ->  +0.7/-0.8 at 500 sf
    Net p = qh * (|GCp| + GCpi), floored at the Sect. 30.2.2 16 psf minimum.
    The governing (larger) of positive and suction magnitudes is returned —
    appropriate for symmetric out-of-plane member design."""
    A = min(max(float(effective_area_sf or 10.0), 10.0), 500.0)
    t = math.log10(A / 10.0) / math.log10(500.0 / 10.0)
    gcp_pos = 1.0 + (0.7 - 1.0) * t
    neg_at_10 = 1.4 if zone == 5 else 1.1
    gcp_neg = neg_at_10 + (0.8 - neg_at_10) * t
    gcp = max(gcp_pos, gcp_neg)
    return max(qh_psf * (gcp + GCpi), min_psf)


def candc_component_pressure(
    V_mph: float,
    mean_roof_height_ft: float,
    effective_area_sf: float,
    exposure: str = "B",
    zone: int = 4,
    Kzt: float = 1.0,
    Kd: float = 0.85,
    ground_elevation_ft: float = 0.0,
) -> Dict[str, float]:
    """Components & Cladding design wind pressure per ASCE 7-22 Fig. 30.3-1
    (walls, enclosed buildings, h <= 60 ft) — full engine entry: computes qh
    at mean roof height and the zone 4/5 net pressure at strength and ASD
    level."""
    exposure = str(exposure or "B").upper()
    Ke = ke_factor(ground_elevation_ft)
    Kz = kz_at(float(mean_roof_height_ft), exposure)
    qh = 0.00256 * Kz * float(Kzt) * float(Kd) * Ke * float(V_mph) ** 2
    p = candc_wall_pressure(qh, float(effective_area_sf), zone=int(zone))
    return {
        "qh_psf": round(qh, 2),
        "Kz": round(Kz, 3),
        "Ke": round(Ke, 4),
        "zone": int(zone),
        "effective_wind_area_sf": float(effective_area_sf),
        "design_pressure_psf_strength": round(p, 2),
        "design_pressure_psf_asd": round(0.6 * p, 2),
    }


def cp_leeward(L_over_B: float) -> float:
    """Leeward wall Cp magnitude per ASCE 7 Fig. 27.3-1 (returned positive)."""
    if L_over_B <= 1.0:
        return 0.5
    if L_over_B <= 2.0:
        return 0.5 - 0.2 * (L_over_B - 1.0)
    if L_over_B <= 4.0:
        return 0.3 - 0.1 * (L_over_B - 2.0) / 2.0
    return 0.2


def ke_factor(ground_elevation_ft: float) -> float:
    """Ground elevation factor Ke per ASCE 7-22 Table 26.9-1 footnote."""
    return math.exp(-0.0000362 * max(float(ground_elevation_ft or 0.0), 0.0))


def gust_effect_factor(mean_roof_height_ft: float, b_ft: float, d_ft: float,
                       exposure: str = "B") -> Dict[str, float]:
    """Rigid-structure gust effect factor G per ASCE 7-22 §26.11.4.

    Uses min(b, d) + h in the background response (the smaller plan dimension
    gives the larger Q — conservative for both wind directions, and matches
    commercial implementations such as Tedds), so one G serves all load cases.
    """
    t = _TERRAIN.get(str(exposure or "B").upper(), _TERRAIN["B"])
    z_bar = max(0.6 * mean_roof_height_ft, t["zmin_ft"])
    Iz = t["c"] * (33.0 / z_bar) ** (1.0 / 6.0)          # Eq. 26.11-7
    Lz = t["l_ft"] * (z_bar / 33.0) ** t["eps"]          # Eq. 26.11-9
    gQ = gv = 3.4
    Q = math.sqrt(1.0 / (1.0 + 0.63 * ((min(b_ft, d_ft) + mean_roof_height_ft) / Lz) ** 0.63))  # Eq. 26.11-8
    G = 0.925 * (1.0 + 1.7 * gQ * Iz * Q) / (1.0 + 1.7 * gv * Iz)  # Eq. 26.11-6
    return {"z_bar_ft": z_bar, "Iz": Iz, "Lz_ft": Lz, "Q": Q, "G": G,
            "gQ": gQ, "gv": gv, "c": t["c"], "l_ft": t["l_ft"],
            "eps": t["eps"], "zmin_ft": t["zmin_ft"]}


def _interp1(x: float, xs: List[float], ys: List[float]) -> float:
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(len(xs) - 1):
        if xs[i] <= x <= xs[i + 1]:
            f = (x - xs[i]) / (xs[i + 1] - xs[i])
            return ys[i] + f * (ys[i + 1] - ys[i])
    return ys[-1]


# Fig. 27.3-1 — windward roof Cp, wind normal to ridge, theta >= 10 deg.
# Rows keyed by h/L; two values per angle: (governing negative, alt positive).
_ROOF_WW_ANGLES = [10.0, 15.0, 20.0, 25.0, 30.0, 35.0, 45.0, 60.0]
_ROOF_WW_NEG = {
    0.25: [-0.7, -0.5, -0.3, -0.2, -0.2, 0.0, 0.4, 0.6],
    0.5:  [-0.9, -0.7, -0.4, -0.3, -0.2, -0.2, 0.0, 0.6],
    1.0:  [-1.3, -1.0, -0.7, -0.5, -0.3, -0.2, 0.0, 0.6],
}
_ROOF_WW_POS = {
    0.25: [-0.18, 0.0, 0.2, 0.3, 0.3, 0.4, 0.4, 0.6],
    0.5:  [-0.18, -0.18, 0.0, 0.2, 0.2, 0.3, 0.4, 0.6],
    1.0:  [-0.18, -0.18, -0.18, 0.0, 0.2, 0.2, 0.3, 0.6],
}
# Fig. 27.3-1 — leeward roof Cp (constant for theta >= 20 deg)
_ROOF_LW_ANGLES = [10.0, 15.0, 20.0]
_ROOF_LW = {0.25: [-0.3, -0.5, -0.6], 0.5: [-0.5, -0.5, -0.6], 1.0: [-0.7, -0.6, -0.6]}


def _hl_rows(table: Dict[float, List[float]], h_over_L: float) -> List[float]:
    """Interpolate a Fig. 27.3-1 row set on h/L (rows at 0.25 / 0.5 / 1.0)."""
    hl = min(max(h_over_L, 0.25), 1.0)
    if hl <= 0.5:
        lo, hi, f = table[0.25], table[0.5], (hl - 0.25) / 0.25
    else:
        lo, hi, f = table[0.5], table[1.0], (hl - 0.5) / 0.5
    return [a + f * (b - a) for a, b in zip(lo, hi)]


def roof_cp_gable(theta_deg: float, h_over_L: float) -> Dict[str, float]:
    """Roof Cp for wind normal to a gable ridge, theta >= 10 deg
    (Fig. 27.3-1, interpolated on both theta and h/L)."""
    th = min(max(theta_deg, 10.0), 60.0)
    ww_neg = _interp1(th, _ROOF_WW_ANGLES, _hl_rows(_ROOF_WW_NEG, h_over_L))
    ww_pos = _interp1(th, _ROOF_WW_ANGLES, _hl_rows(_ROOF_WW_POS, h_over_L))
    lw = _interp1(th, _ROOF_LW_ANGLES, _hl_rows(_ROOF_LW, h_over_L))
    return {"windward_neg": ww_neg, "windward_pos": ww_pos, "leeward": lw}


def roof_zones_distance(h_ft: float, along_wind_ft: float,
                         h_over_L: float) -> List[Dict[str, Any]]:
    """Roof Cp zones by distance from the windward edge — flat roofs
    (theta < 10 deg) and wind parallel to a ridge (Fig. 27.3-1).
    Returns [{label, x0_ft, x1_ft, cp}] clipped to the along-wind length."""
    hl = min(max(h_over_L, 0.5), 1.0)
    f = (hl - 0.5) / 0.5   # 0 at h/L<=0.5  ->  1 at h/L>=1.0
    bounds = [0.0, h_ft / 2.0, h_ft, 2.0 * h_ft, float("inf")]
    cps_low = [-0.9, -0.9, -0.5, -0.3]      # h/L <= 0.5
    cps_high = [-1.3, -0.7, -0.7, -0.7]     # h/L >= 1.0
    labels = ["A", "B", "C", "D"]
    zones = []
    for i in range(4):
        x0 = bounds[i]
        x1 = min(bounds[i + 1], along_wind_ft)
        if x1 <= x0:
            break
        zones.append({"label": labels[i], "x0_ft": x0, "x1_ft": x1,
                      "cp": cps_low[i] + f * (cps_high[i] - cps_low[i])})
    return zones


def compute_seismic_elf(SDS: float, SD1: Optional[float], story_weights_lbs: List[float],
                        story_heights_ft: List[float], R: float = 6.5, Ie: float = 1.0,
                        rho: float = 1.0, S1: Optional[float] = None,
                        TL: Optional[float] = None, W_foundation: float = 0.0) -> Dict[str, Any]:
    """ELF story shears (strength level). story_weights/heights bottom-up;
    weight i is the mass tributary to the level at the top of story i
    (roof weight last). W_foundation is mass at ground level (e.g. lower
    half of first-story walls) — included in W but generates zero lateral
    force since h=0."""
    n = len(story_weights_lbs)
    hn = sum(story_heights_ft)
    Ta = 0.02 * hn ** 0.75
    r_over_ie = R / Ie

    Cs = SDS / r_over_ie
    if SD1 and Ta > 0:
        Cs_upper = SD1 / (Ta * r_over_ie)
        if TL is not None and Ta > TL:
            Cs_upper = SD1 * TL / (Ta ** 2 * r_over_ie)
        Cs = min(Cs, Cs_upper)
    Cs = max(Cs, 0.044 * SDS * Ie, 0.01)
    if S1 is not None and S1 >= 0.6:
        Cs = max(Cs, 0.5 * S1 / r_over_ie)

    W = sum(story_weights_lbs) + W_foundation
    V = Cs * W

    k = 1.0 if Ta <= 0.5 else min(1.0 + (Ta - 0.5) / 2.0, 2.0)  # §12.8.3
    hx = []
    z = 0.0
    for h in story_heights_ft:
        z += h
        hx.append(z)
    denom = sum(w * h ** k for w, h in zip(story_weights_lbs, hx))
    F = [V * w * h ** k / denom if denom > 0 else 0.0
         for w, h in zip(story_weights_lbs, hx)]
    story_shear = [rho * sum(F[i:]) for i in range(n)]   # E = rho*QE

    return {
        "Ta_s": Ta, "Cs": Cs, "W_lbs": W, "V_base_lbs": V,
        "V_base_rho_lbs": rho * V, "k": k,
        "level_forces_lbs": F,
        "story_shears_lbs": story_shear,                 # strength level
        "story_shears_asd_lbs": [0.7 * v for v in story_shear],
    }


def _qz(z_ft: float, exposure: str, Kzt: float, Ke: float, V_mph: float) -> float:
    """Velocity pressure qz per ASCE 7-22 Eq. 26.10-1 (Kd applied in the
    pressure equation, per the 7-22 format)."""
    return 0.00256 * kz_at(z_ft, exposure) * Kzt * Ke * V_mph ** 2


def compute_wind_directional(
    V_mph: float,
    length_ft: float,
    width_ft: float,
    eave_height_ft: float,
    exposure: str = "B",
    roof_type: str = "gable",
    roof_pitch_deg: Optional[float] = None,
    ridge_height_ft: Optional[float] = None,
    Kzt: float = 1.0,
    Kd: float = 0.85,
    ground_elevation_ft: float = 0.0,
    enclosure: str = "enclosed",
    risk_category: str = "II",
    story_heights_ft: Optional[List[float]] = None,
) -> Dict[str, Any]:
    """Full ASCE 7-22 Ch. 27 directional procedure for a rectangular
    enclosed/partially-enclosed building with a flat or gable roof
    (ridge parallel to the long plan dimension).

    Wind 0 deg  = normal to the ridge (pressure on the length_ft face).
    Wind 90 deg = parallel to the ridge (pressure on the gable-end face).

    Returns building data, gust factor derivation, velocity pressure table,
    four load cases (Wind 0/90 x GCpi +/-) with per-zone pressure/force
    tables, and per-case overall loading against the §27.1.5 minimums.
    """
    b = float(length_ft)          # ridge-parallel plan dimension
    d = float(width_ft)           # across-ridge plan dimension
    H = float(eave_height_ft)
    exposure = str(exposure or "B").upper()

    # ---- roof geometry ----
    rt = str(roof_type or "gable").lower()
    theta = float(roof_pitch_deg) if roof_pitch_deg is not None else None
    if theta is None and ridge_height_ft and float(ridge_height_ft) > H and d > 0:
        theta = math.degrees(math.atan((float(ridge_height_ft) - H) / (d / 2.0)))
    if theta is None or rt == "flat":
        theta = 0.0
    z_ridge = float(ridge_height_ft) if ridge_height_ft else H + (d / 2.0) * math.tan(math.radians(theta))
    # ASCE 7 definition: h = mean roof height, except eave height for theta <= 10
    h = (H + z_ridge) / 2.0 if theta > 10.0 else H
    gabled = theta >= 10.0 and rt not in ("flat",)

    # ---- general requirements ----
    Ke = ke_factor(ground_elevation_ft)
    gcpi = GCPI_BY_ENCLOSURE.get(str(enclosure or "enclosed").lower().strip(), 0.18)
    gust = gust_effect_factor(h, b, d, exposure)
    G = gust["G"]
    qh = _qz(h, exposure, Kzt, Ke, V_mph)
    qi = qh                                   # enclosed: qi = qh (conservative)

    cos_t, sin_t, tan_t = (math.cos(math.radians(theta)),
                           math.sin(math.radians(theta)),
                           math.tan(math.radians(theta)))

    # ---- velocity pressure table ----
    z_list: List[float] = []
    z_acc = 0.0
    for sh in (story_heights_ft or []):
        z_acc += float(sh)
        z_list.append(z_acc)
    for z in (H, h, z_ridge):
        z_list.append(z)
    z_list = sorted({round(z, 2) for z in z_list if z > 0})
    velocity_pressures = [
        {"z_ft": z, "Kz": round(kz_at(z, exposure), 3),
         "qz_psf": round(_qz(z, exposure, Kzt, Ke, V_mph), 2)}
        for z in z_list
    ]

    def p_net(q: float, cpe: float, gcpi_signed: float) -> float:
        return Kd * q * G * cpe - Kd * qi * gcpi_signed        # Eq. 27.3-1

    def wall_bands(face_w: float, gable_tri_sf: float) -> List[Dict[str, float]]:
        """Windward wall split into story bands (qz at band top) plus the
        gable triangle at ridge height when the gable end faces the wind."""
        bands = []
        if story_heights_ft:
            z0 = 0.0
            for sh in story_heights_ft:
                z1 = min(z0 + float(sh), H)
                if z1 <= z0:
                    break
                bands.append({"z_ref": z1, "area": face_w * (z1 - z0)})
                z0 = z1
            if z0 < H - 1e-6:
                bands.append({"z_ref": H, "area": face_w * (H - z0)})
        else:
            bands.append({"z_ref": H, "area": face_w * H})
        if gable_tri_sf > 0:
            bands.append({"z_ref": z_ridge, "area": gable_tri_sf})
        return bands

    gable_tri_sf = d * d * tan_t / 4.0 if gabled else 0.0   # both half-triangles

    cases: List[Dict[str, Any]] = []
    for direction in (0, 90):
        if direction == 0:
            face_w, along = b, d                 # pressure on the long face
            ww_area_extra = 0.0                  # rectangular windward wall
            side_area = d * H + gable_tri_sf     # gable ends are side walls
            lw_area = b * H
            avert_wall = b * H
            avert_roof = b * (d / 2.0) * tan_t if gabled else 0.0
        else:
            face_w, along = d, b                 # pressure on the gable end
            ww_area_extra = gable_tri_sf
            side_area = b * H
            lw_area = d * H + gable_tri_sf
            avert_wall = d * H + gable_tri_sf
            avert_roof = 0.0

        cp_lw_wall = cp_leeward(along / face_w if face_w > 0 else 1.0)
        h_over_L = h / along if along > 0 else 1.0

        # roof zoning for this direction
        normal_to_ridge = gabled and direction == 0
        if normal_to_ridge:
            rcp = roof_cp_gable(theta, h_over_L)
            slope_area = b * (d / 2.0) / cos_t
        else:
            dist_zones = roof_zones_distance(h, along, h_over_L)
            width_across = (d / cos_t) if (gabled and direction == 90) else face_w

        for gcpi_sign, cpe_pick in ((+1, "neg"), (-1, "pos")):
            gcpi_s = gcpi_sign * gcpi
            case_name = (f"Wind {direction}°, GCpi {gcpi_s:+.2f}, "
                         f"{'-' if cpe_pick == 'neg' else '+'}cpe")

            # ---- roof zones ----
            roof_rows: List[Dict[str, Any]] = []
            Fv = Fh = 0.0     # vertical (down +) and along-wind horizontal, lbs
            if normal_to_ridge:
                cpe_ww = rcp["windward_neg"] if cpe_pick == "neg" else rcp["windward_pos"]
                for label, cpe, hor_sign in (("A (windward slope)", cpe_ww, +1),
                                             ("B (leeward slope)", rcp["leeward"], -1)):
                    p = p_net(qh, cpe, gcpi_s)
                    F = p * slope_area
                    roof_rows.append({"zone": label, "z_ref_ft": round(h, 2),
                                      "cpe": round(cpe, 2), "qp_psf": round(qh, 2),
                                      "p_psf": round(p, 2), "area_sf": round(slope_area, 2),
                                      "F_lbs": round(F, 0)})
                    Fv += F * cos_t
                    Fh += hor_sign * F * sin_t
            else:
                for zn in dist_zones:
                    cpe = zn["cp"] if cpe_pick == "neg" else -0.18
                    p = p_net(qh, cpe, gcpi_s)
                    area = (zn["x1_ft"] - zn["x0_ft"]) * width_across
                    F = p * area
                    label = (f"{zn['label']} ({_ft_short(zn['x0_ft'], h)}"
                             f"→{_ft_short(zn['x1_ft'], h)})")
                    roof_rows.append({"zone": label, "z_ref_ft": round(h, 2),
                                      "cpe": round(cpe, 2), "qp_psf": round(qh, 2),
                                      "p_psf": round(p, 2), "area_sf": round(area, 2),
                                      "F_lbs": round(F, 0)})
                    Fv += F * (cos_t if gabled and direction == 90 else 1.0)
                    # across-ridge slopes / flat zones: no along-wind component

            # ---- wall zones ----
            wall_rows: List[Dict[str, Any]] = []
            F_ww = 0.0
            for band in wall_bands(face_w, ww_area_extra):
                q_band = _qz(band["z_ref"], exposure, Kzt, Ke, V_mph)
                p = p_net(q_band, 0.8, gcpi_s)
                F = p * band["area"]
                F_ww += F
                wall_rows.append({"zone": "A (windward)", "z_ref_ft": round(band["z_ref"], 2),
                                  "cpe": 0.8, "qp_psf": round(q_band, 2),
                                  "p_psf": round(p, 2), "area_sf": round(band["area"], 2),
                                  "F_lbs": round(F, 0)})
            p_lw = p_net(qh, -cp_lw_wall, gcpi_s)
            F_lw = p_lw * lw_area
            wall_rows.append({"zone": "B (leeward)", "z_ref_ft": round(h, 2),
                              "cpe": round(-cp_lw_wall, 2), "qp_psf": round(qh, 2),
                              "p_psf": round(p_lw, 2), "area_sf": round(lw_area, 2),
                              "F_lbs": round(F_lw, 0)})
            p_side = p_net(qh, -0.7, gcpi_s)
            for label in ("C (side)", "D (side)"):
                wall_rows.append({"zone": label, "z_ref_ft": round(h, 2),
                                  "cpe": -0.7, "qp_psf": round(qh, 2),
                                  "p_psf": round(p_side, 2), "area_sf": round(side_area, 2),
                                  "F_lbs": round(p_side * side_area, 0)})

            # ---- overall loading (§27.1.5 minimum enforced) ----
            F_min = WIND_MIN_WALL_PSF * avert_wall + WIND_MIN_ROOF_PSF * avert_roof
            F_total = max(F_ww - F_lw + Fh, F_min)
            cases.append({
                "name": case_name, "direction_deg": direction,
                "GCpi": round(gcpi_s, 2), "cpe_case": cpe_pick,
                "roof_zones": roof_rows,
                "roof_total_vertical_lbs": round(Fv, 0),
                "roof_total_horizontal_lbs": round(Fh, 0),
                "wall_zones": wall_rows,
                "overall": {
                    "A_vert_wall_sf": round(avert_wall, 2),
                    "A_vert_roof_sf": round(avert_roof, 2),
                    "F_min_lbs": round(F_min, 0),
                    "F_windward_lbs": round(F_ww, 0),
                    "F_leeward_lbs": round(F_lw, 0),
                    "F_roof_horiz_lbs": round(Fh, 0),
                    "F_total_lbs": round(F_total, 0),
                    "governed_by_minimum": F_total == F_min and (F_ww - F_lw + Fh) < F_min,
                },
            })

    # governing horizontal load per direction
    summary = {}
    for direction in (0, 90):
        dir_cases = [c for c in cases if c["direction_deg"] == direction]
        gov = max(dir_cases, key=lambda c: c["overall"]["F_total_lbs"])
        uplift = min(c["roof_total_vertical_lbs"] for c in dir_cases)
        summary[f"wind_{direction}"] = {
            "F_total_lbs": gov["overall"]["F_total_lbs"],
            "governing_case": gov["name"],
            "max_roof_uplift_lbs": round(uplift, 0),
        }

    return {
        "building": {"length_ft": b, "width_ft": d, "eave_height_ft": H,
                     "ridge_height_ft": round(z_ridge, 2),
                     "mean_roof_height_ft": round(h, 2),
                     "roof_type": "gable" if gabled else "flat/monoslope (<10°)",
                     "roof_pitch_deg": round(theta, 1)},
        "requirements": {"V_mph": V_mph, "risk_category": str(risk_category),
                         "Kd": Kd, "Kzt": Kzt,
                         "ground_elevation_ft": float(ground_elevation_ft or 0.0),
                         "Ke": round(Ke, 4), "exposure": exposure,
                         "enclosure": str(enclosure), "GCpi": gcpi},
        "gust": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in gust.items()},
        "velocity_pressures": velocity_pressures,
        "qh_psf": round(qh, 2), "qi_psf": round(qi, 2),
        "min_wall_psf": WIND_MIN_WALL_PSF, "min_roof_psf": WIND_MIN_ROOF_PSF,
        "load_cases": cases,
        "summary": summary,
    }


def _ft_short(x: float, h: float) -> str:
    if x == float("inf"):
        return "end"
    if abs(x - h / 2.0) < 0.01:
        return "h/2"
    if abs(x - h) < 0.01:
        return "h"
    if abs(x - 2 * h) < 0.01:
        return "2h"
    return f"{x:.0f}ft"


def wind_story_shears(pkg: Dict[str, Any], story_heights_ft: List[float],
                      direction_deg: int) -> Dict[str, Any]:
    """Diaphragm-tributary story shears (strength level) from the governing
    directional load case: net windward-leeward wall pressure profile
    integrated from each story mid-height up, plus all above-eave forces
    (roof horizontal + gable triangle) at the top diaphragm; scaled up if the
    §27.1.5 minimum governs."""
    req = pkg["requirements"]
    bld = pkg["building"]
    H = bld["eave_height_ft"]
    exposure, Kzt, Ke, V, Kd = (req["exposure"], req["Kzt"], req["Ke"],
                                req["V_mph"], req["Kd"])
    G = pkg["gust"]["G"]
    qh = pkg["qh_psf"]

    if direction_deg == 0:
        face_w, along = bld["length_ft"], bld["width_ft"]
    else:
        face_w, along = bld["width_ft"], bld["length_ft"]
    cp_lw = cp_leeward(along / face_w if face_w > 0 else 1.0)

    dir_cases = [c for c in pkg["load_cases"] if c["direction_deg"] == direction_deg]
    gov = max(dir_cases, key=lambda c: c["overall"]["F_total_lbs"])
    ov = gov["overall"]

    # net wall pressure profile (GCpi cancels windward-leeward)
    def p_net_wall(z: float) -> float:
        return Kd * G * (_qz(z, exposure, Kzt, Ke, V) * 0.8 + qh * cp_lw)

    # above-eave horizontal force: roof slopes + gable triangles
    F_top = max(0.0, ov["F_roof_horiz_lbs"])
    tri = bld["width_ft"] ** 2 * math.tan(math.radians(bld["roof_pitch_deg"])) / 4.0
    if direction_deg == 90 and tri > 0:
        F_top += p_net_wall(bld["ridge_height_ft"]) * tri

    def load_above(z: float) -> float:
        steps = 40
        dz = max(H - z, 0.0) / steps
        wall = sum(p_net_wall(z + (i + 0.5) * dz) * face_w * dz for i in range(steps)) if dz > 0 else 0.0
        return wall + F_top

    total = load_above(0.0)
    scale = ov["F_total_lbs"] / total if total > 0 and ov["F_total_lbs"] > total else 1.0

    shears = []
    z0 = 0.0
    for h_st in story_heights_ft:
        shears.append(scale * load_above(z0 + float(h_st) / 2.0))
        z0 += float(h_st)
    return {
        "story_shears_lbs": shears,
        "story_shears_asd_lbs": [0.6 * v for v in shears],
        "base_shear_lbs": round(scale * total, 0),
        "base_shear_asd_lbs": round(0.6 * scale * total, 0),
        "governing_case": gov["name"],
        "governed_by_minimum": ov["governed_by_minimum"],
        "Cp_leeward": round(cp_lw, 2),
    }


def _story_weights(areas_sf: List[float], story_heights_ft: List[float],
                   perimeter_ft: float, w: Dict[str, float]) -> tuple[List[float], float]:
    """Level weights bottom-up: floors carry floor DL + half the walls above
    and below; the roof level carries roof DL + half the top story walls.
    Returns (level_weights, foundation_wall_lbs) — the foundation portion
    (lower half of story 1 walls) must be added to W per ASCE 7 §12.7.2
    but generates zero lateral force at h=0."""
    n = len(story_heights_ft)
    weights = []
    for i in range(n):
        wall_below = perimeter_ft * story_heights_ft[i] / 2.0 * w["wall_dead"]
        wall_above = (perimeter_ft * story_heights_ft[i + 1] / 2.0 * w["wall_dead"]
                      if i + 1 < n else 0.0)
        area = areas_sf[min(i, len(areas_sf) - 1)] if areas_sf else 0.0
        plate = w["roof_dead"] if i == n - 1 else w["floor_dead"]
        weights.append(area * plate + wall_below + wall_above)
    foundation_wall_lbs = perimeter_ft * story_heights_ft[0] / 2.0 * w["wall_dead"]
    return weights, foundation_wall_lbs


def compute_lateral_loads(extracted_data: Dict[str, Any], hazard_data: Dict[str, Any]) -> Dict[str, Any]:
    """Auto-generate per-story, per-direction ASD story shears from hazard
    lookup + building geometry. Returns {} when required data is missing.

    Direction convention matches the extraction prompt: X-lines resist load
    along the building's long axis (length_ft), Y-lines along the short axis.

    Output: {"story_shears": {"1": {"X": {"Vs", "Vw"}, "Y": {...}}, ...},
             "meta": {...assumptions and intermediate values...}}
    """
    # Geometry / site context live under extracted_data["project_context"] in
    # the extraction pipeline, but the chatbot tool passes them flat at the top
    # level. Resolve top-level first, then fall back to project_context so both
    # shapes work.
    pc_raw = extracted_data.get("project_context")
    pc: Dict[str, Any] = pc_raw if isinstance(pc_raw, dict) else {}

    def _ctx(key: str) -> Dict[str, Any]:
        val = extracted_data.get(key)
        if not isinstance(val, dict) or not val:
            val = pc.get(key)
        return val if isinstance(val, dict) else {}

    geo = _ctx("building_geometry")
    hazard = hazard_data
    if not isinstance(hazard, dict):
        return {}
    seismic_raw = hazard.get("seismic")
    seismic: Dict[str, Any] = seismic_raw if isinstance(seismic_raw, dict) else {}
    wind_raw = hazard.get("wind")
    wind: Dict[str, Any] = wind_raw if isinstance(wind_raw, dict) else {}
    hz_input_raw = hazard.get("input")
    hz_input: Dict[str, Any] = hz_input_raw if isinstance(hz_input_raw, dict) else {}
    wctx = _ctx("wind_context")
    sctx = _ctx("seismic_context")
    user = extracted_data.get("lateral_loads") or {}
    design_loads = _ctx("design_loads")

    length = _f(geo.get("length_ft"))
    width = _f(geo.get("width_ft"))
    if not length or not width:
        return {}

    heights = [h for h in (geo.get("story_heights_ft") or []) if _f(h)]
    heights = [float(h) for h in heights]
    if not heights:
        n = int(_f(geo.get("number_of_stories")) or 1)
        heights = [DEFAULT_STORY_HEIGHT_FT] * n
    n = len(heights)
    hn = sum(heights)
    h_mean = _f(geo.get("mean_roof_height_ft"))
    if not h_mean:
        eave = _f(geo.get("eave_height_ft")) or hn
        ridge = _f(geo.get("ridge_height_ft")) or eave
        h_mean = (eave + ridge) / 2.0

    areas = [float(a) for a in (geo.get("floor_area_per_story_sf") or []) if _f(a)]
    if not areas:
        areas = [length * width] * n

    w_psf = dict(DEFAULT_WEIGHTS_PSF)
    for key in w_psf:
        user_val = _f((user.get("weights") or {}).get(key))
        design_val = _f(design_loads.get(key))
        if design_val is None:
            # extraction schema uses the long form: roof_dead_load_psf etc.
            design_val = _f(design_loads.get(f"{key}_load_psf"))
        if user_val is not None:
            w_psf[key] = user_val
        elif design_val is not None:
            w_psf[key] = design_val

    meta: Dict[str, Any] = {"stories": n, "story_heights_ft": heights,
                            "length_ft": length, "width_ft": width,
                            "mean_roof_height_ft": h_mean, "weights_psf": w_psf,
                            "assumptions": []}

    # ---- Seismic (same shear each direction) ----
    sds = _f(seismic.get("sds"))
    seismic_asd = None
    if sds:
        Ie = _parse_ie(hz_input.get("risk_category"))
        R = _f(sctx.get("response_modification_R")) or 6.5
        rho = _f(sctx.get("redundancy_rho")) or 1.0
        perimeter = 2.0 * (length + width)
        weights, W_foundation = _story_weights(areas, heights, perimeter, w_psf)
        elf = compute_seismic_elf(
            SDS=sds, SD1=_f(seismic.get("sd1")), S1=_f(seismic.get("s1")),
            story_weights_lbs=weights, story_heights_ft=heights,
            R=R, Ie=Ie, rho=rho, W_foundation=W_foundation)
        seismic_asd = elf["story_shears_asd_lbs"]
        meta["seismic"] = {k: elf[k] for k in ("Ta_s", "Cs", "W_lbs", "V_base_lbs", "k")}
        meta["seismic"].update({"R": R, "Ie": Ie, "rho": rho,
                                "story_shears_asd_lbs": seismic_asd})
        meta["assumptions"].append(
            f"Seismic ELF: R={R}, Ie={Ie}, rho={rho}, estimated W from "
            f"roof {w_psf['roof_dead']} / floor {w_psf['floor_dead']} / "
            f"wall {w_psf['wall_dead']} psf")
    else:
        meta["assumptions"].append("No SDS in hazard lookup — seismic story shears not computed")

    # ---- Wind (full Ch. 27 directional procedure) ----
    v_mph = _f(wind.get("wind_speed_mph"))
    wind_asd = {}
    if v_mph:
        # exposure precedence: engineer's project input > plan-extracted > B
        exposure = str(
            hz_input.get("exposure_category")
            or wctx.get("exposure_category")
            or "B"
        )
        Kzt_input = _f(wctx.get("Kzt"))
        if Kzt_input is not None:
            Kzt = Kzt_input
        elif wctx.get("topography_flat", True):
            Kzt = 1.0
        else:
            Kzt = 1.0
            meta["assumptions"].append("Non-flat topography flagged but Kzt not provided; used Kzt=1.0 pending engineer review")

        eave = _f(geo.get("eave_height_ft")) or hn
        pkg = compute_wind_directional(
            V_mph=v_mph, length_ft=length, width_ft=width,
            eave_height_ft=eave, exposure=exposure,
            roof_type=str(geo.get("roof_type") or "gable"),
            roof_pitch_deg=_f(geo.get("roof_pitch_deg")),
            ridge_height_ft=_f(geo.get("ridge_height_ft")),
            Kzt=Kzt,
            ground_elevation_ft=_f(wctx.get("ground_elevation_ft")) or 0.0,
            enclosure=str(wctx.get("enclosure_classification") or "enclosed"),
            risk_category=str(hz_input.get("risk_category") or "II"),
            story_heights_ft=heights)
        # X-direction load (along long axis) = wind parallel to ridge (90°),
        # pressure on the short face; Y = wind normal to ridge (0°).
        wx = wind_story_shears(pkg, heights, direction_deg=90)
        wy = wind_story_shears(pkg, heights, direction_deg=0)
        wind_asd = {"X": wx["story_shears_asd_lbs"], "Y": wy["story_shears_asd_lbs"]}
        meta["wind"] = {"V_mph": v_mph, "exposure": exposure, "Kzt": Kzt,
                        "Ke": pkg["requirements"]["Ke"],
                        "G": pkg["gust"]["G"], "qh_psf": pkg["qh_psf"],
                        "enclosure": pkg["requirements"]["enclosure"],
                        "GCpi": pkg["requirements"]["GCpi"],
                        "X": {"qh_psf": pkg["qh_psf"], "Cp_leeward": wx["Cp_leeward"],
                              "governing_case": wx["governing_case"],
                              "base_shear_asd_lbs": wx["base_shear_asd_lbs"]},
                        "Y": {"qh_psf": pkg["qh_psf"], "Cp_leeward": wy["Cp_leeward"],
                              "governing_case": wy["governing_case"],
                              "base_shear_asd_lbs": wy["base_shear_asd_lbs"]},
                        "directional": pkg}
        meta["assumptions"].append(
            f"Wind MWFRS directional (Ch. 27, 4 load cases): V={v_mph} mph, "
            f"Exp {exposure}, Kd=0.85, Ke={pkg['requirements']['Ke']}, "
            f"G={pkg['gust']['G']:.2f} (§26.11 rigid), GCpi=±{pkg['requirements']['GCpi']}, "
            f"§27.1.5 minimums 16/8 psf enforced")
    else:
        meta["assumptions"].append("No wind speed in hazard lookup — wind story shears not computed")

    if seismic_asd is None and not wind_asd:
        return {}

    story_shears: Dict[str, Dict[str, Dict[str, float]]] = {}
    for i in range(n):
        story = str(i + 1)
        vs = round(seismic_asd[i], 1) if seismic_asd else 0.0
        story_shears[story] = {
            "X": {"Vs": vs, "Vw": round(wind_asd["X"][i], 1) if wind_asd else 0.0},
            "Y": {"Vs": vs, "Vw": round(wind_asd["Y"][i], 1) if wind_asd else 0.0},
        }
    return {"story_shears": story_shears, "meta": meta}


_STORY_WORDS = {
    "first": 1, "ground": 1, "main": 1, "1st": 1,
    "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4,
}


def story_number(story_label: Any, total_stories: int | None = None) -> Optional[int]:
    """'Level 2' / '2F' / 'second floor' -> 2. None if unresolvable
    (unless the building is single-story, then 1)."""
    s = str(story_label or "").strip().lower()
    m = re.search(r"\d+", s)
    if m:
        return int(m.group())
    for word, num in _STORY_WORDS.items():
        if word in s:
            return num
    if total_stories == 1:
        return 1
    return None
