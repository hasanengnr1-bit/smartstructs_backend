"""
horizontal_calc.py
Unified horizontal member calculator handling Joists, Drop/Flush Beams, rafters, Ridge and Headers.
Now supports Left and Right Cantilever Overhangs.
"""

from services.calculators.utils.lumber import get_section_props, get_ref_values, get_CF
from services.calculators.utils.load_combos import get_load_combinations

import math

def _service_factors(sc):
    """Return (cM_Fb, cM_Fv, cM_E, cM_Fc_p) NDS adjustment factors for wet/temp/incised.
    sc is a dict like {'moisture_condition','temperature_range','incised',...}.
    Defaults (dry / lte_100F / not incised) -> all 1.0, identical to prior behaviour.
    Conservative simple form per NDS Table 4.3.1 / 2.3.3."""
    if not sc:
        return 1.0, 1.0, 1.0, 1.0
    cFb = cFv = cE = cFcp = 1.0
    if sc.get("moisture_condition") == "wet":
        cFb *= 0.85; cFv *= 0.97; cE *= 0.9; cFcp *= 0.67
    t = sc.get("temperature_range", "lte_100F")
    if t == "100_125F":
        cFb *= 0.8; cFv *= 0.8; cE *= 0.9; cFcp *= 0.8
    elif t == "125_150F":
        cFb *= 0.7; cFv *= 0.7; cE *= 0.9; cFcp *= 0.7
    if sc.get("incised"):
        cFb *= 0.8; cFv *= 0.8; cE *= 0.95
        # Ci for Fc_perp = 1.0
    return cFb, cFv, cE, cFcp


def check_horizontal(name, system, application, size, species, grade, plies, clear_span_ft, spacing_in, trib_width_ft, D_psf=0, L_psf=0, Lr_psf=0, S_psf=0, support_species="SPF", bearing_area_length_in=3.50, L_ol_ft=0.0, L_or_ft=0.0, pitch=0.0,
                    defl_LL_denom=None, defl_TL_denom=None, service_conditions=None, ewp_overrides=None,
                    bearing_R1_in=None, bearing_R2_in=None,
                    apply_pitch_defl_correction=True):
    props = get_section_props(size, plies)
    refs = dict(get_ref_values(species, grade))  # copy so we can apply overrides
    # EWP property overrides (if plans called out an engineered wood product with explicit values)
    if ewp_overrides:
        if ewp_overrides.get("Fb_psi")     is not None: refs["Fb"]    = ewp_overrides["Fb_psi"]
        if ewp_overrides.get("Fv_psi")     is not None: refs["Fv"]    = ewp_overrides["Fv_psi"]
        if ewp_overrides.get("Fc_perp_psi")is not None: refs["Fc_p"]  = ewp_overrides["Fc_perp_psi"]
        if ewp_overrides.get("E_psi")      is not None: refs["E"]     = ewp_overrides["E_psi"]
        if ewp_overrides.get("E_min_psi")  is not None: refs["Emin"]  = ewp_overrides["E_min_psi"]
    # NDS service-condition factors
    cM_Fb, cM_Fv, cM_E, cM_Fcp = _service_factors(service_conditions)
    refs["Fb"] *= cM_Fb
    refs["Fv"] *= cM_Fv
    refs["E"]  *= cM_E
    if "Emin" in refs: refs["Emin"] *= cM_E
    
    # Per-side bearing lengths. If callers don't pass split values, both sides
    # use bearing_area_length_in (preserves prior single-value behaviour).
    # Resolved here (before geometry) so the design span and the bearing-
    # capacity check reference the SAME bearing lengths and cannot diverge.
    brg_R1 = bearing_R1_in if bearing_R1_in else bearing_area_length_in
    brg_R2 = bearing_R2_in if bearing_R2_in else bearing_area_length_in

    # 1. Geometry
    # Design span = clear span + half of each bearing length (center-to-center
    # of bearings, NDS convention). Half at each end — not the full bearing.
    L_clear = clear_span_ft
    L_span = L_clear + (brg_R1 / 2.0 + brg_R2 / 2.0) / 12

    L_total = L_ol_ft + L_span + L_or_ft
    
    span_in = L_span * 12
    trib = trib_width_ft if trib_width_ft > 0 else (spacing_in / 12)
    
    # Trigonometry for Sloped Members
    theta = math.atan(pitch / 12.0)
    cos_theta = math.cos(theta)
    
    # 2. Applying Loads
    w_D = D_psf * trib
    w_L = L_psf * trib
    w_Lr = Lr_psf * trib
    w_S = S_psf * trib
    
    density = refs.get("Density", 35.0)
    # Self weight acts along the (sloped) member length, so the per-horizontal-
    # foot contribution is scaled by 1/cosθ. Report the effective value actually
    # added to w_D — otherwise the printed "D·trib + w_self" won't reconcile for
    # pitched members.
    self_weight_flat = density * (props["A"] / 144.0)
    self_weight_plf = self_weight_flat / cos_theta if cos_theta > 0 else self_weight_flat
    w_D += self_weight_plf
    
    combos = get_load_combinations(D=w_D, L=w_L, Lr=w_Lr, S=w_S)
    
    # 3. Form Factor adjustments
    is_ewp = any(x in species for x in ["LSL", "LVL", "PSL", "EWP", "Glulam"])
    
    E_prime = refs["E"]
    Cr = 1.0
    if application.lower() in ["joist", "rafter"] and plies == 1 and spacing_in <= 24 and spacing_in > 0 and not is_ewp:
        Cr = 1.15
        
    if is_ewp:
        # Volume factor Cv per product — ESR-1387 note 7 / ANSI 117:
        #   LVL (12/d)^0.136 <= 1.18, PSL (12/d)^0.111 <= 1.15,
        #   LSL (12/d)^0.092 <= 1.12, glulam depth term (12/d)^0.10 <= 1.0
        if "LVL" in species:
            CF_fb = min((12.0 / props["d"]) ** 0.136, 1.18)
        elif "PSL" in species:
            CF_fb = min((12.0 / props["d"]) ** 0.111, 1.15)
            if props["b"] < 2.69:
                CF_fb *= 0.93  # ESR-1387 note 8: thin PSL billets
        elif "Glulam" in species:
            CF_fb = min((12.0 / props["d"]) ** 0.10, 1.0)
        else:  # LSL
            CF_fb = min((12.0 / props["d"]) ** 0.092, 1.12)
        CF_fv = 1.0
    else:
        CF_fb = get_CF(size, "Fb", species, grade)
        CF_fv = get_CF(size, "Fv", species, grade)
    CL_val = 1.0 
    
    if system.lower() == "floor":
        LL_denom, TL_denom = 360, 240
    elif system.lower() == "roof":
        LL_denom, TL_denom = 240, 180
    elif system.lower() == "wall":
        LL_denom, TL_denom = 360, 240
    elif system.lower() == "ceiling":
        # IBC Table 1604.3: plaster ceiling L/360, others L/240
        LL_denom, TL_denom = 360, 240
    else:
        LL_denom, TL_denom = 240, 240
    # Schema overrides (DeflectionCriteria)
    if defl_LL_denom: LL_denom = defl_LL_denom
    if defl_TL_denom: TL_denom = defl_TL_denom
        
    allowed_TL_defl = span_in / TL_denom
    allowed_LL_defl = span_in / LL_denom
    
    results = {}
    
    # --- Reaction & Statics Solver ---
    support_refs = get_ref_values(support_species, "No. 2")
    allowed_Fc_perp = support_refs.get("Fc_p", 425) * cM_Fcp
    # brg_R1 / brg_R2 resolved above (shared with the design-span calc).
    allowed_R1 = allowed_Fc_perp * (props["b"] * brg_R1)
    allowed_R2 = allowed_Fc_perp * (props["b"] * brg_R2)
    # Backward-compat: kept for any code reading `allowed_R` (none in this file).
    allowed_R = min(allowed_R1, allowed_R2)
    bearing_area = props["b"] * min(brg_R1, brg_R2)
    
    max_reaction_util = 0
    res_reaction = None
    
    max_shear_util = 0
    res_shear = None
    
    max_moment_util = 0
    res_moment = None
    
    allowed_Fb = 0
    
    for combo in combos:
        w_plf = combo["load"]
        
        # Statics
        total_load = w_plf * L_total
        center_of_load = L_total / 2.0
        
        # Taking moments about R2 (located at x = L_ol_ft + L_span)
        # R1 is at x = L_ol_ft
        # Distance from load center to R2: (L_ol_ft + L_span) - (L_total / 2)
        dist_to_R2 = (L_ol_ft + L_span) - center_of_load
        
        R1_lbs = (total_load * dist_to_R2) / L_span if L_span > 0 else total_load / 2
        R2_lbs = total_load - R1_lbs
        
        # Each support is checked against its own bearing length. The governing
        # side (worst utilization) is reported.
        util_R1 = R1_lbs / allowed_R1 if allowed_R1 > 0 else 0
        util_R2 = R2_lbs / allowed_R2 if allowed_R2 > 0 else 0
        if util_R1 >= util_R2:
            util_R, governing_R, governing_allowed, side = util_R1, R1_lbs, allowed_R1, "R1"
        else:
            util_R, governing_R, governing_allowed, side = util_R2, R2_lbs, allowed_R2, "R2"
        if util_R > max_reaction_util:
            max_reaction_util = util_R
            res_reaction = {"Actual": governing_R, "Allowed": governing_allowed, "Result": util_R*100, "LDF": "-", "Combo": f'{combo["name"]} @ {side}'}
            
        # Shear
        # Check absolute peak shears around the supports
        V_R1_left = w_plf * L_ol_ft
        V_R1_right = abs(R1_lbs - V_R1_left)
        V_R2_right = w_plf * L_or_ft
        V_R2_left = abs(R2_lbs - V_R2_right)
        
        max_V = max(V_R1_left, V_R1_right, V_R2_left, V_R2_right)
        d_ft = props["d"] / 12.0
        if L_ol_ft == 0 and L_or_ft == 0 and R1_lbs >= 0 and R2_lbs >= 0:
            max_V_reduced = max(
                max(0, R1_lbs - w_plf * d_ft),
                max(0, R2_lbs - w_plf * d_ft)
            )
        else:
            max_V_reduced = max_V
        
        allowed_Fv = refs["Fv"] * combo["CD"] * CF_fv
        allowed_V = allowed_Fv * props["A"] / 1.5
        util_V = max_V_reduced / allowed_V if allowed_V > 0 else 0
        if util_V > max_shear_util:
            max_shear_util = util_V
            res_shear = {"Actual": max_V_reduced, "Allowed": allowed_V, "Result": util_V*100, "LDF": combo["CD"], "Combo": combo["name"]}
            
        # Moment
        M_neg_R1 = (w_plf * (L_ol_ft**2)) / 2
        M_neg_R2 = (w_plf * (L_or_ft**2)) / 2
        
        # Location of zero shear in the main span (absolute coordinate from left end)
        x_zero_shear = R1_lbs / w_plf if w_plf > 0 else None
        M_pos = 0
        if x_zero_shear is not None and L_ol_ft < x_zero_shear < (L_ol_ft + L_span):
            M_pos = R1_lbs * (x_zero_shear - L_ol_ft) - (w_plf * (x_zero_shear**2)) / 2
            
        max_M_ft_lbs = max(M_neg_R1, M_neg_R2, M_pos)
        
        allowed_Fb = refs["Fb"] * combo["CD"] * CF_fb * Cr * CL_val
        allowed_M_ft_lbs = (allowed_Fb * props["S"]) / 12
        util_M = max_M_ft_lbs / allowed_M_ft_lbs if allowed_M_ft_lbs > 0 else 0
        if util_M > max_moment_util:
            max_moment_util = util_M
            res_moment = {"Actual": max_M_ft_lbs, "Allowed": allowed_M_ft_lbs, "Result": util_M*100, "LDF": combo["CD"], "Combo": combo["name"]}
            
    results["Member Reaction (lbs)"] = res_reaction
    results["Shear (lbs)"] = res_shear
    results["Moment (Ft-lbs)"] = res_moment

    # --- Deflection Superposition ---
    max_TL_plf = max(c["load"] for c in combos)
    
    def calc_deflection(w, is_live_load=False):
        if w == 0: return 0
        w_in = w / 12
        # Simple span term
        delta_simple = (5 * w_in * (span_in**4)) / (384 * E_prime * props["I"])
        # End moment relief (skip for live load — pattern loading puts LL on span only)
        if is_live_load:
            delta_relief = 0.0
        else:
            M1_in_lbs = (w * (L_ol_ft**2) / 2) * 12
            M2_in_lbs = (w * (L_or_ft**2) / 2) * 12
            delta_relief = ((M1_in_lbs + M2_in_lbs) * (span_in**2)) / (16 * E_prime * props["I"])
        
        flat_defl = max(0, delta_simple - delta_relief)
        
        if is_ewp:
            G = E_prime / 16.0
            delta_shear = (1.2 * w_in * (span_in**2)) / (8 * G * props["A"])
            flat_defl += delta_shear

        # Sloped beams: convention is to compare deflection of the
        # equivalent horizontal-projection beam against the limit (matches
        # ForteWeb / Weyerhaeuser). Setting apply_pitch_defl_correction=True
        # restores the prior conservative 1/cos²θ vertical-component scaling.
        if apply_pitch_defl_correction and cos_theta > 0:
            return flat_defl / (cos_theta ** 2)
        return flat_defl
        
    TL_defl = calc_deflection(max_TL_plf)
    results["Total Load Defl (in)"] = {"Actual": TL_defl, "Allowed": allowed_TL_defl, "Result": (TL_defl/allowed_TL_defl)*100, "LDF": "-", "Combo": "Max TL"}
    
    w_LL_eff = max(
        1.0*w_L, 
        1.0*w_Lr, 
        1.0*w_S, 
        0.75*w_L + 0.75*w_Lr, 
        0.75*w_L + 0.75*w_S
    )
    if w_LL_eff > 0:
        LL_defl = calc_deflection(w_LL_eff, is_live_load=True)
        results["Live Load Defl. (in)"] = {"Actual": LL_defl, "Allowed": allowed_LL_defl, "Result": (LL_defl/allowed_LL_defl)*100, "LDF": "-", "Combo": "Live Loads"}
    else:
        results["Live Load Defl. (in)"] = {"Actual": 0.0, "Allowed": allowed_LL_defl, "Result": 0.0, "LDF": "-", "Combo": "None"}

    # Full derivation trail for calculation-sheet reports (additive; the
    # analysis normalizer only consumes "results").
    CD_moment = res_moment["LDF"] if res_moment else 1.0
    CD_shear = res_shear["LDF"] if res_shear else 1.0
    intermediate = {
        "geometry": {
            "L_clear_ft": L_clear, "L_span_ft": L_span, "L_total_ft": L_total,
            "overhang_left_ft": L_ol_ft, "overhang_right_ft": L_or_ft,
            "pitch_rise_per_12": pitch, "trib_width_ft": trib, "spacing_in": spacing_in,
        },
        "section": {
            "b_in": props["b"], "d_in": props["d"], "A_in2": props["A"],
            "S_in3": props["S"], "I_in4": props["I"], "plies": plies,
        },
        "material": {
            "species": species, "grade": grade,
            "Fb_psi": refs["Fb"], "Fv_psi": refs["Fv"], "E_psi": refs["E"],
            "Fc_perp_support_psi": allowed_Fc_perp, "density_pcf": density,
        },
        "adjustment_factors": {
            "CD_moment": CD_moment, "CD_shear": CD_shear, "CF_fb": CF_fb,
            "CF_fv": CF_fv, "Cr": Cr, "CL": CL_val,
        },
        "adjusted_values": {
            "Fb_prime_psi": refs["Fb"] * CD_moment * CF_fb * Cr * CL_val,
            "Fv_prime_psi": refs["Fv"] * CD_shear * CF_fv,
            "E_prime_psi": E_prime,
        },
        "loads": {
            "D_psf": D_psf, "L_psf": L_psf, "Lr_psf": Lr_psf, "S_psf": S_psf,
            "w_D_plf": w_D, "w_L_plf": w_L, "w_Lr_plf": w_Lr, "w_S_plf": w_S,
            "self_weight_plf": self_weight_plf,
        },
        "combos": [
            {"name": c["name"], "w_plf": c["load"], "CD": c["CD"]} for c in combos
        ],
        "bearing": {
            "bearing_R1_in": brg_R1, "bearing_R2_in": brg_R2,
            "support_species": support_species,
            "allowed_R1_lbs": allowed_R1, "allowed_R2_lbs": allowed_R2,
        },
        "deflection": {
            "LL_denom": LL_denom, "TL_denom": TL_denom, "span_in": span_in,
            "allowed_LL_in": allowed_LL_defl, "allowed_TL_in": allowed_TL_defl,
        },
    }

    return {"name": name, "system": system, "application": application, "size": size, "plies": plies, "results": results, "intermediate": intermediate}
