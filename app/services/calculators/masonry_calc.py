"""
masonry_calc.py
Masonry wall panel design — TMS 402/602-16, Strength Design method (Ch. 9).

Single-wythe reinforced concrete masonry (CMU) wall panel loaded
out-of-plane (axial + flexure) plus out-of-plane shear. Mirrors the
Tekla Tedds "Masonry wall panel design (TMS 402/602-16)" module, strength
design, verified line-by-line against a Tedds output (Pelican House A-1599,
8" fully grouted CMU, #5 @ 24", h=12 ft, W=40 psf).

Scope / assumptions:
  - Wall spans vertically, pinned top and bottom for out-of-plane loads
    (simple span -> M = w*h^2/8 at mid-height).
  - Slender wall design per TMS 402 §9.3.5 with P-delta moment magnification
    (§9.3.5.4.2 requires factored axial stress P/Ag <= 0.20*f'm).
  - Reinforcement centered in the wall (d = t/2) unless overridden.
  - Single layer of vertical reinforcement; tension-controlled section.
  - Out-of-plane shear per §9.3.4.1.2.

Out-of-plane load handling (answers "do I apply a share?"):
  Out-of-plane wind is applied as the full pressure (psf) over the panel
  face — NOT a distributed "share" of a building lateral total. Seismic
  out-of-plane is the wall's own inertial force Fp*Wself (ASCE 7 §12.11).
  The "critical panel" is the governing one (tallest / most slender /
  highest axial). Sharing of a story shear applies only to IN-PLANE shear
  walls (see shear_wall_calc / lateral distribution) — not to this check.

Import: from masonry_calc import check_masonry_wall, MasonryWallInputs
"""

import math
from dataclasses import dataclass, field
from typing import List, Optional, Any


# ============================================================
# Reference tables
# ============================================================

# TMS 402-16 Table 9.1.9.2 — Modulus of rupture fr (psi).
# Keyed by (tension direction, grout, mortar class). "ms" = Portland
# cement/lime or mortar cement Type M or S; "n" = Type N.
# Fully grouted values are independent of mortar type.
MODULUS_OF_RUPTURE = {
    # tension normal to bed joints (vertical span — the usual case)
    ("normal", "ungrouted",  "ms"): 100,
    ("normal", "ungrouted",  "n"):  75,
    ("normal", "partial",    "ms"): 163,
    ("normal", "partial",    "n"):  158,
    ("normal", "full",       "ms"): 163,
    ("normal", "full",       "n"):  163,
    # tension parallel to bed joints
    ("parallel", "ungrouted", "ms"): 200,
    ("parallel", "ungrouted", "n"):  150,
    ("parallel", "partial",   "ms"): 267,
    ("parallel", "partial",   "n"):  267,
    ("parallel", "full",      "ms"): 267,
    ("parallel", "full",      "n"):  267,
}

# Standard reinforcing bar areas (in^2) by US bar number
BAR_AREA = {
    3: 0.11, 4: 0.20, 5: 0.31, 6: 0.44, 7: 0.60,
    8: 0.79, 9: 1.00, 10: 1.27, 11: 1.56,
}
BAR_DIA = {
    3: 0.375, 4: 0.500, 5: 0.625, 6: 0.750, 7: 0.875,
    8: 1.000, 9: 1.128, 10: 1.270, 11: 1.410,
}

EPS_MU = 0.0025          # max usable masonry compressive strain (CMU), §9.3.2
PHI_FLEXURE_AXIAL = 0.90  # §9.1.4.1
PHI_SHEAR = 0.80          # §9.1.4.5


@dataclass
class MasonryWallInputs:
    # Geometry
    wall_height_ft: float = 12.0      # h (clear span, out-of-plane)
    panel_length_ft: float = 3.8      # L (informational / in-plane width)
    thickness_in: float = 7.625       # t (nominal 8" CMU = 7.625")
    K: float = 1.0                    # effective height factor

    # Masonry (TMS 602 unit-strength method)
    fm_psi: float = 1900.0            # f'm net compressive strength
    grout: str = "full"              # "full" / "partial" / "ungrouted"
    mortar_class: str = "ms"         # "ms" (Type M/S) or "n" (Type N)

    # Unit geometry (for self weight); defaults = 8" CMU
    block_density_pcf: float = 115.0
    grout_density_pcf: float = 140.0
    unit_length_in: float = 15.625
    n_internal_webs: int = 1
    n_end_webs: int = 2
    web_thickness_in: float = 1.25     # internal web
    face_shell_in: float = 1.25
    end_web_in: float = 1.25

    # Reinforcement
    bar_size: int = 5                  # US bar number
    bar_spacing_in: float = 24.0       # vertical bar spacing
    fy_psi: float = 60000.0
    Es_psi: float = 29_000_000.0
    d_in: Optional[float] = None       # depth to steel; default t/2 (centered)

    # Out-of-plane loads
    wind_psf: float = 40.0             # W on panel (ASD or strength? see note)
    SDS: float = 1.0
    Ie: float = 1.0
    rho_E: float = 1.0                 # redundancy on out-of-plane seismic
    seismic_add_psf: float = 0.0       # Eadd (e.g., attached weights)

    # Axial loads from above (per ft of wall), service level
    P_dead_plf: float = 0.0            # roof/floor dead at top of wall
    P_live_plf: float = 0.0
    P_roof_live_plf: float = 0.0
    P_snow_plf: float = 0.0
    ecc_in: float = 0.0                # eccentricity of top axial load

    # Strength-level lateral flags
    wind_is_strength_level: bool = True   # ASCE 7-16/22 wind is strength level (W factor 1.0)
    wind_source: str = ""                 # provenance note for the wind pressure

    # ---- Section-design mode (TMS "Masonry wall section design"): explicit
    # factored forces, out-of-plane AND in-plane, biaxial + shear friction ----
    mode: str = "panel"                   # "panel" (derive loads) or "section" (explicit)
    heff_x_ft: Optional[float] = None     # effective height, major (in-plane) axis
    heff_y_ft: Optional[float] = None     # effective height, minor (out-of-plane) axis
    pdelta_method: str = "magnifier"      # "magnifier" (Eqn 9-27/28/29) or "iteration"
    Ieff_factor: float = 0.75             # Tedds effective-stiffness factor on In for Pe

    # Explicit factored design forces (section mode), per ft for out-of-plane
    P_axial_plf: float = 1000.0           # factored axial (lb/ft)
    My_lbft_per_ft: float = 1000.0        # out-of-plane moment (lb-ft/ft)
    Vy_lbf_per_ft: float = 1000.0         # out-of-plane shear (lb/ft)
    Mx_lbft: float = 1000.0               # in-plane moment (lb-ft)
    Vx_lbf: float = 1000.0                # in-plane shear (lb)

    # Horizontal (shear) reinforcement for in-plane shear
    h_bar_size: int = 3
    h_bar_spacing_in: float = 48.0
    h_bar_legs: int = 2
    fy_hori_psi: float = 60000.0
    mu_friction: float = 0.7              # shear-friction coefficient §9.3.6.5


def _round(x, n=1):
    return round(x, n)


def self_weight_psf(inp: "MasonryWallInputs") -> float:
    """Wall self weight (psf). Fully grouted = block + grout; else block only."""
    t, lb = inp.thickness_in, inp.unit_length_in
    solid = (t * lb - (lb - inp.n_internal_webs * inp.web_thickness_in
                       - inp.n_end_webs * inp.end_web_in) * (t - 2 * inp.face_shell_in))
    grout = ((lb - inp.n_internal_webs * inp.web_thickness_in
              - inp.n_end_webs * inp.end_web_in) * (t - 2 * inp.face_shell_in))
    A_block = solid * 12.0 / lb
    A_grout = grout * 12.0 / lb
    if inp.grout == "full":
        return A_block / 144.0 * inp.block_density_pcf + A_grout / 144.0 * inp.grout_density_pcf
    return A_block / 144.0 * inp.block_density_pcf


def check_masonry_shear_wall(
    name: str,
    wall_height_ft: float,
    wall_length_ft: float,
    thickness_in: float = 7.625,
    fm_psi: float = 1900.0,
    grout: str = "full",
    vertical_bar_size: int = 5,
    vertical_bar_spacing_in: float = 24.0,
    horizontal_bar_size: int = 4,
    horizontal_bar_spacing_in: float = 48.0,
    seismic_shear_lbs: float = 0.0,
    wind_shear_lbs: float = 0.0,
    axial_dead_plf: float = 0.0,
    sds: float = 0.5,
    wind_pressure_psf: float = 0.0,
) -> dict:
    """CMU wall as an in-plane shear wall (TMS 402 strength design, biaxial
    section mode): converts ASD story-shear demands (0.7E / 0.6W) to strength
    level, models the wall as a base-fixed cantilever (M = V·h), envelopes
    out-of-plane wind vs seismic Fp, and runs the full section design
    (in-plane shear Eq. 9-20/9-21, shear friction, flexure, interaction)."""
    h = float(wall_height_ft)
    L = float(wall_length_ft)
    # ASD -> strength-level in-plane demand
    Vx = max(float(seismic_shear_lbs) / 0.7, float(wind_shear_lbs) / 0.6)
    Mx = Vx * h  # cantilever: fixed base, free top

    inp = MasonryWallInputs(
        wall_height_ft=h,
        panel_length_ft=L,
        thickness_in=float(thickness_in),
        fm_psi=float(fm_psi),
        grout=grout,
        bar_size=int(vertical_bar_size),
        bar_spacing_in=float(vertical_bar_spacing_in),
        SDS=float(sds),
        wind_psf=float(wind_pressure_psf),
        P_dead_plf=float(axial_dead_plf),
    )
    wsw = self_weight_psf(inp)
    E_wall = max(0.4 * float(sds), 0.1) * wsw
    w_gov = max(float(wind_pressure_psf), E_wall)  # out-of-plane, strength level

    inp.mode = "section"
    inp.heff_x_ft = h
    inp.heff_y_ft = h
    inp.P_axial_plf = 0.9 * (float(axial_dead_plf) + wsw * h / 2.0)
    inp.My_lbft_per_ft = w_gov * h ** 2 / 8.0
    inp.Vy_lbf_per_ft = w_gov * h / 2.0
    inp.Mx_lbft = Mx
    inp.Vx_lbf = Vx
    inp.h_bar_size = int(horizontal_bar_size)
    inp.h_bar_spacing_in = float(horizontal_bar_spacing_in)
    out = check_masonry_wall(inp=inp)
    out["name"] = name
    return out


def check_masonry_wall(inp: MasonryWallInputs) -> dict:
    if inp.mode == "section":
        return _masonry_section_design(inp)
    t = inp.thickness_in
    h_ft = inp.wall_height_ft
    h_in = h_ft * 12.0
    b = 12.0                              # design strip width, in (per ft)
    d = inp.d_in if inp.d_in else t / 2.0

    # ---- Self weight (per ft of wall length) ----
    w_self = self_weight_psf(inp)
    if inp.grout == "full":
        A = b * t                              # gross section
    else:
        lb = inp.unit_length_in
        solid_over_unit = (t * lb - (lb - inp.n_internal_webs * inp.web_thickness_in
                           - inp.n_end_webs * inp.end_web_in) * (t - 2 * inp.face_shell_in))
        A = solid_over_unit * 12.0 / lb        # net section (conservative)

    # ---- Masonry properties ----
    Em = 900.0 * inp.fm_psi                    # §4.2.2
    fr = MODULUS_OF_RUPTURE.get((("normal"), inp.grout, inp.mortar_class), 100)

    I = b * t ** 3 / 12.0
    S = I / (t / 2.0)
    r = math.sqrt(I / A)

    # ---- Reinforcement ----
    # Exact circular bar area (matches Tedds: As = pi*dia^2/(4*s) per ft)
    bar_dia = BAR_DIA.get(inp.bar_size, 0.625)
    bar_area = math.pi * bar_dia ** 2 / 4.0
    As = bar_area * 12.0 / inp.bar_spacing_in   # in^2/ft
    n_modular = inp.Es_psi / Em

    # ---- Loads ----
    Fp = 0.4 * inp.SDS * inp.Ie
    E_wall = max(Fp, 0.1) * w_self
    E = E_wall + inp.seismic_add_psf
    W = inp.wind_psf
    FEv = 0.2 * inp.SDS                          # vertical seismic on dead

    # Axial dead at mid-height: top load + self weight of upper half
    P_top_D = inp.P_dead_plf
    P_sw_mid = w_self * (h_ft / 2.0)             # lb/ft

    # ---- Strength load combinations (ASCE 7-16/22 / IBC 1605.2) ----
    Wf = 1.0 if inp.wind_is_strength_level else 1.6
    combos: list[dict[str, Any]] = [
        {"name": "1.4D", "fD": 1.4, "L": 0, "Lr": 0, "S": 0, "lat": 0.0, "kind": "grav"},
        {"name": "1.2D + 1.6L + 0.5(Lr/S)", "fD": 1.2, "L": 1.6, "Lr": 0.5, "S": 0.5, "lat": 0.0, "kind": "grav"},
        {"name": "1.2D + 1.6(Lr/S) + 1.0L", "fD": 1.2, "L": 1.0, "Lr": 1.6, "S": 1.6, "lat": 0.0, "kind": "grav"},
        {"name": "1.2D + 1.0W + 1.0L + 0.5(Lr/S)", "fD": 1.2, "L": 1.0, "Lr": 0.5, "S": 0.5,
         "lat": Wf * W, "kind": "wind"},
        {"name": "0.9D + 1.0W", "fD": 0.9, "L": 0, "Lr": 0, "S": 0, "lat": Wf * W, "kind": "wind"},
        {"name": "(1.2+0.2SDS)D + rho*E + 1.0L + 0.2S", "fD": 1.2 + FEv, "L": 1.0, "Lr": 0, "S": 0.2,
         "lat": inp.rho_E * E, "kind": "seismic"},
        {"name": "(0.9-0.2SDS)D + rho*E", "fD": 0.9 - FEv, "L": 0, "Lr": 0, "S": 0, "lat": inp.rho_E * E,
         "kind": "seismic"},
    ]

    # Cracking moment (uncracked limit)
    Mcr = S * fr                                  # lb_in/ft

    def _axial_mid(c):
        return (c["fD"] * (P_top_D + P_sw_mid)
                + c["L"] * inp.P_live_plf
                + c["Lr"] * inp.P_roof_live_plf
                + c["S"] * inp.P_snow_plf)

    def _design_moment(c, Pu):
        """Mid-height design moment with P-delta magnification (§9.3.5.4)."""
        # simple-span mid-height moment: w*h^2/8, w in lb/ft, h in ft -> lb_in
        M_lat = c["lat"] * h_ft ** 2 / 8.0 * 12.0 if c["lat"] else 0.0   # lb_in/ft
        # top axial eccentric moment shared to mid-height (half)
        M_ecc = (c["fD"] * P_top_D) * inp.ecc_in / 2.0
        M0 = M_lat + M_ecc
        if M0 <= 0:
            return 0.0, 0.0
        # iterate deflection: uncracked (Ig) until Mu>Mcr, then add cracked part
        Icr = _Icr(Pu)
        Mu = M0
        for _ in range(50):
            if Mu <= Mcr:
                delta = 5.0 * Mu * h_in ** 2 / (48.0 * Em * I)
            else:
                delta = (5.0 * Mcr * h_in ** 2 / (48.0 * Em * I)
                         + 5.0 * (Mu - Mcr) * h_in ** 2 / (48.0 * Em * Icr))
            Mu_new = M0 + Pu * delta
            if abs(Mu_new - Mu) < 1e-6:
                Mu = Mu_new
                break
            Mu = Mu_new
        return Mu, delta

    def _Icr(Pu):
        c_na = (As * inp.fy_psi + Pu) / (0.64 * inp.fm_psi * b)
        return (n_modular * (As + Pu * t / (inp.fy_psi * 2.0 * d)) * (d - c_na) ** 2
                + b * c_na ** 3 / 3.0)

    def _phi_Mn(Pu):
        """Design flexural strength at axial load Pu (tension-controlled)."""
        a = (As * inp.fy_psi + Pu) / (0.80 * inp.fm_psi * b)
        Mn = (As * inp.fy_psi + Pu) * (d - a / 2.0)
        return PHI_FLEXURE_AXIAL * Mn

    # ---- Axial strength (§9.3.4.1.1, Eq 9-11; h/r <= 99) ----
    slenderness = inp.K * h_in / r
    if slenderness <= 99:
        Pn = 0.80 * (0.80 * inp.fm_psi * (A - As) + inp.fy_psi * As) * (1.0 - (inp.K * h_in / (140.0 * r)) ** 2)
    else:
        Pn = 0.80 * (0.80 * inp.fm_psi * (A - As) + inp.fy_psi * As) * (70.0 * r / (inp.K * h_in)) ** 2
    phi_Pn = PHI_FLEXURE_AXIAL * Pn

    # ---- Evaluate all combinations ----
    governing: dict[str, Any] = {"util": -1.0}
    combo_utils = {}
    for c in combos:
        Pu = _axial_mid(c)
        Mu, delta = _design_moment(c, Pu)
        phiMn = _phi_Mn(Pu)
        u_flex = Mu / phiMn if phiMn > 0 else 0.0
        u_axial = Pu / phi_Pn if phi_Pn > 0 else 0.0
        u_stress = (Pu / A) / (0.20 * inp.fm_psi)
        util = max(u_flex, u_axial)
        combo_utils[c["name"]] = round(util, 3)
        if util > governing["util"]:
            governing = {"util": util, "combo": c, "Pu": Pu, "Mu": Mu, "delta": delta,
                         "phiMn": phiMn, "u_flex": u_flex, "u_axial": u_axial,
                         "u_stress": u_stress}

    g = governing
    Pu_g, Mu_g = g["Pu"], g["Mu"]

    # ---- Balance point (interaction diagram apex) ----
    eps_s = inp.fy_psi / inp.Es_psi
    c_bal = EPS_MU * d / (EPS_MU + eps_s)
    beta1 = 0.80
    T_bal = As * inp.fy_psi
    C_bal = 0.80 * inp.fm_psi * beta1 * c_bal * b
    P_bal = PHI_FLEXURE_AXIAL * (C_bal - T_bal)
    M_bal = PHI_FLEXURE_AXIAL * (T_bal * (d - t / 2.0)
                                 + C_bal * (t / 2.0 - beta1 * c_bal / 2.0))

    # ---- Max reinforcement (§9.3.3.2) ----
    As_max = 0.64 * inp.fm_psi * (EPS_MU / (EPS_MU + 1.5 * eps_s)) * b * d / inp.fy_psi

    # ---- Out-of-plane shear (at support; M/(Vd) ~ 0 -> coefficient 4) ----
    # Demand enveloped over ALL combos — the flexure-governing combo does
    # not necessarily carry the largest lateral pressure.
    shear_combo = max(combos, key=lambda c: abs(c["lat"]))
    w_lat = abs(shear_combo["lat"])
    V = w_lat * h_ft / 2.0                         # lb/ft (simple span reaction)
    Nu = 0.0                                        # compression at top of panel
    Anv = b * d
    gamma_g = 1.0 if inp.grout == "full" else 0.75
    MoverVd = 0.0                                   # at end, M=0
    Vnm = (4.0 - 1.75 * min(MoverVd, 1.0)) * Anv * math.sqrt(inp.fm_psi) + 0.25 * Nu
    Vn_cap = 6.0 * Anv * math.sqrt(inp.fm_psi)      # M/(V*dv) <= 0.25 -> Eq. 9-18
    Vn = min(Vnm, Vn_cap) * gamma_g
    phi_Vn = PHI_SHEAR * Vn
    u_shear = V / phi_Vn if phi_Vn > 0 else 0.0

    # ---- Assemble checks ----
    results = {
        "Flexure (M / phi*Mn)": {
            "Actual": _round(Mu_g, 0), "Allowed": _round(g["phiMn"], 0),
            "Result": _round(g["u_flex"] * 100, 1), "LDF": "-", "Combo": g["combo"]["name"],
        },
        "Axial (P / phi*Pn)": {
            "Actual": _round(Pu_g, 0), "Allowed": _round(phi_Pn, 0),
            "Result": _round(g["u_axial"] * 100, 1), "LDF": "-", "Combo": g["combo"]["name"],
        },
        "Axial Stress (P/A / 0.2f'm)": {
            "Actual": _round(Pu_g / A, 1), "Allowed": _round(0.20 * inp.fm_psi, 0),
            "Result": _round(g["u_stress"] * 100, 1), "LDF": "-", "Combo": g["combo"]["name"],
        },
        "Out-of-plane Shear (V / phi*Vn)": {
            "Actual": _round(V, 0), "Allowed": _round(phi_Vn, 0),
            "Result": _round(u_shear * 100, 1), "LDF": "-", "Combo": shear_combo["name"],
        },
        "Reinforcement (As / As,max)": {
            "Actual": _round(As, 3), "Allowed": _round(As_max, 3),
            "Result": _round(As / As_max * 100, 1) if As_max > 0 else None,
            "LDF": "-", "Combo": "§9.3.3.2",
        },
    }

    overall = "PASS" if all(
        (chk["Result"] is None or chk["Result"] <= 100.0) for chk in results.values()
    ) else "FAIL"

    return {
        "name": None,
        "system": "Masonry",
        "application": "Masonry Wall Panel",
        "size": f'{t:.3f}" CMU, #{inp.bar_size} @ {inp.bar_spacing_in:g}"',
        "method": "TMS 402-16 Strength Design (out-of-plane)",
        "results": results,
        "section": {
            "t_in": t, "A_in2_per_ft": _round(A, 1), "I_in4_per_ft": _round(I, 1),
            "S_in3_per_ft": _round(S, 1), "r_in": _round(r, 3),
            "w_self_psf": _round(w_self, 2), "Em_psi": _round(Em, 0),
            "fr_psi": fr, "As_in2_per_ft": _round(As, 3), "d_in": _round(d, 3),
            "slenderness_kh_r": _round(slenderness, 2), "modular_ratio_n": _round(n_modular, 3),
            "Mcr_lbin_per_ft": _round(Mcr, 0),
        },
        "loads": {
            "wind_psf": W, "wind_source": inp.wind_source,
            "Fp": _round(Fp, 3), "E_wall_psf": _round(E_wall, 2),
            "E_psf": _round(E, 2), "FEv": _round(FEv, 3),
        },
        "interaction": {
            "P_design_lbf_per_ft": _round(Pu_g, 1), "M_design_lbin_per_ft": _round(Mu_g, 0),
            "delta_in": _round(g["delta"], 4),
            "P_balance_lbf_per_ft": _round(P_bal, 0), "M_balance_lbin_per_ft": _round(M_bal, 0),
            "c_balance_in": _round(c_bal, 3),
        },
        "combo_utilizations": combo_utils,
        "governing_combo": g["combo"]["name"],
        "shear": {
            "w_lat_psf": _round(w_lat, 2), "combo": shear_combo["name"],
            "V_lbf_per_ft": _round(V, 1), "Anv_in2_per_ft": _round(Anv, 1),
            "MoverVdv": 0.0, "Vnm_lbf_per_ft": _round(Vnm, 0),
            "Vn_cap_lbf_per_ft": _round(Vn_cap, 0), "gamma_g": gamma_g,
            "phi": PHI_SHEAR, "phiVn_lbf_per_ft": _round(phi_Vn, 0),
        },
        "summary": {"overall": overall},
    }


# ============================================================
# Section-design mode — TMS 402-16, out-of-plane + in-plane + biaxial
# Verified against Tedds "Masonry wall section design (TMS-16)".
# ============================================================
def _phi_Mn_oop(fm, b, d, t, As, fy, Pu):
    """Out-of-plane design flexural strength at axial Pu (single centered bar)."""
    a = (As * fy + Pu) / (0.80 * fm * b)
    Mn = (As * fy + Pu) * (d - a / 2.0)
    return PHI_FLEXURE_AXIAL * Mn


def _inplane_interaction(fm, t, L_in, bar_dia, bar_spacing_in, fy, Es, P_target_lb):
    """In-plane flexural capacity by strain compatibility with distributed
    vertical bars. Returns (phi*Mn at P_target, Mmax, c_neutral_axis_in).

    Bars are placed at uniform spacing across the length; section bends about
    the vertical (in-plane) axis. Compression block width = wall thickness t.
    """
    abar = math.pi * bar_dia ** 2 / 4.0
    n = max(int(round(L_in / bar_spacing_in)), 2)
    # bar positions from the compression face (x measured along length)
    x = [(i + 0.5) * (L_in / n) for i in range(n)]
    beta1 = 0.80
    eps_y = fy / Es

    def _forces(c):
        Cc = 0.80 * fm * beta1 * c * t            # masonry compression (lb)
        Fs = 0.0
        M_steel = 0.0
        for xi in x:
            eps = EPS_MU * (c - xi) / c
            fsi = max(min(Es * eps, fy), -fy)
            Fsi = fsi * abar
            Fs += Fsi
            M_steel += Fsi * (L_in / 2.0 - xi)     # about centroid
        P = Cc + Fs
        M = Cc * (L_in / 2.0 - beta1 * c / 2.0) + M_steel
        return P, M

    # bisection on c to match P_target
    lo, hi = 0.01, L_in
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        P, _M = _forces(mid)
        if P < P_target_lb:
            lo = mid
        else:
            hi = mid
    c_sol = 0.5 * (lo + hi)
    _, Mn = _forces(c_sol)
    # Mmax ~ moment at balance-ish (scan)
    Mmax = 0.0
    for k in range(1, 100):
        cc = k / 100.0 * L_in
        _, Mk = _forces(cc)
        Mmax = max(Mmax, Mk)
    return PHI_FLEXURE_AXIAL * Mn, PHI_FLEXURE_AXIAL * Mmax, c_sol


def _masonry_section_design(inp: MasonryWallInputs) -> dict:
    t = inp.thickness_in
    b = 12.0
    L_ft = inp.panel_length_ft
    L_in = L_ft * 12.0
    fm = inp.fm_psi
    d = inp.d_in if inp.d_in else t / 2.0
    heff_y = (inp.heff_y_ft or inp.wall_height_ft) * 12.0
    heff_x = (inp.heff_x_ft or inp.wall_height_ft) * 12.0

    Em = 900.0 * fm
    fr = MODULUS_OF_RUPTURE.get(("normal", inp.grout, inp.mortar_class), 100)
    eps_y = inp.fy_psi / inp.Es_psi
    n_modular = inp.Es_psi / Em
    beta1 = 0.80

    # Section properties
    A = b * t                                    # per ft, out-of-plane / axial
    I = b * t ** 3 / 12.0
    S = 2.0 * I / t
    r = math.sqrt(I / A)
    Ix = t * L_in ** 3 / 12.0                    # in-plane gross
    rx = math.sqrt(Ix / (A * L_ft))

    # Reinforcement
    bar_dia = BAR_DIA.get(inp.bar_size, 0.625)
    As = math.pi * bar_dia ** 2 / 4.0 * 12.0 / inp.bar_spacing_in     # in^2/ft
    h_dia = BAR_DIA.get(inp.h_bar_size, 0.375)
    Av = inp.h_bar_legs * math.pi * h_dia ** 2 / 4.0 * 12.0 / inp.h_bar_spacing_in  # in^2/ft

    # Factored design forces
    P = inp.P_axial_plf
    My_total = inp.My_lbft_per_ft + P * (inp.ecc_in / 12.0)   # lb-ft/ft
    Vy = inp.Vy_lbf_per_ft
    Mx = inp.Mx_lbft
    Vx = inp.Vx_lbf

    # ---- Out-of-plane axial (Eqn 9-15) ----
    slender_y = heff_y / r
    Pn = 0.80 * (0.80 * fm * (A - As) + inp.fy_psi * As) * (1.0 - (heff_y / (140.0 * r)) ** 2) \
        if slender_y <= 99 else 0.80 * (0.80 * fm * (A - As) + inp.fy_psi * As) * (70.0 * r / heff_y) ** 2
    phi_Pn = PHI_FLEXURE_AXIAL * Pn
    u_oop_axial = P / phi_Pn
    u_oop_stress = (P / A) / (0.20 * fm)

    # ---- Out-of-plane P-delta (moment magnifier, Eqn 9-27/28/29) ----
    Mcr = S * fr / 12.0                           # lb-ft/ft
    Ieff = inp.Ieff_factor * I
    if inp.pdelta_method == "magnifier":
        Pe = math.pi ** 2 * Em * Ieff / heff_y ** 2     # lb/ft
        delta_mag = 1.0 / (1.0 - min(P / Pe, 0.9999)) if Pe > 0 else 1.0
        Mu_oop = delta_mag * abs(My_total)              # lb-ft/ft
    else:
        Pe = None
        M0 = abs(My_total) * 12.0                       # lb-in
        Mu_in = M0
        for _ in range(50):
            delta = 5.0 * Mu_in * heff_y ** 2 / (48.0 * Em * I)
            new = M0 + P * delta
            if abs(new - Mu_in) < 1e-6:
                Mu_in = new
                break
            Mu_in = new
        delta_mag = Mu_in / M0 if M0 else 1.0
        Mu_oop = Mu_in / 12.0

    phiMn_oop = _phi_Mn_oop(fm, b, d, t, As, inp.fy_psi, P) / 12.0    # lb-ft/ft
    u_oop_flex = abs(Mu_oop) / phiMn_oop if phiMn_oop > 0 else 0.0

    # ---- Out-of-plane shear (cap varies 4..6 with M/(V*d), §9.3.4.1.2) ----
    Anv = b * d
    ratio_y = abs(Mu_oop) * 12.0 / (Vy * d) if Vy > 0 else 0.0
    cap_y = 6.0 if ratio_y <= 0.25 else (4.0 if ratio_y >= 1.0
                                         else 6.0 + (4.0 - 6.0) * (ratio_y - 0.25) / 0.75)
    # TMS 402-16 §9.3.4.1.2: gamma_g = 0.75 for partially grouted, 1.0 fully
    gamma_g = 1.0 if inp.grout == "full" else 0.75
    Vnm_oop = (4.0 - 1.75 * min(ratio_y, 1.0)) * Anv * math.sqrt(fm) + 0.25 * P
    Vn_oop = min(Vnm_oop, cap_y * Anv * math.sqrt(fm)) * gamma_g
    phiVn_oop = PHI_SHEAR * Vn_oop
    u_oop_shear = Vy / phiVn_oop if phiVn_oop > 0 else 0.0

    # ---- In-plane axial ----
    Pin = P * L_ft
    din = L_ft - inp.unit_length_in / 4.0 / 12.0      # ft
    Anv_in = din * 12.0 * t                            # in^2
    slender_x = heff_x / rx
    As_total_in = As * L_ft
    Pn_in = 0.80 * (0.80 * fm * (Anv_in - As_total_in) + inp.fy_psi * As_total_in) * (1.0 - (heff_x / (140.0 * rx)) ** 2) \
        if slender_x <= 99 else 0.80 * (0.80 * fm * (Anv_in - As_total_in) + inp.fy_psi * As_total_in) * (70.0 * rx / heff_x) ** 2
    phi_Pn_in = PHI_FLEXURE_AXIAL * Pn_in
    u_in_axial = Pin / phi_Pn_in

    # ---- In-plane flexure (distributed-bar interaction) ----
    Mlimit_in_lbin, Mmax_in_lbin, cNA_in = _inplane_interaction(
        fm, t, L_in, bar_dia, inp.bar_spacing_in, inp.fy_psi, inp.Es_psi, Pin)
    Mlimit_in = Mlimit_in_lbin / 12.0                  # lb-ft
    u_in_flex = abs(Mx) / Mlimit_in if Mlimit_in > 0 else 0.0

    # ---- In-plane shear (§9.3.4.1.2) + reinforcement ----
    din_in = din * 12.0
    ratio_x = abs(Mx) * 12.0 / (Vx * din_in) if Vx > 0 else 0.0
    Vnm_in = (4.0 - 1.75 * min(ratio_x, 1.0)) * Anv_in * math.sqrt(fm) + 0.25 * Pin
    Vns_in = 0.5 * Av * inp.fy_hori_psi * din          # Av per ft * din ft
    # Cap interpolates 6->4 * Anv*sqrt(f'm) as M/(V*dv) goes 0.25 -> 1.0
    # (Eqs. 9-18/9-19) — same rule as out-of-plane above.
    cap_x = 6.0 if ratio_x <= 0.25 else (4.0 if ratio_x >= 1.0
                                         else 6.0 + (4.0 - 6.0) * (ratio_x - 0.25) / 0.75)
    Vn_in = min(Vnm_in + Vns_in, cap_x * Anv_in * math.sqrt(fm)) * gamma_g
    phiVn_in = PHI_SHEAR * Vn_in
    u_in_shear = Vx / phiVn_in if phiVn_in > 0 else 0.0

    # ---- In-plane shear friction (§9.3.6.5) ----
    ratio_vf = abs(Mx) / (Vx * din) if Vx > 0 else 0.0   # ft-based per Tedds
    Anc = A * (cNA_in / 12.0)                              # A(in^2/ft) * c(ft)
    Vnf1 = max(inp.mu_friction * (As * din * inp.fy_psi + Pin), 0.0)
    Vnf2 = max(0.42 * fm * Anc, 0.0)
    if ratio_vf <= 0.5:
        Vnf = Vnf1
    elif ratio_vf >= 1.0:
        Vnf = Vnf2
    else:
        Vnf = Vnf1 + (Vnf2 - Vnf1) * (ratio_vf - 0.5) / 0.5
    phiVnf = PHI_SHEAR * Vnf
    u_in_friction = Vx / phiVnf if phiVnf > 0 else 0.0

    # ---- Biaxial bending ----
    u_biaxial = math.sqrt(u_oop_flex ** 2 + u_in_flex ** 2)

    def _r(x, n=1):
        return round(x, n)

    results = {
        "Out-of-plane Axial (P / phi*Pn)": {
            "Actual": _r(P / 1000, 3), "Allowed": _r(phi_Pn / 1000, 1),
            "Result": _r(u_oop_axial * 100, 1), "LDF": "-", "Combo": "section"},
        "Out-of-plane Axial Stress": {
            "Actual": _r(P / A, 1), "Allowed": _r(0.20 * fm, 1),
            "Result": _r(u_oop_stress * 100, 1), "LDF": "-", "Combo": "section"},
        "Out-of-plane Flexure (M / phi*Mn)": {
            "Actual": _r(abs(Mu_oop) / 1000, 3), "Allowed": _r(phiMn_oop / 1000, 3),
            "Result": _r(u_oop_flex * 100, 1), "LDF": "-", "Combo": "section"},
        "Out-of-plane Shear (V / phi*Vn)": {
            "Actual": _r(Vy / 1000, 3), "Allowed": _r(phiVn_oop / 1000, 3),
            "Result": _r(u_oop_shear * 100, 1), "LDF": "-", "Combo": "section"},
        "In-plane Axial (P / phi*Pn)": {
            "Actual": _r(Pin / 1000, 1), "Allowed": _r(phi_Pn_in / 1000, 1),
            "Result": _r(u_in_axial * 100, 1), "LDF": "-", "Combo": "section"},
        "In-plane Flexure (M / phi*Mn)": {
            "Actual": _r(abs(Mx) / 1000, 3), "Allowed": _r(Mlimit_in / 1000, 1),
            "Result": _r(u_in_flex * 100, 1), "LDF": "-", "Combo": "section"},
        "In-plane Shear (V / phi*Vn)": {
            "Actual": _r(Vx / 1000, 3), "Allowed": _r(phiVn_in / 1000, 1),
            "Result": _r(u_in_shear * 100, 1), "LDF": "-", "Combo": "section"},
        "In-plane Shear Friction (V / phi*Vnf)": {
            "Actual": _r(Vx / 1000, 3), "Allowed": _r(phiVnf / 1000, 1),
            "Result": _r(u_in_friction * 100, 1), "LDF": "-", "Combo": "§9.3.6.5"},
        "Biaxial Bending": {
            "Actual": _r(u_biaxial, 3), "Allowed": 1.0,
            "Result": _r(u_biaxial * 100, 1), "LDF": "-", "Combo": "section"},
    }
    overall = "PASS" if all(c["Result"] is None or c["Result"] <= 100.0
                            for c in results.values()) else "FAIL"

    return {
        "name": None, "system": "Masonry",
        "application": "Masonry Wall Section (in-plane + out-of-plane)",
        "size": f'{t:.3f}" CMU, #{inp.bar_size} @ {inp.bar_spacing_in:g}"',
        "method": "TMS 402-16 Strength Design (biaxial section)",
        "results": results,
        "section": {
            "t_in": t,
            "A_in2_per_ft": _r(A, 1), "I_oop_in4_per_ft": _r(I, 1), "S_in3_per_ft": _r(S, 1),
            "r_in": _r(r, 2), "Ix_in4": _r(Ix, 0), "rx_in": _r(rx, 1),
            "As_in2_per_ft": _r(As, 2), "Av_in2_per_ft": _r(Av, 3),
            "Em_psi": _r(Em, 0), "fr_psi": fr, "d_in": _r(d, 3), "din_ft": _r(din, 1),
            "Mcr_lbft_per_ft": _r(Mcr, 1),
            "w_self_psf": _r(self_weight_psf(inp), 2),
            "modular_ratio_n": _r(n_modular, 3),
            "slenderness_kh_r": _r(slender_y, 2),
        },
        "pdelta": {
            "method": inp.pdelta_method, "Pe_lbf_per_ft": _r(Pe, 0) if Pe else None,
            "Ieff_in4_per_ft": _r(Ieff, 1), "magnification": _r(delta_mag, 4),
            "My_total_lbft_per_ft": _r(My_total, 1), "Mu_oop_lbft_per_ft": _r(Mu_oop, 1),
        },
        "interaction": {
            "oop_phiMn_lbft_per_ft": _r(phiMn_oop, 1),
            "in_phiMlimit_lbft": _r(Mlimit_in, 0), "in_Mmax_lbft": _r(Mmax_in_lbin / 12.0, 0),
            "in_cNA_ft": _r(cNA_in / 12.0, 2),
        },
        "shear_oop": {
            "V_lbf_per_ft": _r(Vy, 1), "Anv_in2_per_ft": _r(Anv, 1),
            "MoverVdv": _r(ratio_y, 3), "Vnm_lbf_per_ft": _r(Vnm_oop, 0),
            "cap_coeff": _r(cap_y, 2), "gamma_g": gamma_g, "phi": PHI_SHEAR,
            "phiVn_lbf_per_ft": _r(phiVn_oop, 0),
        },
        "shear_inplane": {
            "V_lbf": _r(Vx, 0), "Anv_in2": _r(Anv_in, 0), "dv_ft": _r(din, 2),
            "MoverVdv": _r(ratio_x, 3), "Vnm_lbf": _r(Vnm_in, 0),
            "Vns_lbf": _r(Vns_in, 0), "cap_coeff": _r(cap_x, 2),
            "Av_in2_per_ft": _r(Av, 3), "gamma_g": gamma_g, "phi": PHI_SHEAR,
            "phiVn_lbf": _r(phiVn_in, 0),
        },
        "shear_friction": {"Vnf1_lbf": _r(Vnf1, 0), "Vnf2_lbf": _r(Vnf2, 0),
                           "mu": inp.mu_friction, "phiVnf_lbf": _r(phiVnf, 0)},
        "summary": {"overall": overall,
                    "max_utilization": _r(max(c["Result"] for c in results.values()
                                              if c["Result"] is not None) / 100, 3)},
    }


# ============================================================
# Self-test against the Tedds reference (Pelican House A-1599)
# ============================================================
if __name__ == "__main__":
    inp = MasonryWallInputs()
    r = check_masonry_wall(inp)
    s = r["section"]
    print("MASONRY WALL PANEL — TMS 402-16 SD — Tedds validation")
    print(f"  w_self = {s['w_self_psf']} psf (expect 81.19)")
    print(f"  A={s['A_in2_per_ft']} I={s['I_in4_per_ft']} S={s['S_in3_per_ft']} r={s['r_in']}")
    print(f"  fr={s['fr_psi']} Em={s['Em_psi']} n={s['modular_ratio_n']} Mcr={s['Mcr_lbin_per_ft']}")
    print(f"  slenderness kh/r = {s['slenderness_kh_r']} (expect 65.42)")
    print(f"  E_wall = {r['loads']['E_wall_psf']} psf (expect 32.5)")
    print(f"  governing: {r['governing_combo']}")
    print(f"  M/phiMn flexure util = {r['results']['Flexure (M / φMn)']['Result']}% (expect 28.0)")
    print(f"  interaction: {r['interaction']}")
    print(f"  combos: {r['combo_utilizations']}")
    print(f"  OVERALL: {r['summary']['overall']}")
