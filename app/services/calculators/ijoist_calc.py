"""
ijoist_calc.py
TJI I-joist checks per Weyerhaeuser TJ-4000 (July 2025) / ICC-ES ESR-1153.

I-joists are proprietary sections: capacities are table values (Mr, Vr,
reaction limits) rather than derived from Fb/Fv, and deflection uses the
manufacturer's two-term formula with a shear-deformation term:

    delta = 22.5*w*L^4 / EI  +  coeff*w*L^2 / (d*10^5)      [TJ-4000]
      w in plf, L in ft, d = joist depth (in), EI in lb-in^2
      coeff = 2.67 (TJI 110/210/230/360)  or  2.29 (TJI 560/560D)

Notes per TJ-4000:
  - Moment design properties must NOT be increased by a repetitive-member
    factor.
  - Design shear is computed at the inside face of support.
  - Mr and Vr are 100% load-duration values -> multiplied by CD here.
    Reaction (bearing) limits are NOT duration-adjusted (conservative).
  - TJ-Pro floor vibration ratings are proprietary and not evaluated here.
"""

from services.calculators.utils.load_combos import get_load_combinations

# (series, depth_in) -> properties. TJ-4000 Design Properties, 100% LDF.
# weight plf | Mr ft-lbs | EI x10^6 in^2-lbs | Vr lbs |
# end reactions at 1-3/4" and 3-1/2" bearing (no web stiffeners) |
# 3-1/2" end reaction with web stiffeners
TJI_PROPS = {
    ("110", 9.5):    {"w": 2.3, "Mr": 2500,  "EI": 157,  "Vr": 1220, "R175": 910,  "R35": 1220, "R35_ws": 1220},
    ("210", 9.5):    {"w": 2.6, "Mr": 3000,  "EI": 186,  "Vr": 1330, "R175": 1005, "R35": 1330, "R35_ws": 1330},
    ("230", 9.5):    {"w": 2.7, "Mr": 3330,  "EI": 206,  "Vr": 1330, "R175": 1060, "R35": 1330, "R35_ws": 1330},
    ("110", 11.875): {"w": 2.5, "Mr": 3160,  "EI": 267,  "Vr": 1560, "R175": 910,  "R35": 1375, "R35_ws": 1935},
    ("210", 11.875): {"w": 2.8, "Mr": 3795,  "EI": 315,  "Vr": 1655, "R175": 1005, "R35": 1460, "R35_ws": 2145},
    ("230", 11.875): {"w": 3.0, "Mr": 4215,  "EI": 347,  "Vr": 1655, "R175": 1060, "R35": 1485, "R35_ws": 2410},
    ("360", 11.875): {"w": 3.0, "Mr": 6180,  "EI": 419,  "Vr": 1705, "R175": 1080, "R35": 1505, "R35_ws": 2460},
    ("560", 11.875): {"w": 4.0, "Mr": 9500,  "EI": 636,  "Vr": 2050, "R175": 1265, "R35": 1725, "R35_ws": 3000},
    ("110", 14):     {"w": 2.8, "Mr": 3740,  "EI": 392,  "Vr": 1860, "R175": 910,  "R35": 1375, "R35_ws": 1935},
    ("210", 14):     {"w": 3.1, "Mr": 4490,  "EI": 462,  "Vr": 1945, "R175": 1005, "R35": 1460, "R35_ws": 2145},
    ("230", 14):     {"w": 3.3, "Mr": 4990,  "EI": 509,  "Vr": 1945, "R175": 1060, "R35": 1485, "R35_ws": 2410},
    ("360", 14):     {"w": 3.3, "Mr": 7335,  "EI": 612,  "Vr": 1955, "R175": 1080, "R35": 1505, "R35_ws": 2460},
    ("560", 14):     {"w": 4.2, "Mr": 11275, "EI": 926,  "Vr": 2390, "R175": 1265, "R35": 1725, "R35_ws": 3000},
    ("110", 16):     {"w": 3.0, "Mr": 4280,  "EI": 535,  "Vr": 2145, "R175": 910,  "R35": 1375, "R35_ws": 1935},
    ("210", 16):     {"w": 3.3, "Mr": 5140,  "EI": 629,  "Vr": 2190, "R175": 1005, "R35": 1460, "R35_ws": 2145},
    ("230", 16):     {"w": 3.5, "Mr": 5710,  "EI": 691,  "Vr": 2190, "R175": 1060, "R35": 1485, "R35_ws": 2410},
    ("360", 16):     {"w": 3.5, "Mr": 8405,  "EI": 830,  "Vr": 2190, "R175": 1080, "R35": 1505, "R35_ws": 2460},
    ("560", 16):     {"w": 4.5, "Mr": 12925, "EI": 1252, "Vr": 2710, "R175": 1265, "R35": 1725, "R35_ws": 3000},
}

_DEPTH_ALIASES = {9.5: 9.5, 11.875: 11.875, 11.88: 11.875, 11.9: 11.875, 14: 14.0, 16: 16.0}


def tji_catalog() -> str:
    depths = sorted({d for _, d in TJI_PROPS})
    series = sorted({s for s, _ in TJI_PROPS}, key=int)
    return (f"TJI series: {', '.join(series)}; depths (in): "
            f"{', '.join(str(d) for d in depths)} "
            "(9.5\" available in 110/210/230 only)")


def _normalize_series(series) -> str:
    s = str(series).upper().replace("TJI", "").replace("®", "").strip().strip("-/ ")
    if s.endswith("D"):  # 560D -> use 560 values (flag in notes)
        s = s[:-1]
    return s


def check_ijoist(name, series, depth_in, clear_span_ft, spacing_in=16.0,
                 D_psf=10.0, L_psf=40.0, Lr_psf=0.0, S_psf=0.0,
                 bearing_length_in=1.75, web_stiffeners=False,
                 system=None, defl_LL_denom=None, defl_TL_denom=None):
    series_key = _normalize_series(series)
    depth = _DEPTH_ALIASES.get(round(float(depth_in), 3), float(depth_in))
    props = TJI_PROPS.get((series_key, depth))
    if props is None:
        raise ValueError(
            f"TJI {series} at {depth_in}\" is not in the library. {tji_catalog()}"
        )
    was_560d = str(series).upper().strip().endswith("D")

    # ---- Geometry & loads ----
    system = system or ("Roof" if (Lr_psf or S_psf) else "Floor")
    trib = spacing_in / 12.0
    # design span: clear span + half of min end bearing (1.75") each side
    L = clear_span_ft + 2.0 * (1.75 / 2.0) / 12.0
    w_D = D_psf * trib + props["w"]
    w_L = L_psf * trib
    w_Lr = Lr_psf * trib
    w_S = S_psf * trib
    combos = get_load_combinations(D=w_D, L=w_L, Lr=w_Lr, S=w_S)

    EI = props["EI"] * 1.0e6
    coeff = 2.29 if series_key == "560" else 2.67

    # ---- Reaction (bearing) capacity: interpolate 1.75" -> 3.5" bearing ----
    brg = max(float(bearing_length_in), 1.75)
    R_lo, R_hi = props["R175"], (props["R35_ws"] if web_stiffeners else props["R35"])
    frac = min((brg - 1.75) / (3.5 - 1.75), 1.0)
    R_allow = R_lo + (R_hi - R_lo) * frac      # not duration-adjusted

    # ---- Per-combo demands ----
    face_offset_ft = (brg / 2.0) / 12.0        # support centerline to inside face
    best = {"M": None, "V": None, "R": None}
    for c in combos:
        w = c["load"]
        R = w * L / 2.0
        V = w * (L / 2.0 - face_offset_ft)     # at inside face of support
        M = w * L ** 2 / 8.0
        M_allow = props["Mr"] * c["CD"]
        V_allow = props["Vr"] * c["CD"]
        for key, act, allow, ldf in (
            ("M", M, M_allow, c["CD"]), ("V", V, V_allow, c["CD"]), ("R", R, R_allow, "-"),
        ):
            util = act / allow if allow > 0 else 0.0
            if best[key] is None or util > best[key]["Result"] / 100.0:
                best[key] = {"Actual": act, "Allowed": allow,
                             "Result": util * 100.0, "LDF": ldf, "Combo": c["name"]}

    # ---- Deflection (TJ-4000 two-term formula) ----
    if str(system).lower() == "floor":
        LL_denom_d, TL_denom_d = 360, 240
    else:
        LL_denom_d, TL_denom_d = 240, 180
    LL_denom = defl_LL_denom or LL_denom_d
    TL_denom = defl_TL_denom or TL_denom_d
    span_in = L * 12.0

    def _defl(w):
        if w <= 0:
            return 0.0, 0.0, 0.0
        bend = 22.5 * w * L ** 4 / EI
        shear = coeff * w * L ** 2 / (depth * 1.0e5)
        return bend, shear, bend + shear

    w_TL = max(c["load"] for c in combos)
    w_LL = max(w_L, w_Lr, w_S, 0.75 * w_L + 0.75 * w_Lr, 0.75 * w_L + 0.75 * w_S)
    bend_TL, shear_TL, d_TL = _defl(w_TL)
    bend_LL, shear_LL, d_LL = _defl(w_LL)
    allow_TL = span_in / TL_denom
    allow_LL = span_in / LL_denom

    results = {
        "Member Reaction (lbs)": best["R"],
        "Shear (lbs)": best["V"],
        "Moment (Ft-lbs)": best["M"],
        "Total Load Defl (in)": {
            "Actual": d_TL, "Allowed": allow_TL,
            "Result": d_TL / allow_TL * 100.0 if allow_TL > 0 else 0.0,
            "LDF": "-", "Combo": "Max TL",
        },
        "Live Load Defl. (in)": {
            "Actual": d_LL, "Allowed": allow_LL,
            "Result": d_LL / allow_LL * 100.0 if allow_LL > 0 else 0.0,
            "LDF": "-", "Combo": "Live Loads",
        },
    }

    notes = [
        "Capacities per Weyerhaeuser TJ-4000 / ESR-1153 (100% LDF table values, "
        "CD applied to moment and shear; reactions not duration-adjusted).",
        "Repetitive-member factor NOT applied (prohibited for I-joist moment).",
        "TJ-Pro floor vibration rating is proprietary and not evaluated — "
        "verify serviceability for spans over ~18 ft.",
    ]
    if was_560d:
        notes.append("560D requested — checked with TJI 560 properties "
                     "(560D values are equal or higher; verify with ForteWEB).")

    intermediate = {
        "product": {
            "series": f"TJI {series_key}", "depth_in": depth,
            "weight_plf": props["w"], "Mr_ftlb_100": props["Mr"],
            "Vr_lb_100": props["Vr"], "EI_e6": props["EI"],
            "source": "TJ-4000 (July 2025)",
        },
        "geometry": {
            "L_clear_ft": clear_span_ft, "L_span_ft": L,
            "spacing_in": spacing_in, "trib_width_ft": trib,
        },
        "loads": {
            "D_psf": D_psf, "L_psf": L_psf, "Lr_psf": Lr_psf, "S_psf": S_psf,
            "w_D_plf": w_D, "w_L_plf": w_L, "w_Lr_plf": w_Lr, "w_S_plf": w_S,
            "self_weight_plf": props["w"],
        },
        "combos": [{"name": c["name"], "w_plf": c["load"], "CD": c["CD"]} for c in combos],
        "bearing": {
            "bearing_length_in": brg, "web_stiffeners": web_stiffeners,
            "R_allow_lbs": R_allow, "R175_lbs": R_lo, "R35_lbs": R_hi,
        },
        "deflection": {
            "formula_coeff": coeff, "EI_lbin2": EI,
            "LL_denom": LL_denom, "TL_denom": TL_denom, "span_in": span_in,
            "allowed_LL_in": allow_LL, "allowed_TL_in": allow_TL,
            "bend_LL_in": bend_LL, "shear_LL_in": shear_LL,
            "bend_TL_in": bend_TL, "shear_TL_in": shear_TL,
        },
        "notes": notes,
    }

    return {
        "name": name, "system": system, "application": "I-Joist",
        "size": f'{depth:g}" TJI {series_key}', "plies": 1,
        "results": results, "intermediate": intermediate,
    }
