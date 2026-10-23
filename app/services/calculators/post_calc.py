"""
post_calc.py
Column calculator: slenderness, NDS 3.9-3 biaxial interaction,
lateral shear, and plate bearing per NDS / ASCE 7-22.
"""
import math

from services.calculators.utils.adjustments import cp_factor, cl_factor, cb_factor
from services.calculators.utils.lumber import get_section_props, get_ref_values, get_CF

def check_post(name, system, application, size, species, grade, length_ft,
               D_lbs=0, L_lbs=0, Lr_lbs=0, S_lbs=0,
               support_species="SPF",
               lat_wind_psf=0, lat_trib_ft=0,
               ecc_ratio=0.1667,
               ewp_overrides=None,
               plies=1,
               wind_on_d=True,
               Ke_d=1.0, Ke_b=1.0, lu_b_ft=None,
               service_conditions=None,
               bearing_length_in=None):
    props = get_section_props(size, plies)
    refs = dict(get_ref_values(species, grade))

    if ewp_overrides:
        if ewp_overrides.get("Fb_psi")      is not None: refs["Fb"]    = ewp_overrides["Fb_psi"]
        if ewp_overrides.get("Fv_psi")      is not None: refs["Fv"]    = ewp_overrides["Fv_psi"]
        if ewp_overrides.get("Fc_perp_psi") is not None: refs["Fc_p"]  = ewp_overrides["Fc_perp_psi"]
        if ewp_overrides.get("E_psi")       is not None: refs["E"]     = ewp_overrides["E_psi"]
        if ewp_overrides.get("E_min_psi")   is not None: refs["Emin"]  = ewp_overrides["E_min_psi"]

    is_ewp = any(x in species for x in ["LSL", "LVL", "PSL", "EWP", "Glulam"])
    c_buck = 0.9 if is_ewp else 0.8

    # Service condition adjustments (CM, Ct, Ci per NDS Table 4.3.1)
    cM_Fb = cM_Fv = cM_Fc = cM_E = cM_Fcp = 1.0
    if service_conditions:
        if service_conditions.get("moisture_condition") == "wet":
            cM_Fb *= 0.85; cM_Fv *= 0.97; cM_Fc *= 0.8; cM_E *= 0.9; cM_Fcp *= 0.67
        t = service_conditions.get("temperature_range", "lte_100F")
        if t == "100_125F":
            cM_Fb *= 0.8; cM_Fv *= 0.8; cM_Fc *= 0.8; cM_E *= 0.9; cM_Fcp *= 0.8
        elif t == "125_150F":
            cM_Fb *= 0.7; cM_Fv *= 0.7; cM_Fc *= 0.7; cM_E *= 0.9; cM_Fcp *= 0.7
        if service_conditions.get("incised"):
            cM_Fb *= 0.8; cM_Fv *= 0.8; cM_Fc *= 0.8; cM_E *= 0.95

    refs["Fb"] *= cM_Fb
    refs["Fv"] *= cM_Fv
    refs["Fc"] *= cM_Fc
    refs["E"]  *= cM_E
    refs["Emin"] *= cM_E
    if "Fc_p" in refs:
        refs["Fc_p"] *= cM_Fcp

    length_in = length_ft * 12

    # Self-weight
    density = refs.get("Density", 35.0)
    self_weight_lbs = density * (props["A"] / 144.0) * length_ft
    D_lbs += self_weight_lbs

    # Slenderness per axis (Ke × le / d)
    lu_b_eff = lu_b_ft if lu_b_ft is not None else length_ft
    le_d_in = Ke_d * length_in
    le_b_in = Ke_b * lu_b_eff * 12
    slen_d = le_d_in / props["d"] if props["d"] > 0 else 0
    slen_b = le_b_in / props["b"] if props["b"] > 0 else 0
    slenderness = max(slen_d, slen_b)

    CF_fc = get_CF(size, "Fc", species, grade)
    CF_fb = get_CF(size, "Fb", species, grade)
    CF_fv = get_CF(size, "Fv", species, grade)
    Emin_prime = refs["Emin"]

    # Euler critical buckling stress per axis (NDS 3.7.1)
    FcE_d = (0.822 * Emin_prime) / (slen_d ** 2) if slen_d > 0 else 1e9
    FcE_b = (0.822 * Emin_prime) / (slen_b ** 2) if slen_b > 0 else 1e9

    # Section moduli for both axes
    S_strong = props["S"]
    S_weak = (props["d"] * props["b"] ** 2) / 6.0

    # CL beam stability factor for strong-axis bending (NDS 3.3.3)
    le_bend = 1.63 * length_in + 3 * props["d"]
    CL_strong = cl_factor(le_bend, props["d"], props["b"], Emin_prime,
                          refs["Fb"] * CF_fb)

    # Wind direction: which face receives pressure, which section modulus
    if wind_on_d:
        S_wind = S_strong
        d_shear = props["d"]
    else:
        S_wind = S_weak
        d_shear = props["b"]

    # Bearing
    support_refs = get_ref_values(support_species, "No. 2")
    allowed_Fc_perp = support_refs.get("Fc_p", 425) * cM_Fcp
    if bearing_length_in is not None:
        allowed_Fc_perp *= cb_factor(bearing_length_in)
    bearing_area = props["A"]

    results = {}
    results["Slenderness"] = {
        "Actual": round(slenderness, 1), "Allowed": 50,
        "Result": round((slenderness / 50) * 100, 1), "LDF": "-",
        "Combo": f"le/d={round(slen_d,1)}, le/b={round(slen_b,1)}"
    }

    # ASCE 7-22 ASD load combinations
    CD_D, CD_L, CD_Lr, CD_S, CD_W = 0.90, 1.00, 1.25, 1.15, 1.60
    w_wind_plf = lat_wind_psf * lat_trib_ft

    c_list = [
        {'name': '1.0D',                          'P': 1.0*D_lbs,                             'CD': CD_D,  'w_fact': 0},
        {'name': '1.0D + 1.0L',                   'P': 1.0*D_lbs + 1.0*L_lbs,                 'CD': CD_L,  'w_fact': 0},
        {'name': '1.0D + 1.0Lr',                  'P': 1.0*D_lbs + 1.0*Lr_lbs,                'CD': CD_Lr, 'w_fact': 0},
        {'name': '1.0D + 1.0S',                   'P': 1.0*D_lbs + 1.0*S_lbs,                 'CD': CD_S,  'w_fact': 0},
        {'name': '1.0D + 0.75L + 0.75Lr',         'P': 1.0*D_lbs + 0.75*L_lbs + 0.75*Lr_lbs, 'CD': CD_Lr, 'w_fact': 0},
        {'name': '1.0D + 0.75L + 0.75S',          'P': 1.0*D_lbs + 0.75*L_lbs + 0.75*S_lbs,  'CD': CD_S,  'w_fact': 0},
        {'name': '1.0D + 0.6W',                   'P': 1.0*D_lbs,                             'CD': CD_W,  'w_fact': 0.6},
        {'name': '1.0D + 0.45W + 0.75L + 0.75Lr', 'P': 1.0*D_lbs + 0.75*L_lbs + 0.75*Lr_lbs,'CD': CD_W,  'w_fact': 0.45},
        {'name': '1.0D + 0.45W + 0.75L + 0.75S',  'P': 1.0*D_lbs + 0.75*L_lbs + 0.75*S_lbs, 'CD': CD_W,  'w_fact': 0.45},
        {'name': '0.6D + 0.6W',                   'P': 0.6*D_lbs,                             'CD': CD_W,  'w_fact': 0.6},
    ]

    max_comp_util = 0; res_comp = None
    max_brg_util = 0; res_brg = None
    max_shear_util = 0; res_shear = None
    max_interact_util = 0; res_interact = None
    max_moment_util = 0; res_moment = None
    res_lat_react = None
    gov_CP = 0; gov_Fc_star = 0

    for combo in c_list:
        P = combo["P"]
        W_multi = combo["w_fact"]

        # Column stability (CP from max slenderness — correct for pure axial)
        Fc_star = refs["Fc"] * combo["CD"] * CF_fc
        CP = cp_factor(slenderness, Emin_prime, Fc_star, c=c_buck)
        allowed_Fc = Fc_star * CP
        allowed_P = allowed_Fc * props["A"]

        util_comp = P / allowed_P if allowed_P > 0 else 0
        if util_comp > max_comp_util:
            max_comp_util = util_comp
            res_comp = {"Actual": round(P, 1), "Allowed": round(allowed_P, 1),
                        "Result": round(util_comp * 100, 1), "LDF": combo["CD"],
                        "Combo": combo["name"]}
            gov_CP = CP; gov_Fc_star = Fc_star

        # Plate bearing
        allowed_brg = allowed_Fc_perp * bearing_area
        util_brg = P / allowed_brg if allowed_brg > 0 else 0
        if util_brg > max_brg_util:
            max_brg_util = util_brg
            res_brg = {"Actual": round(P, 1), "Allowed": round(allowed_brg, 1),
                       "Result": round(util_brg * 100, 1), "LDF": "-",
                       "Combo": combo["name"]}

        # --- Moments ---
        wind_plf = w_wind_plf * W_multi
        M_wind = (wind_plf * length_ft ** 2) / 8.0
        M_ecc = P * (ecc_ratio * props["d"] / 12.0)

        # Assign to strong / weak axes based on wind direction
        if wind_on_d:
            M_strong = M_wind + M_ecc
            M_weak_ax = 0.0
        else:
            M_strong = M_ecc
            M_weak_ax = M_wind

        fb1 = (M_strong * 12) / S_strong if S_strong > 0 else 0
        fb2 = (M_weak_ax * 12) / S_weak if S_weak > 0 else 0

        # Adjusted Fb' per axis (CL only < 1 for strong axis when d > b)
        Fb_star1 = refs["Fb"] * combo["CD"] * CF_fb * CL_strong
        Fb_star2 = refs["Fb"] * combo["CD"] * CF_fb

        # --- Lateral shear (from wind) ---
        if W_multi > 0:
            lat_react = (wind_plf * length_ft) / 2.0
            act_shear = max(0, lat_react - wind_plf * (d_shear / 12.0))
            allowed_Fv = refs["Fv"] * combo["CD"] * CF_fv
            allowed_V = allowed_Fv * props["A"] / 1.5
            util_shear = act_shear / allowed_V if allowed_V > 0 else 0
            if util_shear >= max_shear_util:
                max_shear_util = util_shear
                res_shear = {"Actual": round(act_shear, 1), "Allowed": round(allowed_V, 1),
                             "Result": round(util_shear * 100, 1), "LDF": combo["CD"],
                             "Combo": combo["name"]}
                res_lat_react = {"Actual": round(lat_react, 1), "Allowed": round(allowed_brg, 1),
                                 "Result": round((lat_react / allowed_brg) * 100, 1) if allowed_brg > 0 else 0,
                                 "LDF": combo["CD"], "Combo": combo["name"]}

        # --- NDS 3.9-3 Interaction ---
        fc = P / props["A"] if props["A"] > 0 else 0

        if fc >= min(FcE_d, FcE_b):
            util_interact = max(fc / FcE_d, fc / FcE_b) + 1.0
        elif allowed_Fc <= 0:
            util_interact = 999 if P > 0 else 0
        else:
            term1 = (fc / allowed_Fc) ** 2

            amp1 = 1.0 - fc / FcE_d
            term2 = fb1 / (Fb_star1 * amp1) if (fb1 > 0 and amp1 > 0 and Fb_star1 > 0) else 0

            if fb2 > 0 and Fb_star2 > 0:
                RB = math.sqrt(le_bend * props["d"] / (props["b"] ** 2)) if props["b"] > 0 else 50
                FbE = 1.20 * Emin_prime / (RB ** 2) if RB > 0 else 0
                fb1_FbE_sq = (fb1 / FbE) ** 2 if FbE > 0 else 0
                amp2 = 1.0 - fc / FcE_b - fb1_FbE_sq
                term3 = fb2 / (Fb_star2 * amp2) if amp2 > 0 else 999
            else:
                term3 = 0

            util_interact = term1 + term2 + term3

        if util_interact > max_interact_util:
            max_interact_util = util_interact
            res_interact = {"Actual": round(util_interact, 3), "Allowed": 1.0,
                            "Result": round(util_interact * 100, 1), "LDF": combo["CD"],
                            "Combo": combo["name"]}

        # Track moment
        total_M = M_strong + M_weak_ax
        allowed_M = (Fb_star1 * S_strong) / 12.0 if (Fb_star1 > 0 and S_strong > 0) else 1
        util_M = total_M / allowed_M if allowed_M > 0 else 0
        if util_M > max_moment_util:
            max_moment_util = util_M
            res_moment = {"Actual": round(total_M, 1), "Allowed": round(allowed_M, 1),
                          "Result": round(util_M * 100, 1), "LDF": combo["CD"],
                          "Combo": combo["name"]}

    results["Axial Compression (lbs)"] = res_comp
    results["Plate Bearing (lbs)"] = res_brg
    if res_lat_react:
        results["Lateral Reaction (lbs)"] = res_lat_react
        results["Lateral Shear (lbs)"] = res_shear
    if res_moment:
        results["Lateral Moment (ft-lbs)"] = res_moment
    if res_interact:
        results["Bending/Compression"] = res_interact

    return {
        "name": name, "system": system, "application": application,
        "size": size, "plies": plies, "results": results,
        "intermediate": {
            "Fc_ref_psi": round(refs["Fc"], 1),
            "Fb_ref_psi": round(refs["Fb"], 1),
            "Emin_psi": round(Emin_prime, 0),
            "CP": round(gov_CP, 4),
            "Fc_star_psi": round(gov_Fc_star, 1),
            "CL_strong": round(CL_strong, 4),
            "FcE_d_psi": round(FcE_d, 1),
            "FcE_b_psi": round(FcE_b, 1),
            "le_d_ratio": round(slen_d, 1),
            "le_b_ratio": round(slen_b, 1),
            "c_buckling": c_buck,
        }
    }