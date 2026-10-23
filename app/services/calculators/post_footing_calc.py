"""
post_footing_calc.py
Post / Pad Footing Design Calculator — ACI 318.

Handles isolated pad footings with:
- Concentric and eccentric axial loading
- One-way (beam) shear
- Two-way (punching) shear
- Flexural reinforcement in both directions
- Development length
- Bar spacing

Based on Post_Footing.xls formulas.

Run:   python post_footing_calc.py
Import: from post_footing_calc import check_post_footing
"""

import math
from dataclasses import dataclass
from typing import Literal, Optional


# ============================================================
# Lookup tables
# ============================================================
REBAR = {
    "#3": {"dia": 0.375, "area": 0.11},
    "#4": {"dia": 0.500, "area": 0.20},
    "#5": {"dia": 0.625, "area": 0.31},
    "#6": {"dia": 0.750, "area": 0.44},
    "#7": {"dia": 0.875, "area": 0.60},
    "#8": {"dia": 1.000, "area": 0.79},
    "#9": {"dia": 1.128, "area": 1.00},
    "#10": {"dia": 1.270, "area": 1.27},
    "#11": {"dia": 1.410, "area": 1.56},
}


# ============================================================
# Inputs
# ============================================================
@dataclass
class PostFootingInputs:
    # Footing dimensions
    B1: float = 2.5  # ft  — shorter width
    B2: float = 3.0  # ft  — longer width
    H: float = 12.0  # in  — thickness
    cover: float = 3.0  # in  — concrete cover
    depth: float = 3.0  # ft  — depth below grade

    # Column
    a1: float = 6.0  # in  — column width in B1 direction
    a2: float = 6.0  # in  — column width in B2 direction
    bar_no_col: str = "#4"

    # Material
    fc: float = 3000.0  # psi
    fy: float = 60000.0  # psi
    gamma_conc: float = 150.0  # pcf
    gamma_soil: float = 120.0  # pcf

    # Loading
    DL: float = 10_000.0  # lb
    LL: float = 5_000.0  # lb
    SL: float = 0.0  # lb — snow load (cascaded from post)
    Mu_ftkips: float = 0.0  # ft-kips

    # Soil
    q_allow_ton: float = 2.0  # ton/ft^2  (2000 psf)
    surcharge_ft: float = 0.0  # ft of soil surcharge

    # Reinforcement: bar size (if None, auto-sized)
    bar_B1: Optional[str] = None  # bar size in B1 direction
    bar_B2: Optional[str] = None  # bar size in B2 direction
    bar_spacing_B1: Optional[float] = None  # provided spacing B1 (in)
    bar_spacing_B2: Optional[float] = None  # provided spacing B2 (in)

    # Location (for punching shear alpha_s)
    column_location: Literal["interior", "edge", "corner"] = "interior"


# ============================================================
# Core calculations
# ============================================================
def check_post_footing(inp: PostFootingInputs):
    """Run full post footing design per ACI 318."""
    # ---- Unpack ----
    B1, B2 = inp.B1, inp.B2
    H = inp.H
    cover = inp.cover
    fc, fy = inp.fc, inp.fy
    gamma_c, gamma_s = inp.gamma_conc, inp.gamma_soil
    a1 = inp.a1 / 12.0  # ft
    a2 = inp.a2 / 12.0  # ft

    DL, LL, SL = inp.DL, inp.LL, inp.SL
    lrfd_combos = [
        ("1.4D", 1.4 * DL),
        ("1.2D+1.6L+0.5S", 1.2 * DL + 1.6 * LL + 0.5 * SL),
        ("1.2D+1.6S+0.5L", 1.2 * DL + 1.6 * SL + 0.5 * LL),
        ("1.2D+1.6L", 1.2 * DL + 1.6 * LL),
    ]
    combo_gov = max(lrfd_combos, key=lambda c: c[1])
    Pu = combo_gov[1]
    combo_label = combo_gov[0]

    # Effective depth
    bar_dia = REBAR.get(inp.bar_no_col, REBAR["#4"])["dia"]
    d_in = H - cover - bar_dia / 2.0
    d_ft = d_in / 12.0

    # ============================================================
    # Soil bearing (ASD — gross approach, service loads)
    # ============================================================
    allowable_psf = inp.q_allow_ton * 2000.0
    area_selected = B1 * B2
    footing_wt = area_selected * (H / 12.0) * gamma_c
    P_service = DL + LL + SL + footing_wt
    area_required = P_service / allowable_psf if allowable_psf > 0 else 999
    q_avg = P_service / area_selected if area_selected > 0 else 0

    M_service_ftlb = inp.Mu_ftkips * 1000.0
    e_ft = M_service_ftlb / P_service if P_service > 0 else 0
    kern = B1 / 6.0

    if M_service_ftlb > 0:
        S_ft3 = B2 * (B1**2) / 6.0
        if e_ft <= kern:
            q_max_psf = P_service / area_selected + M_service_ftlb / S_ft3
            q_min_psf = P_service / area_selected - M_service_ftlb / S_ft3
            partial_bearing = False
        else:
            B1_eff = 3.0 * (B1 / 2.0 - e_ft)
            area_eff = B1_eff * B2
            q_max_psf = 2.0 * P_service / area_eff if area_eff > 0 else 999
            q_min_psf = 0.0
            partial_bearing = True
    else:
        q_max_psf = q_avg
        q_min_psf = q_avg
        partial_bearing = False

    soil_ok = q_max_psf <= allowable_psf and allowable_psf > 0

    # Factored uniform pressure for LRFD structural checks
    qu_psf = Pu / area_selected if area_selected > 0 else 0

    # ============================================================
    # One-way shear (both directions, governing reported)
    # ============================================================
    phi_shear = 0.75

    def _oneway_shear(B_ft, B_perp_ft, a_dim_ft):
        crit_ft = max((B_ft - a_dim_ft) / 2.0 - d_ft, 0)
        Vu = qu_psf * B_perp_ft * crit_ft
        bw = B_perp_ft * 12.0
        Vc = 2.0 * math.sqrt(fc) * bw * d_in
        phi_Vc = phi_shear * Vc
        d_req = Vu / (phi_shear * 2.0 * math.sqrt(fc) * bw) if Vu > 0 else 0
        return Vu, phi_Vc, d_req

    Vu_B1, phiVc_B1, dreq_B1 = _oneway_shear(B1, B2, a1)
    Vu_B2, phiVc_B2, dreq_B2 = _oneway_shear(B2, B1, a2)

    util_B1 = Vu_B1 / phiVc_B1 if phiVc_B1 > 0 else 0
    util_B2 = Vu_B2 / phiVc_B2 if phiVc_B2 > 0 else 0
    if util_B1 >= util_B2:
        Vu_oneway, phi_Vc_oneway, d_req_oneway = Vu_B1, phiVc_B1, dreq_B1
    else:
        Vu_oneway, phi_Vc_oneway, d_req_oneway = Vu_B2, phiVc_B2, dreq_B2
    oneway_ok = phi_Vc_oneway >= Vu_oneway

    # ============================================================
    # Two-way (punching) shear
    # ============================================================
    # Critical perimeter at d/2 from column face
    bo_in = 2.0 * (a1 * 12.0 + d_in) + 2.0 * (a2 * 12.0 + d_in)
    beta_c = max(a1 / a2, a2 / a1) if min(a1, a2) > 0 else 1.0

    alpha_s_map = {"interior": 40, "edge": 30, "corner": 20}
    alpha_s = alpha_s_map.get(inp.column_location, 40)

    Vc1 = (2.0 + 4.0 / beta_c) * math.sqrt(fc) * bo_in * d_in
    Vc2 = (alpha_s * d_in / bo_in + 2.0) * math.sqrt(fc) * bo_in * d_in
    Vc3 = 4.0 * math.sqrt(fc) * bo_in * d_in
    Vc_punch = min(Vc1, Vc2, Vc3)
    phi_Vc_punch = phi_shear * Vc_punch

    # Vu for punching: subtract soil pressure under the critical area
    punch_area = (a1 + d_ft) * (a2 + d_ft)
    Vu_punch = Pu - qu_psf * punch_area
    if Vu_punch < 0:
        Vu_punch = 0
    punching_ok = phi_Vc_punch >= Vu_punch

    # Required d for punching
    d_req_punch = 0.0
    if Vu_punch > 0:
        d_req_punch = (
            Vu_punch
            / (phi_shear * 4.0 * math.sqrt(fc) * (2.0 * (a1 * 12.0 + a2 * 12.0)))
        ) * 2

    # ============================================================
    # Flexural reinforcement — B1 direction (shorter)
    # ============================================================
    def _flexure_dir(B_ft, B_perp_ft, a_ft):
        L_cant = (B_ft - a_ft) / 2.0
        Mu_ftlb = qu_psf * B_perp_ft * (L_cant**2) / 2.0
        Mu_lbin = Mu_ftlb * 12.0

        As_est = Mu_lbin / (0.9 * fy * 0.9 * d_in) if d_in > 0 else 0
        over_reinforced = False
        for _ in range(5):
            a_block = As_est * fy / (0.85 * fc * B_perp_ft * 12.0) if fc > 0 else 0
            if a_block >= d_in:
                over_reinforced = True
                break
            As_new = (
                Mu_lbin / (0.9 * fy * (d_in - a_block / 2.0))
                if (fy > 0 and d_in > a_block / 2.0)
                else 0
            )
            if abs(As_new - As_est) < 0.001:
                break
            As_est = As_new

        As_min_TS = 0.0018 * (B_perp_ft * 12.0) * H
        As_min_beam = 200.0 * (B_perp_ft * 12.0) * d_in / fy if fy > 0 else 0

        As_required = max(As_est, As_min_TS, As_min_beam)
        return {
            "L_cant_ft": round(L_cant, 3),
            "Mu_ftlb": round(Mu_ftlb, 1),
            "As_calc_in2": round(As_est, 4),
            "As_min_TS_in2": round(As_min_TS, 4),
            "As_min_beam_in2": round(As_min_beam, 4),
            "As_required_in2": round(As_required, 4),
            "over_reinforced": over_reinforced,
        }

    flex_B1 = _flexure_dir(B1, B2, a1)
    flex_B2 = _flexure_dir(B2, B1, a2)

    # ============================================================
    # Bar spacing
    # ============================================================
    def _bar_spacing(As_req, B_ft, bar_label):
        if bar_label is None:
            return None
        bar = REBAR.get(bar_label)
        if bar is None:
            return None
        width_in = B_ft * 12.0
        n_bars = max(1, math.ceil(As_req / bar["area"]))
        spacing = (
            (width_in - 2.0 * cover - bar["dia"]) / (n_bars - 1)
            if n_bars > 1
            else width_in
        )
        As_prov = n_bars * bar["area"]
        return {
            "bar": bar_label,
            "spacing_calc_in": round(spacing, 1),
            "n_bars": n_bars,
            "As_provided_in2": round(As_prov, 4),
            "ok": As_prov >= As_req,
        }

    spacing_B1 = (
        _bar_spacing(flex_B1["As_required_in2"], B1, inp.bar_B1 or inp.bar_no_col)
        if inp.bar_B1 or inp.bar_no_col
        else None
    )
    spacing_B2 = (
        _bar_spacing(flex_B2["As_required_in2"], B2, inp.bar_B2 or inp.bar_no_col)
        if inp.bar_B2 or inp.bar_no_col
        else None
    )

    # ============================================================
    # Development length
    # ============================================================
    psi_t = 1.0  # bottom bars
    psi_e = 1.0  # uncoated
    lambda_c = 1.0  # normal weight

    def _dev_length(bar_size, B_ft, spacing_in):
        bd = REBAR.get(bar_size, REBAR["#4"])["dia"]
        sp_half = (spacing_in / 2.0) if spacing_in else cover
        c_val = min(cover, sp_half)
        term = max(min((c_val + 0.0) / bd, 2.5), 0.01)
        ld = (
            (3.0 / 40.0)
            * (fy / math.sqrt(fc))
            * (psi_t * psi_e / (lambda_c * term))
            * bd
        )
        ld = max(ld, 12.0)
        avail = B_ft * 12.0 / 2.0 - cover
        return ld, avail, avail >= ld

    bar_B1_size = inp.bar_B1 or inp.bar_no_col
    bar_B2_size = inp.bar_B2 or inp.bar_no_col
    ld_B1, avail_B1, ok_B1 = _dev_length(bar_B1_size, B1, inp.bar_spacing_B1)
    ld_B2, avail_B2, ok_B2 = _dev_length(bar_B2_size, B2, inp.bar_spacing_B2)
    ld_in = max(ld_B1, ld_B2)
    avail_in = min(avail_B1, avail_B2)
    ld_ok = ok_B1 and ok_B2

    # ============================================================
    # Thickness check
    # ============================================================
    d_governing = max(d_req_oneway, d_req_punch)
    H_req = d_governing + cover + bar_dia / 2.0
    thickness_ok = H >= H_req

    # ============================================================
    # Overall
    # ============================================================
    checks = [soil_ok, oneway_ok, punching_ok, thickness_ok, ld_ok]
    if spacing_B1:
        checks.append(spacing_B1["ok"])
    if spacing_B2:
        checks.append(spacing_B2["ok"])
    if flex_B1.get("over_reinforced") or flex_B2.get("over_reinforced"):
        checks.append(False)
    overall = all(checks)

    return {
        "soil_bearing": {
            "q_allow_psf": round(allowable_psf, 1),
            "P_service_lb": round(P_service, 0),
            "area_req_ft2": round(area_required, 2),
            "area_selected_ft2": round(area_selected, 2),
            "q_max_psf": round(q_max_psf, 1),
            "q_min_psf": round(q_min_psf, 1),
            "q_avg_psf": round(q_avg, 1),
            "eccentricity_ft": round(e_ft, 3),
            "partial_bearing": partial_bearing,
            "utilization": (
                round(q_max_psf / allowable_psf * 100, 1) if allowable_psf > 0 else 0
            ),
            "status": "PASS" if soil_ok else "FAIL",
        },
        "one_way_shear": {
            "Vu_lb": round(Vu_oneway, 1),
            "phi_Vc_lb": round(phi_Vc_oneway, 1),
            "d_req_in": round(d_req_oneway, 2),
            "utilization": (
                round(Vu_oneway / phi_Vc_oneway * 100, 1) if phi_Vc_oneway > 0 else 0
            ),
            "status": "PASS" if oneway_ok else "FAIL",
            "combo": combo_label,
        },
        "punching_shear": {
            "Vu_lb": round(Vu_punch, 1),
            "phi_Vc_lb": round(phi_Vc_punch, 1),
            "bo_in": round(bo_in, 1),
            "beta_c": round(beta_c, 2),
            "Vc1": round(Vc1, 0),
            "Vc2": round(Vc2, 0),
            "Vc3": round(Vc3, 0),
            "utilization": (
                round(Vu_punch / phi_Vc_punch * 100, 1) if phi_Vc_punch > 0 else 0
            ),
            "status": "PASS" if punching_ok else "FAIL",
            "combo": combo_label,
        },
        "flexure_B1": flex_B1,
        "flexure_B2": flex_B2,
        "reinforcement_B1": spacing_B1,
        "reinforcement_B2": spacing_B2,
        "development_length": {
            "ld_req_in": round(ld_in, 1),
            "available_B1_in": round(avail_B1, 1),
            "available_B2_in": round(avail_B2, 1),
            "status": "OK" if ld_ok else "FAIL",
        },
        "thickness": {
            "H_provided_in": H,
            "H_required_in": round(H_req, 1),
            "d_actual_in": round(d_in, 2),
            "status": "PASS" if thickness_ok else "FAIL",
        },
        "summary": {
            "overall": "PASS" if overall else "FAIL",
            "governing_combo": combo_label,
            "As_req_B1_in2": flex_B1["As_required_in2"],
            "As_req_B2_in2": flex_B2["As_required_in2"],
        },
    }


# ============================================================
# Test
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("POST FOOTING DESIGN CALCULATOR")
    print("=" * 60)

    # Case 1: Small concentric footing
    inp1 = PostFootingInputs(
        B1=1.5,
        B2=1.5,
        H=12,
        cover=3,
        depth=3,
        a1=3.5,
        a2=5.5,  # 4x6 post
        DL=2000,
        LL=1000,
        Mu_ftkips=0,
        q_allow_ton=1.0,
        bar_no_col="#4",
    )
    r1 = check_post_footing(inp1)
    SB = r1["soil_bearing"]
    print(f"\nCase 1: Small concentric (1.5x1.5 ft, P={inp1.DL+inp1.LL} lb)")
    print(
        f"  q_allow: {SB['q_allow_psf']} psf | q_max: {SB['q_max_psf']} psf | {SB['status']} ({SB['utilization']}%)"
    )
    print(
        f"  One-way: {r1['one_way_shear']['status']} ({r1['one_way_shear']['utilization']}%)"
    )
    print(
        f"  Punching: {r1['punching_shear']['status']} ({r1['punching_shear']['utilization']}%)"
    )
    print(
        f"  As B1: {r1['flexure_B1']['As_required_in2']} in2 | As B2: {r1['flexure_B2']['As_required_in2']} in2"
    )
    print(f"  Thickness: {r1['thickness']['status']}")
    print(f"  Overall: {r1['summary']['overall']}")

    # Case 2: Medium eccentric footing (matches Sheet2)
    inp2 = PostFootingInputs(
        B1=2.5,
        B2=2.0,
        H=9,
        cover=2,
        depth=3,
        a1=8.0,
        a2=8.0,  # 8x8 column
        DL=7080,
        LL=3540,
        Mu_ftkips=10,
        q_allow_ton=4.0,
        bar_no_col="#4",
        column_location="interior",
    )
    r2 = check_post_footing(inp2)
    SB2 = r2["soil_bearing"]
    print(f"\nCase 2: Eccentric (2.5x2.0 ft, Mu=10 ft-k)")
    print(f"  Eccentricity: {SB2['eccentricity_ft']} ft (kern={inp2.B1/6:.2f})")
    print(f"  Partial bearing: {SB2['partial_bearing']}")
    print(f"  q_max: {SB2['q_max_psf']} psf | q_avg: {SB2['q_avg_psf']} psf")
    print(
        f"  q_allow: {SB2['q_allow_psf']} psf | Soil: {SB2['status']} ({SB2['utilization']}%)"
    )
    print(
        f"  One-way: {r2['one_way_shear']['status']} ({r2['one_way_shear']['utilization']}%)"
    )
    print(
        f"  Punching: {r2['punching_shear']['status']} ({r2['punching_shear']['utilization']}%)"
    )
    print(f"  As B1: {r2['flexure_B1']['As_required_in2']} in2")
    print(f"  As B2: {r2['flexure_B2']['As_required_in2']} in2")
    if r2["reinforcement_B1"]:
        print(
            f"  B1 bars: {r2['reinforcement_B1']['bar']} @ {r2['reinforcement_B1']['spacing_calc_in']}in ({r2['reinforcement_B1']['n_bars']} bars)"
        )
    print(
        f"  Dev length: {r2['development_length']['ld_req_in']} in (avail B1={r2['development_length']['available_B1_in']} in, B2={r2['development_length']['available_B2_in']} in)"
    )
    print(f"  Overall: {r2['summary']['overall']}")

    # Case 3: Large eccentric (higher moment)
    inp3 = PostFootingInputs(
        B1=3.0,
        B2=3.5,
        H=15,
        cover=3,
        depth=3,
        a1=8.0,
        a2=8.0,
        DL=15000,
        LL=8000,
        Mu_ftkips=25,
        q_allow_ton=3.0,
        bar_no_col="#5",
    )
    r3 = check_post_footing(inp3)
    SB3 = r3["soil_bearing"]
    print(f"\nCase 3: Large eccentric (3.0x3.5 ft, Pu=27.6k, Mu=25 ft-k)")
    print(
        f"  Eccentricity: {SB3['eccentricity_ft']} ft | Partial: {SB3['partial_bearing']}"
    )
    print(f"  q_max: {SB3['q_max_psf']} psf | q_min: {SB3['q_min_psf']} psf")
    print(f"  Soil: {SB3['status']} ({SB3['utilization']}%)")
    print(
        f"  Punching: {r3['punching_shear']['status']} ({r3['punching_shear']['utilization']}%)"
    )
    print(f"  Overall: {r3['summary']['overall']}")
