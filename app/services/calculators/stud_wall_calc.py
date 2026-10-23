"""
stud_calc.py
Wall stud calculations for combined axial + lateral wind, including bearing limits and IBC exceptions.
"""
import math

from services.calculators.utils.adjustments import cp_factor, cl_factor, cb_factor
from services.calculators.utils.lumber import get_section_props, get_ref_values, get_CF


def check_stud(name, system, application, size, species, grade, length_ft,
               spacing_in, wind_psf,
               D_plf=0, L_plf=0, Lr_plf=0, S_plf=0,
               support_species="SPF",
               unbraced_weak_ft=1.0,
               ecc_ratio=0.1667,
               ewp_overrides=None,
               plies=1,
               service_conditions=None,
               defl_ratio=360):
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

    # Tributary loads
    wind_plf = wind_psf * (spacing_in / 12)
    axial_D = D_plf * (spacing_in / 12)
    axial_L = L_plf * (spacing_in / 12)
    axial_Lr = Lr_plf * (spacing_in / 12)
    axial_S = S_plf * (spacing_in / 12)

    # Self-weight
    density = refs.get("Density", 35.0)
    self_weight_lbs = density * (props["A"] / 144.0) * length_ft
    axial_D += self_weight_lbs

    # Slenderness per axis
    slen_d = length_in / props["d"] if props["d"] > 0 else 0
    slen_b = (unbraced_weak_ft * 12) / props["b"] if (unbraced_weak_ft > 0 and props["b"] > 0) else 0
    slenderness = max(slen_d, slen_b)

    CF_fc = get_CF(size, "Fc", species, grade)
    CF_fb = get_CF(size, "Fb", species, grade)
    CF_fv = get_CF(size, "Fv", species, grade)
    Cr = 1.15 if spacing_in <= 24 and spacing_in > 0 else 1.0
    E_prime = refs["E"]
    Emin_prime = refs["Emin"]

    # Euler critical buckling stress per axis (NDS 3.7.1)
    FcE_d = (0.822 * Emin_prime) / (slen_d ** 2) if slen_d > 0 else 1e9
    FcE_b = (0.822 * Emin_prime) / (slen_b ** 2) if slen_b > 0 else 1e9

    # CL beam stability factor (NDS 3.3.3) — wind bends about strong axis
    le_bend = 1.63 * length_in + 3 * props["d"]
    CL_strong = cl_factor(le_bend, props["d"], props["b"], Emin_prime,
                          refs["Fb"] * CF_fb)

    # Bearing setup — support plate
    support_refs = get_ref_values(support_species, "No. 2")
    base_Fc_perp_plate = support_refs.get("Fc_p", 425) * cM_Fcp
    Cb_plate = cb_factor(props["b"])
    allowed_Fc_perp_plate = base_Fc_perp_plate * Cb_plate
    bearing_area = props["A"]

    # Bearing setup — stud's own Fc_perp at top plate
    stud_Fc_perp = refs.get("Fc_p", 425)
    Cb_stud = cb_factor(props["b"])
    allowed_Fc_perp_stud = stud_Fc_perp * Cb_stud

    results = {}
    results["Slenderness"] = {
        "Actual": round(slenderness, 1), "Allowed": 50,
        "Result": round((slenderness / 50) * 100, 1), "LDF": "-",
        "Combo": f"le/d={round(slen_d,1)}, le/b={round(slen_b,1)}"
    }

    # ASCE 7-22 ASD load combinations
    CD_D, CD_L, CD_Lr, CD_S, CD_W = 0.90, 1.00, 1.25, 1.15, 1.60
    c_list = [
        {'name': '1.0D',                              'P': 1.0*axial_D,                                   'W': 0,              'CD': CD_D},
        {'name': '1.0D + 1.0L',                       'P': 1.0*axial_D + 1.0*axial_L,                     'W': 0,              'CD': CD_L},
        {'name': '1.0D + 1.0Lr',                      'P': 1.0*axial_D + 1.0*axial_Lr,                    'W': 0,              'CD': CD_Lr},
        {'name': '1.0D + 1.0S',                       'P': 1.0*axial_D + 1.0*axial_S,                     'W': 0,              'CD': CD_S},
        {'name': '1.0D + 0.75L + 0.75Lr',             'P': 1.0*axial_D + 0.75*axial_L + 0.75*axial_Lr,   'W': 0,              'CD': CD_Lr},
        {'name': '1.0D + 0.75L + 0.75S',              'P': 1.0*axial_D + 0.75*axial_L + 0.75*axial_S,    'W': 0,              'CD': CD_S},
        {'name': '1.0D + 0.6W',                       'P': 1.0*axial_D,                                   'W': 0.6*wind_plf,   'CD': CD_W},
        {'name': '1.0D + 0.45W + 0.75L + 0.75Lr',    'P': 1.0*axial_D + 0.75*axial_L + 0.75*axial_Lr,   'W': 0.45*wind_plf,  'CD': CD_W},
        {'name': '1.0D + 0.45W + 0.75L + 0.75S',     'P': 1.0*axial_D + 0.75*axial_L + 0.75*axial_S,    'W': 0.45*wind_plf,  'CD': CD_W},
        {'name': '0.6D + 0.6W',                       'P': 0.6*axial_D,                                   'W': 0.6*wind_plf,   'CD': CD_W},
    ]

    max_comp_util = 0; res_comp = None
    max_mom_util = 0; res_mom = None
    max_comb_util = 0; res_comb = None
    max_brg_util = 0; res_brg = None
    max_shear_util = 0; res_shear = None
    res_lat_react = None

    for combo in c_list:
        P = combo["P"]
        W_plf = combo["W"]

        # Compression buckling (CP from max slenderness)
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

        # Plate bearing — governing of support plate and stud Fc_perp
        allowed_brg_plate = allowed_Fc_perp_plate * bearing_area
        allowed_brg_stud = allowed_Fc_perp_stud * bearing_area
        allowed_brg = min(allowed_brg_plate, allowed_brg_stud)
        util_brg = P / allowed_brg if allowed_brg > 0 else 0
        if util_brg > max_brg_util:
            max_brg_util = util_brg
            res_brg = {"Actual": round(P, 1), "Allowed": round(allowed_brg, 1),
                       "Result": round(util_brg * 100, 1), "LDF": "-",
                       "Combo": combo["name"]}

        # Lateral shear at stud end (d from support)
        lat_react = (W_plf * length_ft) / 2
        if W_plf > 0:
            act_lat_shear = max(0, lat_react - W_plf * (props["d"] / 12.0))
            allowed_Fv = refs["Fv"] * combo["CD"] * CF_fv
            allowed_V = allowed_Fv * props["A"] / 1.5
            util_shear = act_lat_shear / allowed_V if allowed_V > 0 else 0
            if util_shear >= max_shear_util:
                max_shear_util = util_shear
                res_shear = {"Actual": round(act_lat_shear, 1), "Allowed": round(allowed_V, 1),
                             "Result": round(util_shear * 100, 1), "LDF": combo["CD"],
                             "Combo": combo["name"]}
                res_lat_react = {"Actual": round(lat_react, 1),
                                 "Allowed": round(allowed_brg, 1),
                                 "Result": round((lat_react / allowed_brg) * 100, 1) if allowed_brg > 0 else 0,
                                 "LDF": combo["CD"], "Combo": combo["name"]}

        # Total bending (wind on d-face → strong axis)
        lat_M_ft_lbs = (W_plf * (length_ft ** 2)) / 8
        ecc_M_ft_lbs = P * (ecc_ratio * props["d"] / 12.0)
        M_ft_lbs = lat_M_ft_lbs + ecc_M_ft_lbs

        fb = (M_ft_lbs * 12) / props["S"] if props["S"] > 0 else 0
        Fb_star = refs["Fb"] * combo["CD"] * CF_fb * Cr * CL_strong
        allowed_M_ft_lbs = (Fb_star * props["S"]) / 12 if Fb_star > 0 else 1

        util_mom = M_ft_lbs / allowed_M_ft_lbs if allowed_M_ft_lbs > 0 else 0
        if util_mom > max_mom_util:
            max_mom_util = util_mom
            res_mom = {"Actual": round(M_ft_lbs, 1), "Allowed": round(allowed_M_ft_lbs, 1),
                       "Result": round(util_mom * 100, 1), "LDF": combo["CD"],
                       "Combo": combo["name"]}

        # NDS 3.9-3 interaction (strong-axis FcE for amplification)
        fc = P / props["A"] if props["A"] > 0 else 0

        if fc >= FcE_d:
            util_interact = (fc / FcE_d) + 1.0
        elif allowed_Fc <= 0:
            util_interact = 999 if P > 0 else 0
        else:
            term1 = (fc / allowed_Fc) ** 2
            amp1 = 1.0 - fc / FcE_d
            term2 = fb / (Fb_star * amp1) if (fb > 0 and amp1 > 0 and Fb_star > 0) else 0
            util_interact = term1 + term2

        if util_interact > max_comb_util:
            max_comb_util = util_interact
            res_comb = {"Actual": round(util_interact, 3), "Allowed": 1.0,
                        "Result": round(util_interact * 100, 1), "LDF": combo["CD"],
                        "Combo": combo["name"]}

    results["Axial Compression (lbs)"] = res_comp
    results["Plate Bearing (lbs)"] = res_brg
    if res_lat_react:
        results["Lateral Reaction (lbs)"] = res_lat_react
        results["Lateral Shear (lbs)"] = res_shear
    results["Lateral Moment (ft-lbs)"] = res_mom
    results["Bending/Compression"] = res_comb

    # Deflection — 0.42 factor converts C&C wind to service level per IBC Table 1604.3 footnote f
    max_W = wind_plf * 0.42
    defl = (5 * (max_W / 12) * length_in ** 4) / (384 * E_prime * props["I"]) if max_W > 0 else 0
    allowed_defl = length_in / defl_ratio
    results["Total Deflection (in)"] = {
        "Actual": round(defl, 3), "Allowed": round(allowed_defl, 3),
        "Result": round((defl / allowed_defl) * 100, 1) if allowed_defl > 0 else 0,
        "LDF": "-", "Combo": f"0.42W (L/{defl_ratio})"
    }

    return {
        "name": name, "system": system, "application": application,
        "size": size, "plies": plies, "results": results,
        "intermediate": {
            "Fc_ref_psi": round(refs["Fc"], 1),
            "Fb_ref_psi": round(refs["Fb"], 1),
            "Emin_psi": round(Emin_prime, 0),
            "CP": round(CP, 4),
            "CL_strong": round(CL_strong, 4),
            "Cr": Cr,
            "FcE_d_psi": round(FcE_d, 1),
            "FcE_b_psi": round(FcE_b, 1),
            "le_d": round(slen_d, 1),
            "le_b": round(slen_b, 1),
            "c_buckling": c_buck,
        }
    }