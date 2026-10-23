"""
diaphragm_calc.py
Wood structural panel diaphragm checks per SDPWS 2021 §4.2 — flexible,
single-span rectangular diaphragms (the ASCE 7 §12.3.1.1 idealization for
light-frame construction).

Model: the diaphragm spans L between two parallel lateral-force-resisting
lines and carries the full story shear V in that direction as a uniform
load w = V/L. Then:

    end reaction (per line)   R  = V / 2
    max unit shear            v  = R / d = V / (2·d)
    chord force               T = C = M / d = w·L² / (8·d) = V·L / (8·d)

Capacity: the nominal unit shear v_n comes from the plans' diaphragm
schedule or SDPWS Table 4.2A/4.2C (supplied as input — table values are
not bundled). Per SDPWS §4.1.4:
    ASD seismic:  v_allow = v_n / 2.0
    ASD wind:     v_allow = 1.4 · v_n / 2.0

Aspect ratio (SDPWS Table 4.2.4): L/d <= 4 blocked, <= 3 unblocked.
"""


def check_diaphragm(name, span_ft, depth_ft, Vs_lbs=0.0, Vw_lbs=0.0,
                    nominal_unit_shear_plf=None, blocked=True,
                    sheathing_note="", chord_member=""):
    L = float(span_ft)
    d = float(depth_ft)
    if L <= 0 or d <= 0:
        raise ValueError(f"{name}: diaphragm span and depth must be positive.")
    Vs = max(float(Vs_lbs or 0.0), 0.0)
    Vw = max(float(Vw_lbs or 0.0), 0.0)
    vn = float(nominal_unit_shear_plf) if nominal_unit_shear_plf else None

    # ---- Demands ----
    v_s = Vs / (2.0 * d)                     # ASD seismic unit shear, plf
    v_w = Vw / (2.0 * d)                     # ASD wind unit shear, plf
    w_s = Vs / L if L > 0 else 0.0
    w_w = Vw / L if L > 0 else 0.0
    T_s = Vs * L / (8.0 * d)                 # chord force, lbs (seismic)
    T_w = Vw * L / (8.0 * d)
    R_s, R_w = Vs / 2.0, Vw / 2.0            # collector/line reactions

    # ---- Capacities ----
    v_allow_s = vn / 2.0 if vn else None
    v_allow_w = 1.4 * vn / 2.0 if vn else None

    # ---- Aspect ratio (SDPWS 4.2.4) ----
    ar = L / d
    ar_limit = 4.0 if blocked else 3.0

    def _chk(actual, allowed, combo, ldf="-"):
        util = actual / allowed * 100.0 if allowed else None
        return {"Actual": actual, "Allowed": allowed,
                "Result": round(util, 1) if util is not None else None,
                "LDF": ldf, "Combo": combo}

    results = {
        "Unit Shear Seismic (plf)": _chk(round(v_s, 1), round(v_allow_s, 1) if v_allow_s else None, "0.7E"),
        "Unit Shear Wind (plf)": _chk(round(v_w, 1), round(v_allow_w, 1) if v_allow_w else None, "0.6W"),
        "Aspect Ratio (L/d)": _chk(round(ar, 2), ar_limit, "SDPWS 4.2.4"),
    }

    notes = []
    if vn is None:
        notes.append(
            "Nominal unit shear capacity not provided — demands are reported "
            "but the shear check is incomplete. Supply the diaphragm schedule "
            "value (SDPWS Table 4.2A/4.2C nominal, plf)."
        )
    notes.append(
        "Single-span flexible diaphragm idealization (ASCE 7 §12.3.1.1); "
        "chords and collectors must be continuous or spliced for the forces shown."
    )

    governs = "Seismic" if v_s >= v_w else "Wind"
    intermediate = {
        "geometry": {"span_ft": L, "depth_ft": d, "aspect_ratio": round(ar, 2),
                     "ar_limit": ar_limit, "blocked": bool(blocked)},
        "demands": {
            "Vs_lbs": Vs, "Vw_lbs": Vw,
            "w_s_plf": round(w_s, 1), "w_w_plf": round(w_w, 1),
            "v_s_plf": round(v_s, 1), "v_w_plf": round(v_w, 1),
            "R_s_lbs": round(R_s, 0), "R_w_lbs": round(R_w, 0),
            "chord_T_s_lbs": round(T_s, 0), "chord_T_w_lbs": round(T_w, 0),
            "governs": governs,
        },
        "capacity": {
            "v_nominal_plf": vn,
            "v_allow_seismic_plf": round(v_allow_s, 1) if v_allow_s else None,
            "v_allow_wind_plf": round(v_allow_w, 1) if v_allow_w else None,
            "asd_factor": 2.0, "wind_factor": 1.4,
        },
        "chord_member": chord_member or None,
        "sheathing_note": sheathing_note or None,
        "notes": notes,
    }

    return {
        "name": name, "system": "Lateral", "application": "Diaphragm",
        "size": sheathing_note or f"{L:g} x {d:g} ft", "plies": 1,
        "results": results, "intermediate": intermediate,
    }
