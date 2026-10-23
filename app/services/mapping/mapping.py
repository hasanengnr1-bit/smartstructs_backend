import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Literal, List

from services.calculators.utils.lumber import normalize_member_size
from services.calculators.shear_wall_calc import ShearWallInputs, compute_perforated_co
from services.calculators.lateral_calc import (
    compute_lateral_loads,
    story_number,
    candc_wall_pressure,
)


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


# ShearWallInputs is imported from services.calculators.shear_wall_calc —
# a local duplicate previously shadowed it and drifted out of sync.


# ---------------------------------------------------------------- helpers
def _normalize_grade(g: str | None) -> str:
    """Schema grades ('No.1','SS','Stud') -> engine grades ('No. 1','SS','Stud')."""
    if not g:
        return "No. 2"
    g = g.strip()
    table = {"No.1": "No. 1", "No.2": "No. 2", "No.3": "No. 3"}
    return table.get(g, g)


def _lumber(item: Dict[str, Any], default_species="DF-L", default_grade="No. 2"):
    spec = item.get("lumber_spec") or {}
    species = spec.get("species") or default_species
    grade = _normalize_grade(spec.get("grade")) or default_grade
    return species, grade


def _member_size(raw: Any, default: str) -> str:
    return normalize_member_size(raw or default) or default


def _support_species(item: Dict[str, Any]) -> str:
    """Map support_material enum to a species the engine knows for bearing checks.

    When no support material is called out, assume the member bears on framing of
    its own species rather than defaulting to the weakest (SPF) — otherwise two
    identical DF-L members report different Fc⊥ (625 vs 425) purely because one
    happened to have support_material extracted and the other did not.
    """
    wood = {"DF-L", "SP", "SPF", "HF"}
    sm = item.get("support_material")
    if sm in wood:
        return sm
    own = (item.get("lumber_spec") or {}).get("species")
    if own in wood:
        return own
    return "SPF"


def _wind_psf(env: Dict[str, Any], effective_area_sf: Optional[float] = None) -> float:
    """Out-of-plane wind pressure on a member (psf).
    Precedence: user design_loads override > C&C pressure (ASCE 7 Ch. 30,
    zone 4) when an effective wind area is known > MWFRS-derived wall
    pressure as a legacy fallback."""
    dl = env.get("design_loads") or {}
    override = _user_load(env, "wall_wind_psf") if isinstance(dl, dict) else None
    if override:
        return override
    meta = (env.get("lateral") or {}).get("computed") or {}
    wind = meta.get("wind") or {}
    qh = None
    for d in ("X", "Y"):
        q = (wind.get(d) or {}).get("qh_psf")
        if q:
            qh = max(qh or 0.0, q)
    if qh:
        if effective_area_sf:
            return round(candc_wall_pressure(qh, effective_area_sf, zone=4), 2)
        return round(qh * (0.85 * 0.8 + 0.18), 2)
    return 0.0


def _parse_l_over_n(s: str | None) -> float | None:
    """'L/240' -> 240. Returns None if unparseable."""
    if not s:
        return None
    try:
        if "/" in s:
            return float(s.split("/")[1])
        return float(s)
    except Exception:
        return None


def _parse_pitch(pitch_str: str | None) -> float:
    """'6/12' or '4:12' -> rise value (6.0 or 4.0). Returns 0.0 if unparseable."""
    if not pitch_str:
        return 0.0
    try:
        if "/" in pitch_str:
            return float(pitch_str.split("/")[0])
        elif ":" in pitch_str:
            return float(pitch_str.split(":")[0])
        else:
            return float(pitch_str)
    except Exception:
        return 0.0


def _ewp(item: Dict[str, Any]):
    """Extract EWP species override and property overrides from schema item."""
    ewp = item.get("ewp_spec") or {}
    species_override = ewp.get("product_type")
    overrides = {
        k: ewp.get(k)
        for k in ("Fb_psi", "Fv_psi", "Fc_perp_psi", "E_psi", "E_min_psi")
        if ewp.get(k) is not None
    } or None
    return species_override, overrides


def _defl(item: Dict[str, Any]):
    """Extract deflection criteria denominators from schema item."""
    dc = item.get("deflection_criteria") or {}
    return (
        _parse_l_over_n(dc.get("live_load_limit")),
        _parse_l_over_n(dc.get("total_load_limit")),
    )


def _bearing(item: Dict[str, Any]):
    """Extract bearing lengths (default + optional left/right split)."""
    brg_default = float(item.get("available_support_bearing_in") or 3.5)
    brg_left = item.get("available_support_bearing_left_in")
    brg_right = item.get("available_support_bearing_right_in")
    brg_R1 = float(brg_left) if brg_left not in (None, "") else None
    brg_R2 = float(brg_right) if brg_right not in (None, "") else None
    return brg_default, brg_R1, brg_R2


def _require_fields(item: Dict[str, Any], fields: Dict[str, str], label: str) -> None:
    """Raise ValueError listing every critical field that is missing or empty.

    `fields` maps schema key -> human-readable name shown in the error,
    e.g. {"clear_span_ft": "Clear Span (ft)"}.
    """
    missing = [
        display
        for key, display in fields.items()
        if item.get(key) in (None, "", 0, 0.0)
    ]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(
            f"{label}: cannot run analysis — "
            f"missing required input(s): {joined}. "
            f"Please fill in the missing value(s) and re-run."
        )


def _user_load(env: Dict[str, Any], key: str) -> float | None:
    """User-defined design load (psf) from env['design_loads']. None if not set."""
    loads = env.get("design_loads") or {}
    value = loads.get(key)
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _roof_loads(item: Dict[str, Any], env: Dict[str, Any]):
    """Roof D/Lr/S (psf). Precedence: user-defined > plan-extracted > code default."""
    user_D = _user_load(env, "roof_dead_load_psf")
    user_Lr = _user_load(env, "roof_live_load_psf")
    user_S = _user_load(env, "roof_snow_load_psf")
    D = user_D if user_D is not None else float(item.get("roof_dead_load_psf") or 15)
    Lr = user_Lr if user_Lr is not None else float(item.get("roof_live_load_psf") or 20)
    S = user_S if user_S is not None else float(item.get("roof_snow_load_psf") or 0)
    return D, Lr, S


def _floor_dead(item: Dict[str, Any], env: Dict[str, Any]) -> float:
    """Floor D (psf). Precedence: user/extracted design loads > per-member > 10 psf default."""
    user_D = _user_load(env, "floor_dead_load_psf")
    if user_D is not None:
        return user_D
    return float(item.get("dead_load_psf") or 10)


def stud_wall_line_loads(item: Dict[str, Any], env: Dict[str, Any]) -> Dict[str, float]:
    """Axial line loads (plf of wall) at the base of a bearing stud wall:
    roof D/Lr/S and floor L over the tributary width, plus wall self-weight.
    Single source of truth for the stud check (map_stud_wall) and the footing
    load cascade (_store_member_reactions / map_continuous_footing), so the
    two can never drift apart. check_stud consumes plf-of-wall and converts
    to per-stud internally via spacing."""
    trib_w = float(item.get("tributary_width_ft") or 0)
    wall_ht = float(item.get("stud_height_ft") or 10)
    wall_dl = float(item.get("wall_dead_load_psf") or 8)
    roof_dl = _user_load(env, "roof_dead_load_psf")
    roof_dl = roof_dl if roof_dl is not None else 15.0
    roof_ll = _user_load(env, "roof_live_load_psf")
    roof_ll = roof_ll if roof_ll is not None else 20.0
    floor_ll = _user_load(env, "floor_live_load_psf") or 0.0
    try:
        snow_raw = item.get("roof_snow_load_psf")
        snow = (
            float(snow_raw)
            if snow_raw not in (None, "")
            else float(env.get("hazard_snow_psf") or 0)
        )
    except (TypeError, ValueError):
        snow = float(env.get("hazard_snow_psf") or 0)
    return {
        "D_plf": roof_dl * trib_w + wall_ht * wall_dl,
        "L_plf": floor_ll * trib_w,
        "Lr_plf": roof_ll * trib_w,
        "S_plf": snow * trib_w,
    }


def _footing_info(env: Dict[str, Any]):
    """Pull FootingProjectInfo fields from env."""
    fi = env.get("footing_project_info") or {}
    q_allow = float(fi.get("soil_bearing_pressure_psf") or 2000)
    fc_ftg = float(fi.get("concrete_fc_footings_psi") or 2500)
    fc_slab = float(fi.get("concrete_fc_slab_psi") or 2500)
    return q_allow, fc_ftg, fc_slab


# ================================================================
#  WALL  —  stud_calc.check_stud
# ================================================================
def map_stud_wall(item: Dict[str, Any], env: Dict[str, Any]) -> Dict[str, Any]:
    """schema.StudWall -> stud_calc.check_stud kwargs"""
    label = item.get("zone") or "Stud Wall"
    _require_fields(item, {"stud_height_ft": "Wall Height (ft)"}, label)
    species, grade = _lumber(item)
    ewp_species, ewp_overrides = _ewp(item)
    if ewp_species:
        species = ewp_species
    sc = item.get("service_conditions") or None
    defl_LL, _ = _defl(item)
    spacing = float(item.get("spacing_in") or 16)
    loads = stud_wall_line_loads(item, env)
    axial_lbs = float(item.get("axial_load_lbs") or 0)

    wall_len = float(item.get("wall_length_ft") or 0) or 1.0
    extra_dl_plf = (axial_lbs / wall_len) if axial_lbs else 0.0

    # Effective wind area for C&C: span x max(spacing, span/3) — ASCE 7 §26.2
    stud_h = float(item.get("stud_height_ft") or 0.0)
    eff_area = stud_h * max(spacing / 12.0, stud_h / 3.0)

    return dict(
        name=item.get("zone") or "Stud",
        system="Wall",
        application="Stud",
        size=_member_size(item.get("stud_size"), "2x6"),
        species=species,
        grade=grade,
        length_ft=stud_h,
        spacing_in=spacing,
        wind_psf=_wind_psf(env, effective_area_sf=eff_area),
        D_plf=loads["D_plf"] + extra_dl_plf,
        L_plf=loads["L_plf"],
        Lr_plf=loads["Lr_plf"],
        S_plf=loads["S_plf"],
        support_species=_support_species(item),
        unbraced_weak_ft=1.0,
        ecc_ratio=0.1667,
        ewp_overrides=ewp_overrides,
        plies=int(item.get("number_of_plies") or 1),
        service_conditions=sc,
        defl_ratio=int(defl_LL) if defl_LL else 360,
    )


# ================================================================
#  ROOF  —  horizontal_calc.check_horizontal
# ================================================================
def map_roof_rafter(item: Dict[str, Any], env: Dict[str, Any]) -> Dict[str, Any]:
    """schema.RoofRafter -> horizontal_calc.check_horizontal kwargs"""
    label = item.get("zone") or "Roof Rafter"
    _require_fields(item, {"clear_span_ft": "Clear Span (ft)"}, label)
    species, grade = _lumber(item)
    pitch = _parse_pitch(item.get("roof_pitch"))
    spacing = float(item.get("spacing_in") or 16)
    overhang_ft = float(item.get("overhang_in") or 0) / 12.0
    brg_default, brg_R1, brg_R2 = _bearing(item)
    sc = item.get("service_conditions") or None
    defl_LL, defl_TL = _defl(item)
    ewp_species, ewp_overrides = _ewp(item)
    if ewp_species:
        species = ewp_species
    D_psf, Lr_psf, S_psf = _roof_loads(item, env)

    return dict(
        name=item.get("zone") or "Rafter",
        system="Roof",
        application="Joist",
        size=_member_size(item.get("size"), "2x10"),
        species=species,
        grade=grade,
        plies=int(item.get("number_of_plies") or 1),
        clear_span_ft=float(item.get("clear_span_ft") or 0.0),
        spacing_in=spacing,
        trib_width_ft=spacing / 12.0,
        D_psf=D_psf,
        L_psf=0.0,
        Lr_psf=Lr_psf,
        S_psf=S_psf,
        support_species=_support_species(item),
        bearing_area_length_in=brg_default,
        L_ol_ft=overhang_ft,
        L_or_ft=0.0,
        pitch=pitch,
        defl_LL_denom=defl_LL,
        defl_TL_denom=defl_TL,
        service_conditions=sc,
        ewp_overrides=ewp_overrides,
        bearing_R1_in=brg_R1,
        bearing_R2_in=brg_R2,
    )


def map_ceiling_joist(item: Dict[str, Any], env: Dict[str, Any]) -> Dict[str, Any]:
    """schema.CeilingJoist -> horizontal_calc.check_horizontal kwargs"""
    label = item.get("zone") or "Ceiling Joist"
    _require_fields(item, {"clear_span_ft": "Clear Span (ft)"}, label)
    species, grade = _lumber(item)
    spacing = float(item.get("spacing_in") or 16)
    brg_default, brg_R1, brg_R2 = _bearing(item)
    sc = item.get("service_conditions") or None
    defl_LL, defl_TL = _defl(item)

    return dict(
        name=item.get("zone") or "Ceiling Joist",
        system="Ceiling",
        application="Joist",
        size=_member_size(item.get("size"), "2x6"),
        species=species,
        grade=grade,
        plies=int(item.get("number_of_plies") or 1),
        clear_span_ft=float(item.get("clear_span_ft") or 0.0),
        spacing_in=spacing,
        trib_width_ft=spacing / 12.0,
        D_psf=float(item.get("ceiling_dead_load_psf") or 5),
        L_psf=float(item.get("attic_live_load_psf") or 10),
        Lr_psf=0.0,
        S_psf=0.0,
        support_species=_support_species(item),
        bearing_area_length_in=brg_default,
        L_ol_ft=0.0,
        L_or_ft=0.0,
        pitch=0.0,
        defl_LL_denom=defl_LL,
        defl_TL_denom=defl_TL,
        service_conditions=sc,
        ewp_overrides=None,
        bearing_R1_in=brg_R1,
        bearing_R2_in=brg_R2,
    )


def map_ridge_beam(item: Dict[str, Any], env: Dict[str, Any]) -> Dict[str, Any]:
    """schema.RidgeBeam -> horizontal_calc.check_horizontal kwargs"""
    label = item.get("zone") or "Ridge Beam"
    _require_fields(item, {"clear_span_ft": "Clear Span (ft)"}, label)
    species, grade = _lumber(item)
    ewp_species, ewp_overrides = _ewp(item)
    if ewp_species:
        species = ewp_species
    brg_default, brg_R1, brg_R2 = _bearing(item)
    sc = item.get("service_conditions") or None
    defl_LL, defl_TL = _defl(item)
    trib = float(item.get("tributary_width_ft") or 6)

    D_psf, Lr_psf, S_psf = _roof_loads(item, env)

    return dict(
        name=item.get("zone") or "Ridge Beam",
        system="Roof",
        application="Beam",
        size=_member_size(item.get("size"), "4x8"),
        species=species,
        grade=grade,
        plies=int(item.get("number_of_plies") or 1),
        clear_span_ft=float(item.get("clear_span_ft") or 0.0),
        spacing_in=0,
        trib_width_ft=trib,
        D_psf=D_psf,
        L_psf=0.0,
        Lr_psf=Lr_psf,
        S_psf=S_psf,
        support_species=_support_species(item),
        bearing_area_length_in=brg_default,
        L_ol_ft=0.0,
        L_or_ft=0.0,
        pitch=0.0,
        defl_LL_denom=defl_LL,
        defl_TL_denom=defl_TL,
        service_conditions=sc,
        ewp_overrides=ewp_overrides,
        bearing_R1_in=brg_R1,
        bearing_R2_in=brg_R2,
    )


def map_hip_valley_rafter(item: Dict[str, Any], env: Dict[str, Any]) -> Dict[str, Any]:
    """schema.HipValleyRafter -> horizontal_calc.check_horizontal kwargs"""
    label = item.get("zone") or "Hip/Valley Rafter"
    _require_fields(item, {"clear_span_ft": "Clear Span (ft)"}, label)
    species, grade = _lumber(item)
    ewp_species, ewp_overrides = _ewp(item)
    if ewp_species:
        species = ewp_species
    pitch = _parse_pitch(item.get("roof_pitch"))
    brg_default, brg_R1, brg_R2 = _bearing(item)
    sc = item.get("service_conditions") or None
    defl_LL, defl_TL = _defl(item)
    trib = float(item.get("tributary_width_ft") or 6)
    member_type = item.get("member_type") or "hip"

    D_psf, Lr_psf, S_psf = _roof_loads(item, env)

    return dict(
        name=item.get("zone") or f"{member_type.title()} Rafter",
        system="Roof",
        application="Beam",
        size=_member_size(item.get("size"), "2x10"),
        species=species,
        grade=grade,
        plies=int(item.get("number_of_plies") or 1),
        clear_span_ft=float(item.get("clear_span_ft") or 0.0),
        spacing_in=0,
        trib_width_ft=trib,
        D_psf=D_psf,
        L_psf=0.0,
        Lr_psf=Lr_psf,
        S_psf=S_psf,
        support_species=_support_species(item),
        bearing_area_length_in=brg_default,
        L_ol_ft=0.0,
        L_or_ft=0.0,
        pitch=pitch,
        defl_LL_denom=defl_LL,
        defl_TL_denom=defl_TL,
        service_conditions=sc,
        ewp_overrides=ewp_overrides,
        bearing_R1_in=brg_R1,
        bearing_R2_in=brg_R2,
    )


def map_roof_drop_beam(item: Dict[str, Any], env: Dict[str, Any]) -> Dict[str, Any]:
    """schema.RoofDropBeam -> horizontal_calc.check_horizontal kwargs"""
    label = item.get("zone") or "Roof Drop Beam"
    _require_fields(item, {"clear_span_ft": "Clear Span (ft)"}, label)
    species, grade = _lumber(item)
    ewp_species, ewp_overrides = _ewp(item)
    if ewp_species:
        species = ewp_species
    pitch = _parse_pitch(item.get("roof_pitch"))
    brg_default, brg_R1, brg_R2 = _bearing(item)
    sc = item.get("service_conditions") or None
    defl_LL, defl_TL = _defl(item)
    trib = float(item.get("tributary_width_ft") or 6)

    D_psf, Lr_psf, S_psf = _roof_loads(item, env)

    return dict(
        name=item.get("zone") or "Roof Drop Beam",
        system="Roof",
        application="Beam",
        size=_member_size(item.get("size"), "4x8"),
        species=species,
        grade=grade,
        plies=int(item.get("number_of_plies") or 1),
        clear_span_ft=float(item.get("clear_span_ft") or 0.0),
        spacing_in=0,
        trib_width_ft=trib,
        D_psf=D_psf,
        L_psf=0.0,
        Lr_psf=Lr_psf,
        S_psf=S_psf,
        support_species=_support_species(item),
        bearing_area_length_in=brg_default,
        L_ol_ft=0.0,
        L_or_ft=0.0,
        pitch=pitch,
        defl_LL_denom=defl_LL,
        defl_TL_denom=defl_TL,
        service_conditions=sc,
        ewp_overrides=ewp_overrides,
        bearing_R1_in=brg_R1,
        bearing_R2_in=brg_R2,
    )


def map_roof_flush_beam(item: Dict[str, Any], env: Dict[str, Any]) -> Dict[str, Any]:
    """schema.RoofFlushBeam -> horizontal_calc.check_horizontal kwargs"""
    label = item.get("zone") or "Roof Flush Beam"
    _require_fields(item, {"clear_span_ft": "Clear Span (ft)"}, label)
    species, grade = _lumber(item)
    ewp_species, ewp_overrides = _ewp(item)
    if ewp_species:
        species = ewp_species
    brg_default, brg_R1, brg_R2 = _bearing(item)
    sc = item.get("service_conditions") or None
    defl_LL, defl_TL = _defl(item)
    trib = float(item.get("tributary_width_ft") or 6)

    D_psf, Lr_psf, S_psf = _roof_loads(item, env)

    return dict(
        name=item.get("zone") or "Roof Flush Beam",
        system="Roof",
        application="Beam",
        size=_member_size(item.get("size"), "4x8"),
        species=species,
        grade=grade,
        plies=int(item.get("number_of_plies") or 1),
        clear_span_ft=float(item.get("clear_span_ft") or 0.0),
        spacing_in=0,
        trib_width_ft=trib,
        D_psf=D_psf,
        L_psf=0.0,
        Lr_psf=Lr_psf,
        S_psf=S_psf,
        support_species=_support_species(item),
        bearing_area_length_in=brg_default,
        L_ol_ft=0.0,
        L_or_ft=0.0,
        pitch=0.0,
        defl_LL_denom=defl_LL,
        defl_TL_denom=defl_TL,
        service_conditions=sc,
        ewp_overrides=ewp_overrides,
        bearing_R1_in=brg_R1,
        bearing_R2_in=brg_R2,
    )


# ================================================================
#  FLOOR  —  horizontal_calc.check_horizontal
# ================================================================
def map_floor_joist(item: Dict[str, Any], env: Dict[str, Any]) -> Dict[str, Any]:
    """schema.FloorJoist -> horizontal_calc.check_horizontal kwargs"""
    label = item.get("zone") or "Floor Joist"
    _require_fields(item, {"clear_span_ft": "Clear Span (ft)"}, label)
    species, grade = _lumber(item)
    ewp_species, ewp_overrides = _ewp(item)
    if ewp_species:
        species = ewp_species
    spacing = float(item.get("spacing_in") or 16)
    brg_default, brg_R1, brg_R2 = _bearing(item)
    sc = item.get("service_conditions") or None
    defl_LL, defl_TL = _defl(item)
    cantilever = float(item.get("cantilever_ft") or 0)

    return dict(
        name=item.get("zone") or "Floor Joist",
        system="Floor",
        application="Joist",
        size=_member_size(item.get("size"), "2x10"),
        species=species,
        grade=grade,
        plies=int(item.get("number_of_plies") or 1),
        clear_span_ft=float(item.get("clear_span_ft") or 0.0),
        spacing_in=spacing,
        trib_width_ft=spacing / 12.0,
        D_psf=_floor_dead(item, env),
        L_psf=float((env.get("design_loads") or {}).get("floor_live_load_psf") or 40),
        Lr_psf=0.0,
        S_psf=0.0,
        support_species=_support_species(item),
        bearing_area_length_in=brg_default,
        L_ol_ft=cantilever,
        L_or_ft=0.0,
        pitch=0.0,
        defl_LL_denom=defl_LL,
        defl_TL_denom=defl_TL,
        service_conditions=sc,
        ewp_overrides=ewp_overrides,
        bearing_R1_in=brg_R1,
        bearing_R2_in=brg_R2,
    )


def map_floor_drop_beam(item: Dict[str, Any], env: Dict[str, Any]) -> Dict[str, Any]:
    """schema.FloorDropBeam -> horizontal_calc.check_horizontal kwargs"""
    label = item.get("zone") or "Floor Drop Beam"
    _require_fields(item, {"clear_span_ft": "Clear Span (ft)"}, label)
    species, grade = _lumber(item)
    ewp_species, ewp_overrides = _ewp(item)
    if ewp_species:
        species = ewp_species
    brg_default, brg_R1, brg_R2 = _bearing(item)
    sc = item.get("service_conditions") or None
    defl_LL, defl_TL = _defl(item)
    trib = float(item.get("tributary_width_ft") or 6)

    return dict(
        name=item.get("zone") or "Floor Drop Beam",
        system="Floor",
        application="Beam",
        size=_member_size(item.get("size"), "4x8"),
        species=species,
        grade=grade,
        plies=int(item.get("number_of_plies") or 1),
        clear_span_ft=float(item.get("clear_span_ft") or 0.0),
        spacing_in=0,
        trib_width_ft=trib,
        D_psf=_floor_dead(item, env),
        L_psf=float((env.get("design_loads") or {}).get("floor_live_load_psf") or 40),
        Lr_psf=0.0,
        S_psf=0.0,
        support_species=_support_species(item),
        bearing_area_length_in=brg_default,
        L_ol_ft=0.0,
        L_or_ft=0.0,
        pitch=0.0,
        defl_LL_denom=defl_LL,
        defl_TL_denom=defl_TL,
        service_conditions=sc,
        ewp_overrides=ewp_overrides,
        bearing_R1_in=brg_R1,
        bearing_R2_in=brg_R2,
    )


def map_floor_flush_beam(item: Dict[str, Any], env: Dict[str, Any]) -> Dict[str, Any]:
    """schema.FloorFlushBeam -> horizontal_calc.check_horizontal kwargs"""
    label = item.get("zone") or "Floor Flush Beam"
    _require_fields(item, {"clear_span_ft": "Clear Span (ft)"}, label)
    species, grade = _lumber(item)
    ewp_species, ewp_overrides = _ewp(item)
    if ewp_species:
        species = ewp_species
    brg_default, brg_R1, brg_R2 = _bearing(item)
    sc = item.get("service_conditions") or None
    defl_LL, defl_TL = _defl(item)
    trib = float(item.get("tributary_width_ft") or 6)

    return dict(
        name=item.get("zone") or "Floor Flush Beam",
        system="Floor",
        application="Beam",
        size=_member_size(item.get("size"), "4x8"),
        species=species,
        grade=grade,
        plies=int(item.get("number_of_plies") or 1),
        clear_span_ft=float(item.get("clear_span_ft") or 0.0),
        spacing_in=0,
        trib_width_ft=trib,
        D_psf=_floor_dead(item, env),
        L_psf=float((env.get("design_loads") or {}).get("floor_live_load_psf") or 40),
        Lr_psf=0.0,
        S_psf=0.0,
        support_species=_support_species(item),
        bearing_area_length_in=brg_default,
        L_ol_ft=0.0,
        L_or_ft=0.0,
        pitch=0.0,
        defl_LL_denom=defl_LL,
        defl_TL_denom=defl_TL,
        service_conditions=sc,
        ewp_overrides=ewp_overrides,
        bearing_R1_in=brg_R1,
        bearing_R2_in=brg_R2,
    )


# ================================================================
#  HEADER  —  horizontal_calc.check_horizontal
# ================================================================
def map_header(item: Dict[str, Any], env: Dict[str, Any]) -> Dict[str, Any]:
    """schema.Header -> horizontal_calc.check_horizontal kwargs"""
    label = item.get("zone") or item.get("opening_mark") or "Header"
    _require_fields(item, {"header_clear_span_ft": "Header Clear Span (ft)"}, label)
    species, grade = _lumber(item)
    ewp_species, ewp_overrides = _ewp(item)
    if ewp_species:
        species = ewp_species
    sc = item.get("service_conditions") or None
    defl_LL, defl_TL = _defl(item)

    brg = float(item.get("available_bearing_in") or 3.5)
    trib = float(item.get("tributary_width_ft") or 2)
    span = float(item.get("header_clear_span_ft") or 0.0)

    D_psf = 0.0
    L_psf = float((env.get("design_loads") or {}).get("floor_live_load_psf") or 40)
    Lr_psf = float(item.get("roof_load_on_header_psf") or 0)

    # Distribute point load as equivalent uniform dead load over the span
    point_load = float(item.get("point_load_lbs") or 0)
    if point_load and span > 0 and trib > 0:
        D_psf += (point_load / span) / trib

    return dict(
        name=item.get("zone") or item.get("opening_mark") or "Header",
        system="Wall",
        application="Header",
        size=_member_size(item.get("header_size"), "4x12"),
        species=species,
        grade=grade,
        plies=int(item.get("number_of_plies") or 2),
        clear_span_ft=span,
        spacing_in=0,
        trib_width_ft=trib,
        D_psf=D_psf,
        L_psf=L_psf,
        Lr_psf=Lr_psf,
        S_psf=0.0,
        support_species=_support_species(item),
        bearing_area_length_in=brg,
        L_ol_ft=0.0,
        L_or_ft=0.0,
        pitch=0.0,
        defl_LL_denom=defl_LL,
        defl_TL_denom=defl_TL,
        service_conditions=sc,
        ewp_overrides=ewp_overrides,
        bearing_R1_in=None,
        bearing_R2_in=None,
    )


def _normalize_mark(text: str) -> str:
    """Normalize a member mark for fuzzy matching."""
    return re.sub(r"[\s\-_]+", "", text.strip().lower())


# Words that carry no identity when linking a footing's supported_element to
# a wall/post label — nearly every wall description contains them.
_GENERIC_MARK_TOKENS = {
    "wall",
    "walls",
    "bearing",
    "exterior",
    "interior",
    "stud",
    "framed",
    "wood",
    "footing",
    "under",
    "along",
    "at",
    "the",
    "of",
    "and",
    "a",
    "an",
    "to",
    "on",
    "new",
    "existing",
    "line",
    "grid",
    "side",
    "floor",
    "level",
    "story",
    "first",
    "second",
    "third",
    "1st",
    "2nd",
    "3rd",
}


def _mark_tokens(text: Any) -> set:
    return set(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def _match_mark(store: Dict[str, Any], supported_element: str) -> Dict[str, Any] | None:
    """Match a supported_element string against a dict keyed by member marks.
    Prefers exact match, then substring containment, then token overlap.

    The footing and wall extractions run as independent LLM calls, so the
    footing's supported_element rarely matches a wall zone verbatim
    ("South exterior bearing wall" vs "Exterior bearing wall - south side,
    1st floor"). The token stage matches on the distinctive words (direction,
    room, grid, mark) regardless of word order and boilerplate."""
    normalized = _normalize_mark(supported_element)
    if not normalized:
        return None
    for mark, data in store.items():
        if _normalize_mark(mark) == normalized:
            return data
    for mark, data in store.items():
        nm = _normalize_mark(mark)
        if len(nm) >= 2 and len(normalized) >= 2:
            if nm in normalized or normalized in nm:
                return data

    # Token-overlap fallback
    q_tokens = _mark_tokens(supported_element)
    if not q_tokens:
        return None
    q_key = q_tokens - _GENERIC_MARK_TOKENS
    best, best_score = None, 0.0
    for mark, data in store.items():
        m_tokens = _mark_tokens(mark)
        if not m_tokens:
            continue
        m_key = m_tokens - _GENERIC_MARK_TOKENS
        # distinctive words of one side must all appear in the other; if
        # neither side has any distinctive words (pure boilerplate labels),
        # fall through to plain overlap scoring
        if not (
            (q_key and q_key <= m_tokens)
            or (m_key and m_key <= q_tokens)
            or (not q_key and not m_key)
        ):
            continue
        score = len(q_tokens & m_tokens) / len(q_tokens | m_tokens)
        if score > best_score:
            best, best_score = data, score
    return best if best_score >= 0.3 else None


def _lookup_member_reactions(
    env: Dict[str, Any], supported_element: str | None
) -> Dict[str, Any] | None:
    """Find a post/wall's stored reactions by matching against supported_element text."""
    if not supported_element:
        return None
    reactions = env.get("member_reactions") or {}
    return _match_mark(reactions, supported_element) if reactions else None


def _lookup_member_index(
    env: Dict[str, Any], supported_element: str | None
) -> Dict[str, Any] | None:
    """Find a member's extracted data by matching against supported_element text."""
    if not supported_element:
        return None
    index = env.get("member_index") or {}
    return _match_mark(index, supported_element) if index else None


# ================================================================
#  POST  —  post_calc.check_post
# ================================================================
def post_axial_loads(item: Dict[str, Any], env: Dict[str, Any]) -> Dict[str, float]:
    """Axial loads (lbs) on a standalone post from tributary area + point
    load. Shared by the post check (map_post) and the pad-footing fallback
    (map_pad_footing) so the footing sees the same load the post ran with."""
    trib = float(item.get("tributary_area_sf") or 0)
    point = float(item.get("point_load_lbs") or 0)
    user_dl = env.get("design_loads") or {}
    roof_dl = float(
        item.get("roof_dead_load_psf") or user_dl.get("roof_dead_load_psf") or 15
    )
    roof_ll = float(
        item.get("roof_live_load_psf") or user_dl.get("roof_live_load_psf") or 20
    )
    roof_sl = float(
        item.get("roof_snow_load_psf") or env.get("hazard_snow_psf", 0) or 0
    )
    floor_dl = float(item.get("floor_dead_load_psf") or 0)
    floor_ll = float(item.get("floor_live_load_psf") or 0)
    return {
        "D_lbs": (roof_dl + floor_dl) * trib + point,
        "L_lbs": floor_ll * trib,
        "Lr_lbs": roof_ll * trib,
        "S_lbs": roof_sl * trib,
    }


def map_post(item: Dict[str, Any], env: Dict[str, Any]) -> Dict[str, Any]:
    """schema.StandalonePost -> post_calc.check_post kwargs"""
    height = item.get("height_ft")
    if not height:
        mark = item.get("post_mark") or "Post"
        raise ValueError(
            f"{mark}: post height (height_ft) is missing — "
            f"cannot run analysis without a column height."
        )

    sp = item.get("species") or "DF-L"
    gr = _normalize_grade(item.get("grade")) or "No. 2"
    ewp_species, ewp_overrides = _ewp(item)
    if ewp_species:
        sp = ewp_species
    sc = item.get("service_conditions") or None

    trib = float(item.get("tributary_area_sf") or 0)
    loads = post_axial_loads(item, env)
    D_lbs = loads["D_lbs"]
    L_lbs = loads["L_lbs"]
    Lr_lbs = loads["Lr_lbs"]
    S_lbs = loads["S_lbs"]

    brg_len = item.get("bearing_length_in")
    brg_len = float(brg_len) if brg_len not in (None, "") else None

    return dict(
        name=item.get("post_mark") or "Post",
        system="Post",
        application="Column",
        size=_member_size(item.get("post_size"), "4x4"),
        species=sp,
        grade=gr,
        length_ft=float(height),
        D_lbs=D_lbs,
        L_lbs=L_lbs,
        Lr_lbs=Lr_lbs,
        S_lbs=S_lbs,
        support_species=_support_species(item),
        lat_wind_psf=_wind_psf(
            env,
            effective_area_sf=(
                float(height) * max(trib**0.5 * 0.5, float(height) / 3.0)
                if trib > 0
                else None
            ),
        ),
        lat_trib_ft=trib**0.5 * 0.5 if trib > 0 else 0.0,
        ecc_ratio=0.1667,
        ewp_overrides=ewp_overrides,
        plies=int(item.get("number_of_plies") or 1),
        service_conditions=sc,
        bearing_length_in=brg_len,
    )


# ================================================================
#  CONTINUOUS STRIP FOOTING  —  footing_calc.check_footing
# ================================================================
def map_continuous_footing(item: Dict[str, Any], env: Dict[str, Any]) -> Dict[str, Any]:
    """schema.ContinuousStripFooting -> footing_calc.check_footing kwargs"""
    width_in = float(item.get("width_in") or 12)
    depth_in = float(item.get("depth_in") or 8)

    rebar_num = item.get("transverse_rebar_size")
    rebar_size = (
        f"#{rebar_num}"
        if rebar_num and not str(rebar_num).startswith("#")
        else (str(rebar_num or "#4"))
    )
    rebar_spacing = float(item.get("transverse_rebar_spacing_in") or 12)
    cover = float(item.get("concrete_cover_in") or 3.0)

    q_allow, fc_ftg, _ = _footing_info(env)

    # Link key: explicit supported_element, else the location text (fuzzy match)
    link = item.get("supported_element") or item.get("location_description")
    wall_reactions = _lookup_member_reactions(env, link)
    wall_data = _lookup_member_index(env, link)
    if wall_reactions and wall_reactions.get("type") == "wall":
        axial_D = wall_reactions["D_plf"] / 1000.0
        axial_L = wall_reactions["L_plf"] / 1000.0
        axial_S = wall_reactions["S_plf"] / 1000.0
    elif wall_data and wall_data.get("tributary_width_ft"):
        loads = stud_wall_line_loads(wall_data, env)
        axial_D = loads["D_plf"] / 1000.0
        axial_L = loads["L_plf"] / 1000.0
        axial_S = loads["S_plf"] / 1000.0
    else:
        raw_D = item.get("axial_D_kips")
        raw_L = item.get("axial_L_kips")
        if not raw_D and not raw_L:
            label = item.get("footing_mark") or "Strip Footing"
            raise ValueError(
                f"{label}: cannot run analysis — "
                f"Either provide Axial Dead Load (kips), Axial Live Load (kips) manually."
                f"Or set supported_element to a valid wall zone/mark or fill"
                f"(Our system is susceptible to providing invalid wall zone/marks)"
            )
        axial_D = float(raw_D or 0)
        axial_L = float(raw_L or 0)
        axial_S = float(item.get("axial_S_kips") or 0)

    long_num = item.get("longitudinal_rebar_size") or item.get("longitudinal_bar_size")
    long_bar = (
        f"#{long_num}"
        if long_num and not str(long_num).startswith("#")
        else str(long_num or "#4")
    )
    n_long = item.get("longitudinal_rebar_count") or item.get("n_longitudinal_bars")

    return dict(
        name=item.get("footing_mark") or "Strip Footing",
        footing_type="continuous",
        size_x_ft=width_in / 12.0,
        size_y_ft=1.0,
        thickness_in=depth_in,
        axial_D_kips=axial_D,
        axial_L_kips=axial_L,
        axial_S_kips=axial_S,
        q_allow_psf=q_allow,
        f_prime_c=fc_ftg,
        rebar_size=rebar_size,
        rebar_spacing_in=rebar_spacing,
        cover_in=cover,
        long_bar_size=long_bar,
        n_long_bars=int(n_long or 2),
    )


# ================================================================
#  PAD FOOTING  —  post_footing_calc.check_post_footing
# ================================================================
def map_pad_footing(item: Dict[str, Any], env: Dict[str, Any]) -> Dict[str, Any]:
    """schema.PadFooting -> post_footing_calc.check_post_footing kwargs"""

    width_in = float(item.get("width_in") or 24)
    length_in = float(item.get("length_in") or 24)
    depth_in = float(item.get("depth_in") or 12)
    cover = float(item.get("concrete_cover_in") or 3.0)

    post_size = _member_size(item.get("post_size"), "4x4")
    try:
        parts = post_size.lower().replace("x", " ").split()
        a1 = float(parts[0]) - 0.5
        a2 = float(parts[1]) - 0.5 if len(parts) > 1 else a1
    except Exception:
        a1 = a2 = 3.5

    q_allow, fc_ftg, _ = _footing_info(env)

    rebar_raw = item.get("rebar_size")
    rebar_str = (
        f"#{rebar_raw}"
        if rebar_raw and not str(rebar_raw).startswith("#")
        else (str(rebar_raw or "#4"))
    )
    rebar_spacing = float(item.get("rebar_spacing_in") or 12)

    link = item.get("supported_element") or item.get("location_description")
    post_reactions = _lookup_member_reactions(env, link)
    post_data = _lookup_member_index(env, link)
    if post_reactions and post_reactions.get("type") == "post":
        DL = post_reactions["D_lbs"]
        LL = post_reactions["L_lbs"]
        SL = post_reactions.get("S_lbs", 0) + post_reactions.get("Lr_lbs", 0)
    elif (
        post_data
        and post_data.get("post_mark")
        and (post_data.get("tributary_area_sf") or post_data.get("point_load_lbs"))
    ):
        # post found in the extraction but its reactions were never stored
        # (e.g. the post check itself errored) — derive the same loads here
        loads = post_axial_loads(post_data, env)
        DL = loads["D_lbs"]
        LL = loads["L_lbs"]
        SL = loads["S_lbs"] + loads["Lr_lbs"]
    else:
        raw_DL = item.get("axial_DL_lbs")
        raw_LL = item.get("axial_LL_lbs")
        if not raw_DL and not raw_LL:
            label = item.get("footing_mark") or "Pad Footing"
            raise ValueError(
                f"{label}: cannot run analysis — "
                f"Either provide Axial Dead Load (kips), Axial Live Load (kips) manually."
                f"Or set supported_element to a valid post mark or fill in"
                f"(Our system is susceptible to providing invalid post mark or fill in)"
            )
        DL = float(raw_DL or 0)
        LL = float(raw_LL or 0)
        SL = float(item.get("axial_SL_lbs") or 0)

    inp = PostFootingInputs(
        B1=width_in / 12.0,
        B2=length_in / 12.0,
        H=depth_in,
        cover=cover,
        a1=a1,
        a2=a2,
        fc=fc_ftg,
        q_allow_ton=q_allow / 2000.0,
        DL=DL,
        LL=LL,
        SL=SL,
        bar_B1=rebar_str,
        bar_B2=rebar_str,
        bar_spacing_B1=rebar_spacing,
        bar_spacing_B2=rebar_spacing,
    )
    return dict(inp=inp)


# ================================================================
#  SHEAR WALL  —  lateral load distribution + mapper
# ================================================================
def _lat_f(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _lat_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _lat_line_key(item: Dict[str, Any]) -> tuple:
    """(story, line) key for lateral line grouping and lookup, normalized so
    the independent extraction calls land on the same key: story labels
    resolve to their number ("Level 1" / "1st Floor" / "1" -> "1") and SWL
    ids drop spaces/dashes ("SWL-A" / "swl a" -> "swla", "X-1" -> "x1")."""
    story = item.get("story_level")
    num = story_number(story)
    story_key = str(num) if num is not None else _lat_key(story)
    sid = _normalize_mark(str(item.get("swl_id") or item.get("bwl_id") or ""))
    return story_key, sid


def sw_wall_length(item: Dict[str, Any]) -> float:
    """Sheathed (full-height) length of a shear wall entry, ft."""
    piers = [p for p in (item.get("pier_lengths_ft") or []) if _lat_f(p)]
    if piers:
        return sum(float(p) for p in piers)
    return _lat_f(item.get("pier_length_ft")) or 4.0


def _piers_from_opening_positions(
    total_sheathed_ft: float, raw_openings: List[Dict[str, Any]]
) -> List[float]:
    """Reconstruct individual pier lengths from opening positions
    (distance_from_wall_left_ft = wall's left end to opening's left edge).

    Wall length = sheathed total + opening widths; the piers are the gaps
    between/outside the openings, left to right. Returns [] whenever any
    position or width is missing, openings overlap, or the reconstruction
    does not add back up to the sheathed total — callers then fall back."""
    spans = []
    for o in raw_openings:
        x = _lat_f(o.get("distance_from_wall_left_ft"))
        w = _lat_f(o.get("width_ft"))
        if x is None or x < 0 or not w or w <= 0:
            return []
        spans.append((x, w))
    if not spans:
        return []
    spans.sort()
    wall_len = total_sheathed_ft + sum(w for _, w in spans)
    piers: List[float] = []
    cursor = 0.0
    for x, w in spans:
        gap = x - cursor
        if gap < -0.01:  # overlapping openings — positions unreliable
            return []
        if gap > 0.01:
            piers.append(gap)
        cursor = x + w
    tail = wall_len - cursor
    if tail < -0.01:
        return []
    if tail > 0.01:
        piers.append(tail)
    if not piers or abs(sum(piers) - total_sheathed_ft) > max(
        0.05 * total_sheathed_ft, 0.1
    ):
        return []
    return piers


def build_lateral_line_shears(
    extracted_data: Dict[str, Any], hazard_data: Dict[str, Any]
) -> Dict[str, Any]:
    """Distribute story shear to shear wall lines.

    Without diaphragm geometry a tributary-area distribution is not
    computable, so lines of known direction share the story shear in
    proportion to their sheathed length (stiffness ~ length for same
    construction). Precedence per line:
      1. user override            extracted_data["lateral_loads"]["line_shears"][swl_id]
      2. direction known (X/Y)    story shear x line_length / sum(parallel line lengths)
      3. direction unknown        full story shear on the line (conservative)
    Story shear per direction, in precedence order:
      1. user:     extracted_data["lateral_loads"]["story_shears"][story][X|Y]
                   (or directionless [story] values)
      2. computed: lateral_calc.compute_lateral_loads — ASCE 7-22 ELF seismic
                   + MWFRS wind from hazard_lookup_result + building_geometry
      3. flat:     extracted_data Vs_lbs / Vw_lbs
    The full story shear applies to each principal direction separately
    (not split between orthogonal directions).

    Returns {"line_shears": {(story, swl_id): {"Vs", "Vw", "total_length_ft",
    "direction", "note"}}} with normalized lowercase keys.
    """
    sw = extracted_data.get("shear_wall") or {}
    if not isinstance(sw, dict):
        return {}
    walls = [w for w in (sw.get("shear_walls") or []) if isinstance(w, dict)]
    if not walls:
        return {}
    user = extracted_data.get("lateral_loads")
    user = user if isinstance(user, dict) else {}
    story_user = {
        _lat_key(k): v
        for k, v in (user.get("story_shears") or {}).items()
        if isinstance(v, dict)
    }
    line_user = {
        _lat_key(k): v
        for k, v in (user.get("line_shears") or {}).items()
        if isinstance(v, dict)
    }

    Vs0 = _lat_f(extracted_data.get("Vs_lbs")) or 0.0
    Vw0 = _lat_f(extracted_data.get("Vw_lbs")) or 0.0

    computed = compute_lateral_loads(extracted_data, hazard_data)
    computed_story = computed.get("story_shears") or {}
    n_stories = len(computed_story) or None

    def _dir_from_swl(swl_id: str) -> str | None:
        """Infer direction from SWL naming convention: X1->X, Y2->Y."""
        if not swl_id:
            return None
        first = swl_id.lstrip().upper()[0] if swl_id.strip() else None
        return first if first in ("X", "Y") else None

    # Group walls into lines (wood shear walls + participating masonry walls)
    lines: Dict[tuple, Dict[str, Any]] = {}
    for w in walls:
        key = _lat_line_key(w)
        rec = lines.setdefault(key, {"total_length_ft": 0.0})
        rec["total_length_ft"] += sw_wall_length(w)

    # Masonry walls live under extracted_data["wall"]["masonry_walls"] (the
    # extractor's wall call); also accept a legacy top-level "masonry" key.
    mwalls: list[dict[str, Any]] = []
    for container in (extracted_data.get("wall"), extracted_data.get("masonry")):
        if isinstance(container, dict):
            mwalls.extend(
                m for m in (container.get("masonry_walls") or []) if isinstance(m, dict)
            )
    for m in mwalls:
        if _lat_key(m.get("seismic_participation")) != "participating":
            continue
        key = _lat_line_key(m)
        if not key[1]:
            continue
        rec = lines.setdefault(key, {"total_length_ft": 0.0})
        rec["total_length_ft"] += _lat_f(m.get("panel_length_ft")) or 0.0

    for (story, swl), rec in lines.items():
        rec["direction"] = _dir_from_swl(swl)

    # Parallel-line length pools per (story, direction)
    pool_len: Dict[tuple, float] = {}
    pool_count: Dict[tuple, int] = {}
    for (story, _), rec in lines.items():
        d = rec.get("direction")
        if d:
            pool_len[(story, d)] = (
                pool_len.get((story, d), 0.0) + rec["total_length_ft"]
            )
            pool_count[(story, d)] = pool_count.get((story, d), 0) + 1

    def _computed_shear(story: str, d: str | None) -> tuple:
        """(Vs, Vw) from the auto-computed story shears, (None, None) if n/a.
        Unknown direction -> max of X/Y (conservative)."""
        num = story_number(story, n_stories)
        levels = computed_story.get(str(num)) if num is not None else None
        if not isinstance(levels, dict):
            return None, None
        if d in ("X", "Y"):
            ds = levels.get(d) or {}
            return _lat_f(ds.get("Vs")), _lat_f(ds.get("Vw"))
        vals = [v for v in (levels.get("X"), levels.get("Y")) if isinstance(v, dict)]
        if not vals:
            return None, None
        return (
            max((_lat_f(v.get("Vs")) or 0.0) for v in vals),
            max((_lat_f(v.get("Vw")) or 0.0) for v in vals),
        )

    def _story_shear(story: str, d: str | None) -> tuple:
        su = story_user.get(story) or story_user.get("all") or {}
        ds = su.get(d) if d and isinstance(su.get(d), dict) else None
        src = ds if ds is not None else su
        vs = _lat_f(src.get("Vs")) if isinstance(src, dict) else None
        vw = _lat_f(src.get("Vw")) if isinstance(src, dict) else None
        cs, cw = _computed_shear(story, d)
        if vs is None:
            vs = cs if cs is not None else Vs0
        if vw is None:
            vw = cw if cw is not None else Vw0
        return vs, vw

    line_shears: Dict[tuple, Dict[str, Any]] = {}
    for key, rec in lines.items():
        story, swl = key
        d = rec.get("direction")
        lu = line_user.get(swl)
        if lu is not None:
            vs_line = _lat_f(lu.get("Vs")) or 0.0
            vw_line = _lat_f(lu.get("Vw")) or 0.0
            note = "user-defined line shear"
        elif d:
            vs_story, vw_story = _story_shear(story, d)
            total = pool_len.get((story, d), 0.0)
            n_parallel = pool_count.get((story, d), 1)
            # Envelope: length-proportional share, floored at an equal share
            # per parallel line so a short line with a large tributary slice
            # is never starved of demand.
            frac_len = rec["total_length_ft"] / total if total > 0 else 1.0
            frac = max(frac_len, 1.0 / max(n_parallel, 1))
            vs_line, vw_line = vs_story * frac, vw_story * frac
            governs = (
                "equal-share floor" if frac > frac_len else "sheathed-length share"
            )
            note = (
                f"{d}-direction story shear by sheathed length with "
                f"equal-share floor — {governs} governs (fraction {frac:.3f})"
            )
        else:
            vs_line, vw_line = _story_shear(story, None)
            note = (
                "line direction unknown — full story shear assigned "
                "(conservative); use X/Y prefix in swl_id or set "
                "lateral_loads.line_shears to refine"
            )
        line_shears[key] = {
            "Vs": vs_line,
            "Vw": vw_line,
            "total_length_ft": rec["total_length_ft"],
            "direction": d,
            "note": note,
        }
    return {
        "line_shears": line_shears,
        "computed": computed.get("meta"),
        "story_shears": computed.get("story_shears"),
    }


# ================================================================
#  SHEAR WALL  —  shear_wall_calc.check_shear_wall
# ================================================================
def map_shear_wall(item: Dict[str, Any], env: Dict[str, Any]) -> Dict[str, Any]:
    """schema.ShearWall -> shear_wall_calc.check_shear_wall kwargs

    Lateral demands (Vs, Vw) and seismic parameters (SDS) are expected in env
    from the lateral load analysis — they are not part of the per-wall schema.
    """
    label = item.get("sw_mark") or "Shear Wall"
    _require_fields(item, {"wall_height_ft": "Wall Height (ft)"}, label)

    wall_height = float(item.get("wall_height_ft") or 0.0)
    pier_length = float(item.get("pier_length_ft") or 4)

    # Sheathing thickness: schema stores float (0.4375), engine wants string ("7/16")
    thickness_raw = item.get("sheathing_thickness_in")
    _thickness_map = {0.375: "3/8", 0.4375: "7/16", 0.46875: "15/32"}
    if isinstance(thickness_raw, (int, float)):
        panel_thickness = _thickness_map.get(float(thickness_raw))
        if panel_thickness is None:
            # snap to nearest tabulated thickness
            panel_thickness = min(
                _thickness_map.items(), key=lambda kv: abs(kv[0] - float(thickness_raw))
            )[1]
    else:
        panel_thickness = str(thickness_raw or "15/32")

    # Plywood vs OSB (FTAO deflection stiffness tables)
    sheathing_desc = str(item.get("sheathing_type") or "").lower()
    panel_material = (
        "Plywood" if ("ply" in sheathing_desc or "cdx" in sheathing_desc) else "OSB"
    )
    panel_grade = (
        "APA Structural 1"
        if ("struct" in sheathing_desc and "1" in sheathing_desc)
        else "APA Rated Sheathing"
    )

    faces = (item.get("sheathing_faces") or "one_side").lower()
    both_sides = "two" in faces

    edge_nail = int(item.get("edge_nail_spacing_in") or 6)
    nail_size = item.get("nail_size") or "10d"
    fastener = "10d" if "10" in nail_size else "8d"

    hd_model = item.get("holdown_model") or "HDU2"
    stud_size = _member_size(item.get("stud_size"), "2x6")
    stud_spacing = int(item.get("stud_spacing_in") or 16)

    # Lateral demand: line shear share by sheathed length (uniform unit
    # shear along the line); falls back to flat story shear from env.
    Vs = float(env.get("Vs_lbs", 0) or 0)
    Vw = float(env.get("Vw_lbs", 0) or 0)
    line = ((env.get("lateral") or {}).get("line_shears") or {}).get(
        _lat_line_key(item)
    )
    if line and line.get("total_length_ft"):
        frac = min(sw_wall_length(item) / line["total_length_ft"], 1.0)
        Vs = float(line.get("Vs") or 0) * frac
        Vw = float(line.get("Vw") or 0) * frac
    sds = float(env.get("SDS", 0.5))

    Cd = float(env.get("Cd", 4.0))
    Ie = float(env.get("Ie", 1.0))
    species = str(item.get("framing_species") or "DF No. 2")

    common: Dict[str, Any] = dict(
        Vs=Vs,
        Vw=Vw,
        wall_height=wall_height,
        nail_edge_spacing=edge_nail,
        sheathing_both_sides=both_sides,
        panel_thickness=panel_thickness,
        panel_material=panel_material,
        panel_grade=panel_grade,
        fastener_type=fastener,
        holdown_model=hd_model,
        chord_studs=f"(2) {stud_size}",
        chord_species=species,
        sill_plate_type="(1)-2x",
        top_sill_species=species.split()[0],
        stud_spacing=stud_spacing,
        SDS=sds,
        Cd_defl=Cd,
        Ie_defl=Ie,
    )

    # Method routing: segmented (default), perforated (SDPWS 4.3.5.3),
    # or FTAO (APA T555) when opening geometry was extracted.
    method = str(item.get("shear_wall_method") or "segmented").lower()
    pier_lengths = [float(p) for p in (item.get("pier_lengths_ft") or []) if p]
    raw_openings = [o for o in (item.get("openings") or []) if isinstance(o, dict)]

    # Perforated/FTAO capacity depends on opening area — an opening with a
    # missing dimension would be silently dropped, INCREASING the computed
    # capacity. Refuse to run and flag for review instead.
    if ("perf" in method or "ftao" in method) and raw_openings:
        incomplete = [
            str(i)
            for i, o in enumerate(raw_openings, start=1)
            if not _lat_f(o.get("width_ft")) or not _lat_f(o.get("height_ft"))
        ]
        if incomplete:
            raise ValueError(
                f"{label}: cannot run {method} shear wall analysis — "
                f"opening(s) {', '.join(incomplete)} missing width/height. "
                f"Fill in the opening dimensions and re-run."
            )

    # Pier list fallbacks when the extractor gave only the sheathed total.
    # A multi-pier wall must NEVER be analyzed as one long pier: h/b is
    # checked per pier, and merging piers understates the aspect ratio
    # (e.g. two 2 ft piers at h=8.75 ft fail AR 3.5; one 4 ft pier passes).
    if not pier_lengths and raw_openings:
        pier_lengths = _piers_from_opening_positions(pier_length, raw_openings)
        if not pier_lengths:
            # positions unknown — split the sheathed total equally into
            # n_openings + 1 piers (shorter piers -> higher AR: conservative)
            n_piers = len(raw_openings) + 1
            pier_lengths = [pier_length / n_piers] * n_piers

    openings = []
    for o in raw_openings:
        w = float(o.get("width_ft") or 0)
        ho = float(o.get("height_ft") or 0)
        hb = float(o.get("sill_height_ft") or 0)
        if w > 0 and ho > 0:
            openings.append(
                {
                    "width_ft": w,
                    "ho_ft": ho,
                    "ha_ft": max(wall_height - ho - hb, 0),
                    "hb_ft": hb,
                }
            )

    if "ftao" in method and openings and len(pier_lengths) == len(openings) + 1:
        inp = ShearWallInputs(
            wall_type="FTAO",
            wall_length_total=sum(pier_lengths) + sum(o["width_ft"] for o in openings),
            pier_lengths=pier_lengths,
            openings=openings,
            panels=pier_lengths,
            sum_Li=sum(pier_lengths),
            bs_perf=min(pier_lengths),
            **common,
        )
    elif "perf" in method and pier_lengths:
        sum_li = sum(pier_lengths)
        total_len = sum_li + sum(o["width_ft"] for o in openings)
        Ao = sum(o["width_ft"] * o["ho_ft"] for o in openings)
        co = compute_perforated_co(total_len, wall_height, sum_li, Ao)
        inp = ShearWallInputs(
            wall_type="PERF",
            wall_length_total=total_len,
            Co=co,
            sum_Li=sum_li,
            bs_perf=min(pier_lengths),
            panels=pier_lengths,
            **common,
        )
    else:
        # Segmented whole-wall: use all pier lengths when provided.
        if pier_lengths:
            sum_li = sum(pier_lengths)
            opening_ws = [float(o.get("width_ft") or 0) for o in raw_openings]
            opening_hs = [float(o.get("height_ft") or 0) for o in raw_openings]
            total_len = sum_li + sum(opening_ws)
            inp = ShearWallInputs(
                wall_type="SEGMENT",
                wall_length_total=total_len,
                panels=pier_lengths,
                sum_Li=sum_li,
                bs_perf=min(pier_lengths),
                opening_widths_ft=opening_ws,
                opening_heights_ft=opening_hs,
                **common,
            )
        else:
            inp = ShearWallInputs(
                wall_type="SEGMENT",
                wall_length_total=pier_length,
                panels=[pier_length],
                sum_Li=pier_length,
                bs_perf=pier_length,
                **common,
            )
    return dict(inp=inp)


# ================================================================
#  MASONRY WALL  —  masonry_calc.check_masonry_wall (TMS 402-16 SD)
# ================================================================
_CMU_THK = {"6": 5.625, "8": 7.625, "10": 9.625, "12": 11.625}
_GROUT_MAP = {
    "fully_grouted": "full",
    "partially_grouted": "partial",
    "ungrouted": "ungrouted",
}


def _bar_number(raw: Any, default: int = 5) -> int:
    s = str(raw or "")
    m = re.search(r"\d+", s)
    return int(m.group()) if m else default


def _out_of_plane_wind_psf(
    env: Dict[str, Any], wall_height_ft: Optional[float] = None
) -> tuple:
    """Out-of-plane (component) wind pressure on a wall, psf, with source.
    Precedence: user design_loads override > C&C pressure per ASCE 7
    Fig. 30.3-1 zone 4 (effective area = h x h/3 per-foot strip) > None."""
    dl = env.get("design_loads") or {}
    override = _user_load(env, "wall_wind_psf") if isinstance(dl, dict) else None
    if override:
        return override, "user-defined wall_wind_psf"
    meta = (env.get("lateral") or {}).get("computed") or {}
    wind = meta.get("wind") or {}
    qh = None
    for d in ("X", "Y"):
        q = (wind.get(d) or {}).get("qh_psf")
        if q:
            qh = max(qh or 0.0, q)
    if qh:
        h = float(wall_height_ft or 10.0)
        eff_area = h * max(1.0, h / 3.0)
        return (
            round(candc_wall_pressure(qh, eff_area, zone=4), 2),
            "C&C per ASCE 7 Fig. 30.3-1 zone 4 (interior), 16 psf min",
        )
    return None, "no wind pressure available — set design_loads.wall_wind_psf"


def map_masonry_wall(item: Dict[str, Any], env: Dict[str, Any]) -> Dict[str, Any]:
    """schema.MasonryWall -> masonry_calc.check_masonry_wall kwargs.

    Out-of-plane seismic is computed inside the calc from SDS + self weight;
    out-of-plane wind comes from the wind analysis (or user override).
    Participating shear walls also get in-plane + biaxial checks (section mode).
    """
    label = item.get("wall_mark") or "Masonry Wall"
    _require_fields(item, {"wall_height_ft": "Wall Height (ft)"}, label)
    # Thickness: prefer explicit actual dim, else map nominal CMU callout
    t = _lat_f(item.get("nominal_thickness_in"))
    if not t:
        t = 7.625

    fm = (
        _lat_f(item.get("fm_psi"))
        or _lat_f(item.get("unit_compressive_strength_psi"))
        or 1900.0
    )
    grout = _GROUT_MAP.get(str(item.get("grouting") or ""), "full")
    mortar = "n" if str(item.get("mortar_type") or "").upper() == "N" else "ms"

    bar = _bar_number(item.get("vertical_bar_size"), 5)
    bar_spacing = _lat_f(item.get("vertical_bar_spacing_in")) or 24.0
    fy = (_lat_f(item.get("rebar_grade_ksi")) or 60.0) * 1000.0

    sds = float(env.get("SDS", 0.5))
    h = _lat_f(item.get("wall_height_ft")) or 0.0
    wind_psf, wind_src = _out_of_plane_wind_psf(env, wall_height_ft=h)
    L = _lat_f(item.get("panel_length_ft")) or 4.0
    P_dead = _lat_f(item.get("axial_dead_plf")) or 0.0
    P_live = _lat_f(item.get("axial_live_plf")) or 0.0
    S_psf = _lat_f(item.get("roof_snow_load_psf")) or 0.0

    from services.calculators.masonry_calc import MasonryWallInputs, self_weight_psf

    base = MasonryWallInputs(
        wall_height_ft=h,
        panel_length_ft=L,
        thickness_in=t,
        fm_psi=fm,
        grout=grout,
        mortar_class=mortar,
        bar_size=bar,
        bar_spacing_in=bar_spacing,
        fy_psi=fy,
        wind_psf=wind_psf or 0.0,
        SDS=sds,
        P_snow_plf=S_psf * (bar_spacing / 12),
        P_dead_plf=P_dead,
        P_live_plf=P_live,
        wind_source=wind_src,
    )

    participating = _lat_key(item.get("seismic_participation")) == "participating"
    line = ((env.get("lateral") or {}).get("line_shears") or {}).get(
        _lat_line_key(item)
    )

    if participating and line and line.get("total_length_ft"):
        # In-plane demand from the lateral line-shear share. Line shears are
        # stored at ASD level (0.7E / 0.6W) — masonry strength design needs
        # strength-level demands, so convert back up before comparing to phi*Vn.
        frac = min((L / line["total_length_ft"]), 1.0)
        Vx = (
            max(
                float(line.get("Vs") or 0) / 0.7,
                float(line.get("Vw") or 0) / 0.6,
            )
            * frac
        )  # lbf, strength level
        Mx = Vx * h  # cantilever (fixed base, free top), lb-ft
        # Out-of-plane factored demand: governing of wind (0.6W gravity combo
        # uses 1.0W strength) and seismic Ewall on the wall's own weight.
        wsw = self_weight_psf(base)
        E_wall = max(0.4 * sds, 0.1) * wsw
        w_gov = max(wind_psf or 0.0, E_wall)  # psf, out-of-plane
        My = w_gov * h**2 / 8.0  # lb-ft/ft
        Vy = w_gov * h / 2.0  # lb/ft
        P_axial = 0.9 * (P_dead + wsw * h / 2.0)  # factored axial at mid-height
        base.mode = "section"
        base.heff_x_ft = h
        base.heff_y_ft = h
        base.P_axial_plf = P_axial
        base.My_lbft_per_ft = My
        base.Vy_lbf_per_ft = Vy
        base.Mx_lbft = Mx
        base.Vx_lbf = Vx
        base.h_bar_size = _bar_number(item.get("horizontal_bar_size"), 4)
        base.h_bar_spacing_in = _lat_f(item.get("horizontal_bar_spacing_in")) or 48.0

    return dict(inp=base)


# ================================================================
#  DIAPHRAGM  —  diaphragm_calc.check_diaphragm
# ================================================================
def map_diaphragm(item: Dict[str, Any], env: Dict[str, Any]) -> Dict[str, Any]:
    """schema.Diaphragm -> diaphragm_calc.check_diaphragm kwargs.

    Demand: full story shear in the diaphragm's direction from the lateral
    analysis. Geometry: extracted span/depth when dimensioned, else derived
    from building plan dimensions (X-direction load -> spans the width
    between the long walls; depth = length; Y mirrored)."""
    label = item.get("level") or "Diaphragm"
    meta = (env.get("lateral") or {}).get("computed") or {}
    story_shears = (env.get("lateral") or {}).get("story_shears") or {}
    story_num = story_number(item.get("level"), meta.get("stories"))
    story = story_shears.get(str(story_num)) if story_num else None
    direction = (item.get("direction") or "").upper()

    def _shear(d):
        ds = (story or {}).get(d) or {}
        return _lat_f(ds.get("Vs")) or 0.0, _lat_f(ds.get("Vw")) or 0.0

    if direction in ("X", "Y"):
        Vs, Vw = _shear(direction)
        gov_dir = direction
    else:
        vx, wx = _shear("X")
        vy, wy = _shear("Y")
        # check the governing direction (max unit shear given the geometry)
        gov_dir = "X" if max(vx, wx) >= max(vy, wy) else "Y"
        Vs, Vw = _shear(gov_dir)

    length = _lat_f(meta.get("length_ft"))
    width = _lat_f(meta.get("width_ft"))
    span = _lat_f(item.get("span_ft"))
    depth = _lat_f(item.get("depth_ft"))
    if (not span or not depth) and length and width:
        # X-direction load: resisted by walls along the length at the two
        # sides -> diaphragm spans the width; depth = length. Y mirrored.
        if gov_dir == "X":
            span, depth = span or width, depth or length
        else:
            span, depth = span or length, depth or width
    if not span or not depth:
        raise ValueError(
            f"{label}: cannot run diaphragm analysis — no span/depth extracted "
            "and no building plan dimensions available to derive them."
        )

    blocked = item.get("blocked")
    sheathing = item.get("sheathing_type") or ""
    if item.get("nail_size"):
        sheathing = f"{sheathing} w/ {item['nail_size']} nails".strip()

    return dict(
        name=f"{label} ({gov_dir}-dir)",
        span_ft=span,
        depth_ft=depth,
        Vs_lbs=Vs,
        Vw_lbs=Vw,
        nominal_unit_shear_plf=_lat_f(item.get("nominal_unit_shear_plf")),
        blocked=bool(blocked) if blocked is not None else True,
        sheathing_note=sheathing,
        chord_member=item.get("chord_member") or "",
    )


# ================================================================
#  Registry — extend when new engines come online
# ================================================================
MAPPERS = {
    # Wall
    "stud_wall": map_stud_wall,
    # Roof
    "roof_rafter": map_roof_rafter,
    "ceiling_joist": map_ceiling_joist,
    "ridge_beam": map_ridge_beam,
    "hip_valley_rafter": map_hip_valley_rafter,
    "roof_drop_beam": map_roof_drop_beam,
    "roof_flush_beam": map_roof_flush_beam,
    # Floor
    "floor_joist": map_floor_joist,
    "floor_drop_beam": map_floor_drop_beam,
    "floor_flush_beam": map_floor_flush_beam,
    # Header
    "header": map_header,
    # Post
    "post": map_post,
    # Foundations
    "continuous_footing": map_continuous_footing,
    "pad_footing": map_pad_footing,
    # Lateral
    "shear_wall": map_shear_wall,
    "diaphragm": map_diaphragm,
    # Masonry
    "masonry_wall": map_masonry_wall,
}
