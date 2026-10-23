"""
footing_calc.py
Analysis and design of concrete footings (Isolated Post and Continuous Wall).
Includes ASD soil bearing and LRFD concrete/rebar checks.
"""

## Continuous_strip footing

import math
from services.calculators.utils.rebar_data import get_rebar_area, get_rebar_diam

def check_footing(name, footing_type, size_x_ft, size_y_ft, thickness_in, axial_D_kips, axial_L_kips=0, axial_S_kips=0, q_allow_psf=2000, f_prime_c=3000, rebar_size="#4", rebar_spacing_in=12, cover_in=3.0, post_size_in=5.5, post_y_in=None, wall_width_in=6.0, long_bar_size="#4", n_long_bars=2):
    """
    Check structural integrity of a concrete footing.
    - Isolated: size_x x size_y footing under post_size_in x post_y_in support.
    - Continuous: size_x wide strip footing under a wall_width_in wide wall.
      For continuous footings axial loads are per linear foot of wall.
    """
    D = axial_D_kips * 1000
    L = axial_L_kips * 1000
    S = axial_S_kips * 1000
    fy = 60000

    is_continuous = footing_type.lower() == "continuous"

    if is_continuous:
        area_sqft = size_x_ft * 1.0
        width_in = size_x_ft * 12
        length_in = 12.0
        sup_x = wall_width_in
        sup_y = wall_width_in
    else:
        area_sqft = size_x_ft * size_y_ft
        width_in = size_x_ft * 12
        length_in = size_y_ft * 12
        sup_x = post_size_in
        sup_y = post_y_in if post_y_in is not None else post_size_in

    self_weight = (area_sqft * (thickness_in / 12.0)) * 150.0

    # 1. ASD Soil Bearing
    total_load_asd = D + L + S + self_weight
    q_applied = total_load_asd / area_sqft if area_sqft > 0 else 0

    results = {}
    results["Soil Bearing (psf)"] = {
        "Actual": round(q_applied, 1),
        "Allowed": q_allow_psf,
        "Result": (q_applied / q_allow_psf) * 100,
        "LDF": "-",
        "Combo": "D+L+S+Self"
    }

    # 2. Governing LRFD combination (net — self-weight cancels)
    lrfd_combos = [
        ("1.4D", 1.4 * D),
        ("1.2D+1.6L+0.5S", 1.2 * D + 1.6 * L + 0.5 * S),
        ("1.2D+1.6S+1.0L", 1.2 * D + 1.6 * S + 1.0 * L),
    ]
    governing = max(lrfd_combos, key=lambda c: c[1])
    Pu = governing[1]
    combo_label = governing[0]
    qu = Pu / area_sqft if area_sqft > 0 else 0

    # 3. Effective depth
    db = get_rebar_diam(rebar_size)
    d_bot = thickness_in - cover_in - (db / 2.0)
    if d_bot <= 0:
        d_bot = 1.0
    d_top = max(d_bot - db, 1.0)

    phi_v = 0.75
    phi_f = 0.9

    # 4. One-Way Shear (both directions for isolated, governing reported)
    def _one_way_shear(proj_in, section_width_in, d_eff):
        crit = max(proj_in - d_eff, 0)
        Vu = (qu / 144.0) * crit * section_width_in
        Vc = phi_v * 2 * math.sqrt(f_prime_c) * section_width_in * d_eff
        return Vu, Vc

    proj_x = (width_in - sup_x) / 2.0
    Vu_x, Vc_x = _one_way_shear(proj_x, length_in, d_bot)

    if is_continuous:
        Vu1, Vc1 = Vu_x, Vc_x
    else:
        proj_y = (length_in - sup_y) / 2.0
        Vu_y, Vc_y = _one_way_shear(proj_y, width_in, d_top)
        ratio_x = Vu_x / Vc_x if Vc_x > 0 else 0
        ratio_y = Vu_y / Vc_y if Vc_y > 0 else 0
        if ratio_x >= ratio_y:
            Vu1, Vc1 = Vu_x, Vc_x
        else:
            Vu1, Vc1 = Vu_y, Vc_y

    results["One-way Shear (lbs)"] = {
        "Actual": round(Vu1, 1),
        "Allowed": round(Vc1, 1),
        "Result": (Vu1 / Vc1) * 100 if Vc1 > 0 else 0,
        "LDF": "-",
        "Combo": combo_label
    }

    # 5. Two-Way (Punching) Shear — isolated only, ACI 318 §22.6.5.2
    if not is_continuous:
        d_avg = (d_bot + d_top) / 2.0
        punch_x = sup_x + d_avg
        punch_y = sup_y + d_avg
        bo = 2 * (punch_x + punch_y)
        Area_punch = punch_x * punch_y
        outside_sf = max(area_sqft - Area_punch / 144.0, 0.0)
        Vu2 = qu * outside_sf

        beta_c = max(sup_x, sup_y) / min(sup_x, sup_y) if min(sup_x, sup_y) > 0 else 1.0
        alpha_s = 40
        Vc_a = 4 * math.sqrt(f_prime_c) * bo * d_avg
        Vc_b = (2 + 4 / beta_c) * math.sqrt(f_prime_c) * bo * d_avg
        Vc_c = (2 + alpha_s * d_avg / bo) * math.sqrt(f_prime_c) * bo * d_avg
        Vc2 = phi_v * min(Vc_a, Vc_b, Vc_c)

        punch_note = combo_label
        if outside_sf == 0:
            punch_note += " (punching perimeter exceeds footing — review sizing)"

        results["Punching Shear (lbs)"] = {
            "Actual": round(Vu2, 1),
            "Allowed": round(Vc2, 1),
            "Result": (Vu2 / Vc2) * 100 if Vc2 > 0 else 0,
            "LDF": "-",
            "Combo": punch_note
        }

    # 6. Flexure (both directions for isolated, governing reported)
    def _flexure(proj_in, section_width_in, d_eff):
        Mu = (qu / 144.0) * (proj_in ** 2 / 2.0) * section_width_in
        As_prov = (get_rebar_area(rebar_size) * (12.0 / rebar_spacing_in)) * (section_width_in / 12.0)
        a = (As_prov * fy) / (0.85 * f_prime_c * section_width_in)
        if a >= d_eff:
            phiMn = 0.0
        else:
            et = 0.003 * (d_eff - a) / a if a > 0 else 999
            phi_flex = min(max(0.65 + 0.25 * (et - 0.002) / 0.003, 0.65), 0.9) if a > 0 else 0.9
            Mn = As_prov * fy * (d_eff - a / 2.0)
            phiMn = phi_flex * Mn
        As_min = 0.0018 * section_width_in * thickness_in
        return Mu, phiMn, As_prov, As_min

    Mu_x, phiMn_x, As_x, Asmin_x = _flexure(proj_x, length_in, d_bot)

    if is_continuous:
        Mu, phiMn, As_prov, As_min = Mu_x, phiMn_x, As_x, Asmin_x
    else:
        Mu_y, phiMn_y, As_y, Asmin_y = _flexure(proj_y, width_in, d_top)
        ratio_fx = Mu_x / phiMn_x if phiMn_x > 0 else 0
        ratio_fy = Mu_y / phiMn_y if phiMn_y > 0 else 0
        if ratio_fx >= ratio_fy:
            Mu, phiMn, As_prov, As_min = Mu_x, phiMn_x, As_x, Asmin_x
        else:
            Mu, phiMn, As_prov, As_min = Mu_y, phiMn_y, As_y, Asmin_y

    results["Flexural Capacity (in-lbs)"] = {
        "Actual": round(Mu, 1),
        "Allowed": round(phiMn, 1),
        "Result": (Mu / phiMn) * 100 if phiMn > 0 else 0,
        "LDF": "-",
        "Combo": combo_label
    }

    # 7. Minimum reinforcement as separate check
    results["Min Reinforcement (in2)"] = {
        "Actual": round(As_prov, 3),
        "Allowed": round(As_min, 3),
        "Result": (As_min / As_prov) * 100 if As_prov > 0 else 999,
        "LDF": "-",
        "Combo": "ACI 7.6.1 T&S"
    }

    # 8. Longitudinal (along-wall) reinforcement — continuous footings only.
    # No primary flexure under uniform wall load; steel is T&S on the gross
    # section (0.0018*b*h) and bridges non-uniform soil support. IRC R403.1.3
    # additionally requires 1 top + 1 bottom continuous bar under braced
    # wall lines in seismic areas.
    if is_continuous:
        As_long_prov = get_rebar_area(long_bar_size) * n_long_bars
        As_long_min = 0.0018 * width_in * thickness_in
        results["Longitudinal Reinforcement (in2)"] = {
            "Actual": round(As_long_prov, 3),
            "Allowed": round(As_long_min, 3),
            "Result": (As_long_min / As_long_prov) * 100 if As_long_prov > 0 else 999,
            "LDF": "-",
            "Combo": "ACI T&S 0.0018*Ag",
        }

    rebar_str = f'{rebar_size} @ {rebar_spacing_in:g}"'
    if is_continuous:
        size_str = f"{size_x_ft} ft wide x {thickness_in} in thick, {rebar_str}"
    else:
        size_str = f"{size_x_ft}x{size_y_ft} ft x {thickness_in} in thick, {rebar_str}"

    return {
        "name": name,
        "system": "Foundation",
        "application": f"{footing_type} Footing",
        "size": size_str,
        "results": results,
        "plies": 1,
        # Derivation data for calculation-sheet reports
        "details": {
            "rebar_size": rebar_size, "rebar_spacing_in": rebar_spacing_in,
            "As_prov_in2": round(As_prov, 3), "As_min_in2": round(As_min, 3),
            "d_in": round(d_bot, 2), "cover_in": cover_in,
            "fc_psi": f_prime_c, "fy_psi": fy,
            "qu_psf": round(qu, 1), "governing_combo": combo_label,
            "projection_in": round(proj_x, 2),
            "shear_crit_in": round(max(proj_x - d_bot, 0), 2),
            "thickness_in": thickness_in,
            "width_in": round(width_in, 1),
            "long_bar_size": long_bar_size if is_continuous else None,
            "n_long_bars": n_long_bars if is_continuous else None,
        },
    }
