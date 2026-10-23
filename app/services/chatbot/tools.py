"""Tool registry for the SmartStructs chatbot.

Each tool is a plain callable with full type hints and a docstring — the
google.genai SDK converts them into function declarations automatically.
Project tools are built per-request via `build_tools` so they close over the
authenticated user's email (and never leak other users' data).
"""

import re
import uuid
import inspect
from typing import Optional, Callable, Any, get_type_hints, cast

from services.hazard import get_seismic_data, get_snow_data, get_wind_data
from services.calculators.post_footing_calc import PostFootingInputs
from services.calculators import (
    check_horizontal,
    check_stud,
    check_post,
    check_footing,
    check_post_footing,
    check_shear_wall,
    check_masonry_wall,
)
from services.calculators.shear_wall_calc import ShearWallInputs
from services.calculators.masonry_calc import (
    MasonryWallInputs,
    check_masonry_shear_wall,
)
from services.calculators.ijoist_calc import check_ijoist
from services.calculators.lateral_calc import (
    compute_lateral_loads,
    candc_component_pressure,
)
from services.calculators.utils.lumber import (
    normalize_species,
    normalize_grade,
    normalize_member_size,
    EWP_RECTANGULAR_PRODUCTS,
)


_EWP_PRODUCT_BY_MARKER = {"LVL": "lvl", "LSL": "lsl", "PSL": "psl", "GLULAM": "glulam"}

# Full directional wind packages are far too large for the LLM context (the
# zone tables balloon the prompt and stall the model). They are stashed here
# under a short ref id; the calc-sheet endpoint resolves the ref back to the
# full package for report rendering.
WIND_DETAIL_CACHE: dict[str, dict] = {}
_WIND_DETAIL_CACHE_MAX = 40


def _stash_wind_detail(pkg: dict) -> str:
    ref = uuid.uuid4().hex[:12]
    WIND_DETAIL_CACHE[ref] = pkg
    while len(WIND_DETAIL_CACHE) > _WIND_DETAIL_CACHE_MAX:
        WIND_DETAIL_CACHE.pop(next(iter(WIND_DETAIL_CACHE)))
    return ref


def _catalog_size_error(species: Optional[str], size: Optional[str]) -> Optional[dict]:
    """Reject engineered-wood sizes that aren't real catalog dimensions.
    The calc engine computes any WxD rectangle, so without this check the
    model can 'pass' a beam nobody manufactures (e.g. 1.75x16.25 LVL)."""
    marker = next(
        (m for m in _EWP_PRODUCT_BY_MARKER if m in (species or "").upper()), None
    )
    if marker is None:
        return None  # sawn lumber — engine validates nominal sizes itself
    product = EWP_RECTANGULAR_PRODUCTS[_EWP_PRODUCT_BY_MARKER[marker]]
    try:
        w_str, d_str = (size or "").lower().replace(" ", "").split("x")
        width, depth = float(w_str), float(d_str)
    except ValueError:
        return {
            "error": f"EWP size '{size}' must be 'WIDTHxDEPTH' in decimal inches, e.g. '1.75x18'."
        }
    if width not in product["widths"] or depth not in product["depths"]:
        return {
            "error": f"'{size}' is not a standard {marker} size — do not invent dimensions. "
            f"Catalog widths (in): {', '.join(str(w) for w in product['widths'])}. "
            f"Catalog depths (in): {', '.join(str(d) for d in product['depths'])}. "
            f"Re-run with the nearest catalog size."
        }
    return None


def _grade(g: Optional[str]) -> str:
    """Normalize grade text to engine format, incl. dotted plan callouts
    like 'No.2' that lumber.normalize_grade doesn't cover."""
    s = str(g or "No. 2").strip()
    m = re.match(r"(?i)^(?:no\.?\s*|#)?([123])$", s)
    if m:
        return f"No. {m.group(1)}"
    return normalize_grade(s)


# ================================================================
#  Member check tools (deterministic calculators)
# ================================================================


def check_beam_or_joist(
    name: str,
    application: str,
    size: str,
    clear_span_ft: float,
    species: str = "DF-L",
    grade: str = "No. 2",
    plies: int = 1,
    spacing_in: float = 16.0,
    tributary_width_ft: float = 0.0,
    dead_load_psf: float = 0.0,
    floor_live_load_psf: float = 0.0,
    roof_live_load_psf: float = 0.0,
    snow_load_psf: float = 0.0,
    support_species: str = "SPF",
    bearing_length_in: float = 3.5,
    overhang_left_ft: float = 0.0,
    overhang_right_ft: float = 0.0,
    roof_pitch_rise: float = 0.0,
) -> dict:
    """Check any horizontal wood member per NDS/ASD: rafters, ceiling/floor joists, ridge/drop/flush beams, and headers. Checks bending, shear, deflection (floor members L/360 live & L/240 total; roof members L/240 & L/180 per IBC 1604.3), and bearing.

    application: "Joist" (repetitive member — pass spacing_in, tributary defaults to spacing/12), "Beam" or "Header" (pass tributary_width_ft, set spacing_in=0).
    size: nominal like "2x10"/"4x12" or EWP dimensions like "1.75x11.875".
    species: DF-L, SP, SPF, HF, or EWP names LVL, LSL, PSL, glulam. grade: "SS", "No. 1", "No. 2", "Stud" (for EWP use "2.0E" etc.).
    Loads are area loads in psf; roof members typically use dead + roof live (Lr) + snow, floor members dead + floor live.
    roof_pitch_rise: rise per 12 (e.g. 6 for a 6/12 roof), 0 for flat/floor.
    Returns per-check Actual/Allowed/utilization and a summary."""
    # Identity guard: this tool checks solid rectangular sections only. A
    # member named as an I-joist must go through check_i_joist (or be
    # renamed) so reports never carry a product label that wasn't analyzed.
    ident = f"{name} {size}".upper()
    if "TJI" in ident or "I-JOIST" in ident or "IJOIST" in ident:
        return {
            "error": "This member is named as a TJI/I-joist, but check_beam_or_joist "
            "analyzes solid rectangular lumber/LVL only. Use check_i_joist for TJI "
            "products, or rename the member to the actual solid section being checked."
        }
    catalog_error = _catalog_size_error(
        normalize_species(species), normalize_member_size(size)
    )
    if catalog_error:
        return catalog_error
    if application.lower() == "joist":
        trib = tributary_width_ft or spacing_in / 12.0
        system = "Roof" if (roof_live_load_psf or snow_load_psf) else "Floor"
    else:
        trib = tributary_width_ft or 6.0
        spacing_in = 0
        if application.lower() == "header":
            system = "Wall"
        else:
            # System drives deflection limits (IBC 1604.3): floor beams get
            # L/360 live / L/240 total, roof beams L/240 / L/180.
            system = "Roof" if (roof_live_load_psf or snow_load_psf) else "Floor"
    return check_horizontal(
        name=name,
        system=system,
        application=application.title(),
        size=normalize_member_size(size),
        species=normalize_species(species),
        grade=_grade(grade),
        plies=int(plies),
        clear_span_ft=float(clear_span_ft),
        spacing_in=float(spacing_in),
        trib_width_ft=float(trib),
        D_psf=float(dead_load_psf),
        L_psf=float(floor_live_load_psf),
        Lr_psf=float(roof_live_load_psf),
        S_psf=float(snow_load_psf),
        support_species=normalize_species(support_species),
        bearing_area_length_in=float(bearing_length_in),
        L_ol_ft=float(overhang_left_ft),
        L_or_ft=float(overhang_right_ft),
        pitch=float(roof_pitch_rise),
    )


def check_i_joist(
    name: str,
    series: str,
    depth_in: float,
    clear_span_ft: float,
    spacing_in: float = 16.0,
    dead_load_psf: float = 10.0,
    floor_live_load_psf: float = 40.0,
    roof_live_load_psf: float = 0.0,
    snow_load_psf: float = 0.0,
    bearing_length_in: float = 1.75,
    web_stiffeners: bool = False,
) -> dict:
    """Check a Weyerhaeuser TJI I-joist per TJ-4000/ESR-1153: moment vs Mr, shear at the inside face vs Vr, end reaction vs bearing-length capacity, and deflection using the manufacturer's two-term formula (bending + shear deformation).

    series: "110", "210", "230", "360", or "560" (560D is checked with 560 values).
    depth_in: 9.5, 11.875, 14, or 16 (9.5 only for 110/210/230).
    Use this instead of check_beam_or_joist whenever the plan calls out a TJI/I-joist. Prefer TJIs for floor joist spans over ~18 ft.
    Note: TJ-Pro vibration rating is NOT evaluated."""
    return check_ijoist(
        name=name,
        series=series,
        depth_in=float(depth_in),
        clear_span_ft=float(clear_span_ft),
        spacing_in=float(spacing_in),
        D_psf=float(dead_load_psf),
        L_psf=float(floor_live_load_psf),
        Lr_psf=float(roof_live_load_psf),
        S_psf=float(snow_load_psf),
        bearing_length_in=float(bearing_length_in),
        web_stiffeners=bool(web_stiffeners),
    )


def check_wall_stud(
    name: str,
    size: str,
    wall_height_ft: float,
    species: str = "DF-L",
    grade: str = "No. 2",
    spacing_in: float = 16.0,
    wind_pressure_psf: float = 0.0,
    axial_dead_plf: float = 0.0,
    axial_live_plf: float = 0.0,
    axial_roof_live_plf: float = 0.0,
    axial_snow_plf: float = 0.0,
    plies: int = 1,
    support_species: str = "SPF",
    deflection_ratio: int = 360,
) -> dict:
    """Check a bearing wall stud for combined axial + out-of-plane wind bending per NDS, including column stability (CP), bending, deflection and plate crushing.

    Axial loads are in plf of wall (the tool converts to per-stud using spacing_in).
    wind_pressure_psf: out-of-plane component wind pressure on the wall (0 if interior wall).
    size like "2x6"; species DF-L/SP/SPF/HF; grade "No. 2" etc."""
    catalog_error = _catalog_size_error(
        normalize_species(species), normalize_member_size(size)
    )
    if catalog_error:
        return catalog_error
    return check_stud(
        name=name,
        system="Wall",
        application="Stud",
        size=normalize_member_size(size),
        species=normalize_species(species),
        grade=_grade(grade),
        length_ft=float(wall_height_ft),
        spacing_in=float(spacing_in),
        wind_psf=float(wind_pressure_psf),
        D_plf=float(axial_dead_plf),
        L_plf=float(axial_live_plf),
        Lr_plf=float(axial_roof_live_plf),
        S_plf=float(axial_snow_plf),
        support_species=normalize_species(support_species),
        plies=int(plies),
        defl_ratio=int(deflection_ratio),
    )


def check_wood_post(
    name: str,
    size: str,
    height_ft: float,
    species: str = "DF-L",
    grade: str = "No. 2",
    dead_load_lbs: float = 0.0,
    live_load_lbs: float = 0.0,
    roof_live_load_lbs: float = 0.0,
    snow_load_lbs: float = 0.0,
    plies: int = 1,
    support_species: str = "SPF",
    bearing_length_in: Optional[float] = None,
) -> dict:
    """Check a wood post/column per NDS: slenderness, axial compression with CP buckling factor, and bearing on the supporting plate.

    Loads are total axial loads on the post in lbs by load type (dead, floor live, roof live, snow).
    size like "4x4", "6x6", or EWP like "3.5x5.25" with species "PSL"."""
    catalog_error = _catalog_size_error(
        normalize_species(species), normalize_member_size(size)
    )
    if catalog_error:
        return catalog_error
    return check_post(
        name=name,
        system="Post",
        application="Column",
        size=normalize_member_size(size),
        species=normalize_species(species),
        grade=_grade(grade),
        length_ft=float(height_ft),
        D_lbs=float(dead_load_lbs),
        L_lbs=float(live_load_lbs),
        Lr_lbs=float(roof_live_load_lbs),
        S_lbs=float(snow_load_lbs),
        support_species=normalize_species(support_species),
        plies=int(plies),
        bearing_length_in=float(bearing_length_in) if bearing_length_in else None,
    )


def check_strip_or_pad_footing(
    name: str,
    footing_type: str,
    width_ft: float,
    thickness_in: float,
    dead_load_kips: float,
    length_ft: float = 1.0,
    live_load_kips: float = 0.0,
    snow_load_kips: float = 0.0,
    soil_bearing_psf: float = 2000.0,
    concrete_fc_psi: float = 3000.0,
    rebar_size: str = "#4",
    rebar_spacing_in: float = 12.0,
    cover_in: float = 3.0,
    post_size_in: float = 5.5,
    longitudinal_bar_size: str = "#4",
    n_longitudinal_bars: int = 2,
) -> dict:
    """Check a concrete footing per ACI: soil bearing pressure, one-way shear, punching shear (pads), flexural reinforcement, and (strip footings) longitudinal T&S reinforcement.

    footing_type: "continuous" (strip footing under a wall — loads in kips PER FOOT of wall, width_ft is the footing width, length_ft stays 1.0) or "pad" (isolated footing under a post — loads are TOTAL kips, width_ft x length_ft is the plan size).
    thickness_in: footing depth in inches. rebar_size like "#4"/"#5" (transverse bars).
    longitudinal_bar_size / n_longitudinal_bars: continuous bars along a strip footing (default (2) #4 — one top + one bottom).
    """
    if footing_type.lower().startswith("pad"):
        inp = PostFootingInputs(
            B1=float(width_ft),
            B2=float(length_ft if length_ft > 1.0 else width_ft),
            H=float(thickness_in),
            cover=float(cover_in),
            a1=float(post_size_in),
            a2=float(post_size_in),
            fc=float(concrete_fc_psi),
            q_allow_ton=float(soil_bearing_psf) / 2000.0,
            DL=float(dead_load_kips) * 1000.0,
            LL=float(live_load_kips) * 1000.0,
            SL=float(snow_load_kips) * 1000.0,
            bar_B1=rebar_size,
            bar_B2=rebar_size,
            bar_spacing_B1=float(rebar_spacing_in),
            bar_spacing_B2=float(rebar_spacing_in),
        )
        return check_post_footing(inp=inp)
    return check_footing(
        name=name,
        footing_type="continuous",
        size_x_ft=float(width_ft),
        size_y_ft=1.0,
        thickness_in=float(thickness_in),
        axial_D_kips=float(dead_load_kips),
        axial_L_kips=float(live_load_kips),
        axial_S_kips=float(snow_load_kips),
        q_allow_psf=float(soil_bearing_psf),
        f_prime_c=float(concrete_fc_psi),
        rebar_size=rebar_size,
        rebar_spacing_in=float(rebar_spacing_in),
        cover_in=float(cover_in),
        long_bar_size=longitudinal_bar_size,
        n_long_bars=int(n_longitudinal_bars),
    )


def check_wood_shear_wall(
    seismic_shear_lbs: float,
    wind_shear_lbs: float,
    wall_height_ft: float,
    wall_length_ft: float,
    panel_thickness: str = "15/32",
    nail_edge_spacing_in: int = 6,
    fastener_type: str = "10d",
    sheathing_both_sides: bool = False,
    panel_material: str = "OSB",
    holdown_model: str = "HDU2",
    chord_studs: str = "(2) 2x6",
    chord_species: str = "DF No. 2",
    stud_spacing_in: int = 16,
    sds: float = 0.5,
) -> dict:
    """Check a segmented wood structural panel shear wall per SDPWS: unit shear capacity vs demand, holdown/chord tension, compression chords, sill anchorage, and drift.

    seismic_shear_lbs / wind_shear_lbs: ASD shear demand at the top of this wall segment (0.7E and 0.6W already applied).
    panel_thickness: "3/8", "7/16", or "15/32". fastener_type: "8d" or "10d". nail_edge_spacing_in: 6, 4, 3, or 2.
    holdown_model: Simpson model like "HDU2", "HDU4", "HDU5". sds: seismic design parameter SDS (from the seismic hazard tool).
    """
    inp = ShearWallInputs(
        wall_type="SEGMENT",
        Vs=float(seismic_shear_lbs),
        Vw=float(wind_shear_lbs),
        wall_height=float(wall_height_ft),
        wall_length_total=float(wall_length_ft),
        panels=[float(wall_length_ft)],
        sum_Li=float(wall_length_ft),
        bs_perf=float(wall_length_ft),
        panel_thickness=panel_thickness,
        nail_edge_spacing=int(nail_edge_spacing_in),
        fastener_type=fastener_type,
        sheathing_both_sides=bool(sheathing_both_sides),
        panel_material=panel_material,
        panel_grade="APA Rated Sheathing",
        holdown_model=holdown_model,
        chord_studs=chord_studs,
        chord_species=chord_species,
        sill_plate_type="(1)-2x",
        top_sill_species=chord_species.split()[0],
        stud_spacing=int(stud_spacing_in),
        SDS=float(sds),
    )
    return check_shear_wall(inp=inp)


def check_cmu_masonry_wall(
    wall_height_ft: float,
    panel_length_ft: float = 4.0,
    thickness_in: float = 7.625,
    fm_psi: float = 1900.0,
    grout: str = "full",
    bar_size: int = 5,
    bar_spacing_in: float = 24.0,
    wind_pressure_psf: float = 0.0,
    sds: float = 0.5,
    axial_dead_plf: float = 0.0,
    axial_live_plf: float = 0.0,
    axial_snow_plf: float = 0.0,
) -> dict:
    """Check a reinforced CMU masonry wall per TMS 402 strength design for out-of-plane wind/seismic bending plus axial load.

    thickness_in: actual CMU thickness — 5.625 (6"), 7.625 (8"), 9.625 (10"), 11.625 (12" nominal).
    grout: "full", "partial", or "ungrouted". bar_size: vertical bar number (5 = #5). fm_psi: masonry compressive strength f'm.
    sds: seismic SDS parameter (from the seismic hazard tool)."""
    inp = MasonryWallInputs(
        wall_height_ft=float(wall_height_ft),
        panel_length_ft=float(panel_length_ft),
        thickness_in=float(thickness_in),
        fm_psi=float(fm_psi),
        grout=grout,
        bar_size=int(bar_size),
        bar_spacing_in=float(bar_spacing_in),
        wind_psf=float(wind_pressure_psf),
        SDS=float(sds),
        P_dead_plf=float(axial_dead_plf),
        P_live_plf=float(axial_live_plf),
        P_snow_plf=float(axial_snow_plf),
    )
    return check_masonry_wall(inp=inp)


def check_cmu_shear_wall(
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
    """Check a CMU wall as an IN-PLANE shear wall per TMS 402 strength design: in-plane shear (Vnm + Vns per Eq. 9-20/9-21), shear friction (§9.3.6.5), in-plane flexure by strain compatibility, in-plane axial, plus the out-of-plane checks and biaxial interaction.

    seismic_shear_lbs / wind_shear_lbs: ASD shear demand on this wall (0.7E / 0.6W — same convention as check_wood_shear_wall); converted to strength level internally.
    thickness_in: actual CMU — 5.625 (6"), 7.625 (8"), 9.625 (10"), 11.625 (12" nominal).
    horizontal bars resist in-plane shear (Vns); vertical bars are the distributed flexural steel.
    Use this (not check_cmu_masonry_wall) whenever a masonry wall resists lateral story shear.
    """
    return check_masonry_shear_wall(
        name=name,
        wall_height_ft=float(wall_height_ft),
        wall_length_ft=float(wall_length_ft),
        thickness_in=float(thickness_in),
        fm_psi=float(fm_psi),
        grout=grout,
        vertical_bar_size=int(vertical_bar_size),
        vertical_bar_spacing_in=float(vertical_bar_spacing_in),
        horizontal_bar_size=int(horizontal_bar_size),
        horizontal_bar_spacing_in=float(horizontal_bar_spacing_in),
        seismic_shear_lbs=float(seismic_shear_lbs),
        wind_shear_lbs=float(wind_shear_lbs),
        axial_dead_plf=float(axial_dead_plf),
        sds=float(sds),
        wind_pressure_psf=float(wind_pressure_psf),
    )


def compute_wind_and_seismic_loads(
    building_length_ft: float,
    building_width_ft: float,
    number_of_stories: int = 1,
    story_height_ft: float = 9.0,
    mean_roof_height_ft: Optional[float] = None,
    wind_speed_mph: float = 0.0,
    exposure: str = "B",
    roof_type: str = "gable",
    roof_pitch_deg: Optional[float] = None,
    eave_height_ft: Optional[float] = None,
    ridge_height_ft: Optional[float] = None,
    enclosure: str = "enclosed",
    ground_elevation_ft: float = 0.0,
    Kzt: float = 1.0,
    sds: float = 0.0,
    sd1: float = 0.0,
    s1: float = 0.0,
    risk_category: str = "II",
    response_modification_R: float = 6.5,
    redundancy_rho: float = 1.0,
    roof_dead_psf: float = 15.0,
    floor_dead_psf: float = 15.0,
    wall_dead_psf: float = 12.0,
) -> dict:
    """Generate ASCE 7-22 lateral loads for a regular rectangular building: full wind MWFRS directional procedure (Ch. 27 — §26.11 gust factor, per-height qz, zoned wall/roof Cp per Fig. 27.3-1, 4 load cases Wind 0/90 x GCpi±, §27.1.5 minimums) and seismic ELF story shears (§12.8 — Ta, Cs, V = Cs·W), both at ASD level (0.6W / 0.7E).

    Provide wind_speed_mph (from get_wind_speed) and/or sds+sd1 (from get_seismic_parameters). exposure: B, C, or D. roof_type: gable or flat; roof_pitch_deg e.g. 4:12 -> 18.4 (a gable ridge is assumed parallel to the long dimension). enclosure: enclosed / partially enclosed / partially open (sets GCpi 0.18/0.55/0.46). ground_elevation_ft sets Ke. R defaults to 6.5 (light-frame WSP shear walls). X-direction = load along building length; Y = along width.
    Returns per-story Vs/Vw plus the full wind package (gust factor derivation, velocity pressure table, per-zone pressure/force tables per load case, overall loading vs code minimum).
    """
    extracted = {
        "building_geometry": {
            "length_ft": float(building_length_ft),
            "width_ft": float(building_width_ft),
            "number_of_stories": int(number_of_stories),
            "story_heights_ft": [float(story_height_ft)] * int(number_of_stories),
            "mean_roof_height_ft": (
                float(mean_roof_height_ft) if mean_roof_height_ft else None
            ),
            "eave_height_ft": float(eave_height_ft) if eave_height_ft else None,
            "ridge_height_ft": float(ridge_height_ft) if ridge_height_ft else None,
            "roof_type": str(roof_type).lower(),
            "roof_pitch_deg": (
                float(roof_pitch_deg) if roof_pitch_deg is not None else None
            ),
        },
        "wind_context": {
            "exposure_category": str(exposure).upper(),
            "Kzt": float(Kzt),
            "enclosure_classification": str(enclosure).lower(),
            "ground_elevation_ft": float(ground_elevation_ft),
        },
        "seismic_context": {
            "response_modification_R": float(response_modification_R),
            "redundancy_rho": float(redundancy_rho),
        },
        "design_loads": {
            "roof_dead": float(roof_dead_psf),
            "floor_dead": float(floor_dead_psf),
            "wall_dead": float(wall_dead_psf),
        },
    }
    hazard = {
        "wind": {"wind_speed_mph": float(wind_speed_mph)} if wind_speed_mph else {},
        "seismic": (
            {"sds": float(sds), "sd1": float(sd1), "s1": float(s1)} if sds else {}
        ),
        "input": {"risk_category": str(risk_category)},
    }
    result = compute_lateral_loads(extracted, hazard)
    if not result:
        return {
            "error": "Could not compute — need building length/width plus wind_speed_mph "
            "and/or sds. Use get_wind_speed / get_seismic_parameters first."
        }
    # The full directional package (4 load cases of zone tables) is too large
    # for the chat context — stash it and hand the LLM a compact summary; the
    # calculation sheet resolves directional_ref back to the full package.
    wind_meta = (result.get("meta") or {}).get("wind") or {}
    pkg = wind_meta.pop("directional", None)
    if pkg:
        wind_meta["directional_ref"] = _stash_wind_detail(pkg)
        wind_meta["overall_loading"] = pkg.get("summary")
        wind_meta["note"] = (
            "Full per-zone pressure/force tables (4 load cases) are on the "
            "calculation sheet. Quote story shears, base shears, and governing "
            "cases from this summary."
        )
    return result


def compute_component_wind_pressure(
    wind_speed_mph: float,
    mean_roof_height_ft: float,
    effective_wind_area_sf: float,
    exposure: str = "B",
    zone: int = 4,
    Kzt: float = 1.0,
) -> dict:
    """Components & Cladding design wind pressure per ASCE 7-22 Fig. 30.3-1 (walls, enclosed buildings, h <= 60 ft) — for doors, windows, wall studs, cladding, and their attachments.

    effective_wind_area_sf: component area (e.g., a 9x7 door = 63 sf; a stud = span x max(spacing, span/3)).
    zone: 4 = interior wall zone, 5 = edge/corner strips (width a = 10% of least building width, min 3 ft).
    Returns the strength-level pressure; multiply by 0.6 for ASD combinations."""
    out = cast(
        dict[str, Any],
        candc_component_pressure(
            V_mph=float(wind_speed_mph),
            mean_roof_height_ft=float(mean_roof_height_ft),
            effective_area_sf=float(effective_wind_area_sf),
            exposure=str(exposure).upper(),
            zone=int(zone),
            Kzt=float(Kzt),
        ),
    )
    out["notes"] = (
        "Fig. 30.3-1 walls, GCpi ±0.18, 16 psf minimum applied; "
        "Kd = 0.85. Use zone 5 within a = 0.1·least width of corners."
    )
    return out


# ================================================================
#  Hazard lookup tools (USGS / ASCE APIs — USA only)
# ================================================================


async def get_wind_speed(longitude: float, latitude: float) -> dict:
    """Look up the ASCE 7 basic (ultimate) wind speed in mph for a USA location by longitude/latitude."""
    return await get_wind_data(long=float(longitude), lat=float(latitude))


async def get_ground_snow_load(
    longitude: float, latitude: float, risk_category: str = "II"
) -> dict:
    """Look up the ASCE 7 ground snow load in psf for a USA location by longitude/latitude. risk_category: I, II, III, or IV (most residential = II)."""
    return await get_snow_data(
        long=float(longitude), lat=float(latitude), risk_category=risk_category
    )


async def get_seismic_parameters(
    longitude: float,
    latitude: float,
    risk_category: str = "II",
    site_class: str = "Default",
) -> dict:
    """Look up ASCE 7-22 seismic design parameters (SS, S1, SDS, SD1, SDC...) from USGS for a USA location. risk_category: I-IV. site_class: Default, A, B, BC, C, CD, D, DE, E, or F."""
    return await get_seismic_data(
        long=float(longitude),
        lat=float(latitude),
        risk_category=risk_category,
        site_class=site_class,
    )


def build_tools() -> list[Callable[..., Any]]:
    """All chatbot tools for one authenticated user."""
    tools: list[Callable[..., Any]] = [
        check_beam_or_joist,
        check_i_joist,
        check_wall_stud,
        check_wood_post,
        check_strip_or_pad_footing,
        check_wood_shear_wall,
        check_cmu_masonry_wall,
        check_cmu_shear_wall,
        get_wind_speed,
        get_ground_snow_load,
        get_seismic_parameters,
        compute_wind_and_seismic_loads,
        compute_component_wind_pressure,
    ]
    return tools


# ================================================================
#  OpenAI function-calling schema conversion
# ================================================================

_PYTHON_TYPE_TO_JSON: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def _python_type_to_json_schema(annotation: Any) -> dict[str, Any]:
    """Convert a Python type annotation to a JSON Schema type."""
    # Handle Optional[X]
    origin = getattr(annotation, "__origin__", None)
    args = getattr(annotation, "__args__", ())
    if origin is type(None):
        return {"type": "null"}
    # Optional[X] shows up as Union[X, None]
    if origin is not None and str(origin) in ("<class 'typing.Union'>", "typing.Union"):
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            schema = _python_type_to_json_schema(non_none[0])
            return schema
    # Direct type match
    if annotation in _PYTHON_TYPE_TO_JSON:
        return {"type": _PYTHON_TYPE_TO_JSON[annotation]}
    # Fallback
    return {"type": "string"}


def _tool_to_openai_schema(fn: Callable) -> dict[str, Any]:
    """Convert a single tool function to an OpenAI function-calling schema."""
    sig = inspect.signature(fn)
    try:
        hints = get_type_hints(fn)
    except Exception:
        hints = {}

    doc = inspect.getdoc(fn) or ""
    # First paragraph of docstring = description
    desc_lines: list[str] = []
    for line in doc.split("\n"):
        line = line.strip()
        if not line and desc_lines:
            break
        desc_lines.append(line)
    description = " ".join(desc_lines)

    properties: dict[str, Any] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        if param_name in ("return",):
            continue
        annotation = hints.get(param_name, str)
        prop = _python_type_to_json_schema(annotation)
        # Add description from docstring if we can find it
        # Look for "param_name:" or "param_name =" patterns in the docstring
        param_doc_match = re.search(
            rf"\b{re.escape(param_name)}[:\s]+(.*?)(?:\n\s*\w|\Z)",
            doc,
            re.DOTALL,
        )
        if param_doc_match:
            param_desc = param_doc_match.group(1).strip()
            param_desc = re.sub(r"\s+", " ", param_desc)[:200]
            prop["description"] = param_desc

        # Enum-like hints from docstring (e.g., "zone: 4 = interior, 5 = corner")
        if param.default is not inspect.Parameter.empty:
            prop["default"] = param.default
        else:
            required.append(param_name)

        properties[param_name] = prop

    schema: dict[str, Any] = {
        "type": "function",
        "function": {
            "name": fn.__name__,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
            },
        },
    }
    if required:
        schema["function"]["parameters"]["required"] = required
    return schema


def tools_to_openai_schema(tools: list[Callable]) -> list[dict[str, Any]]:
    """Convert a list of tool callables to OpenAI function-calling tool schemas."""
    return [_tool_to_openai_schema(fn) for fn in tools]


def build_tool_dispatch(tools: list[Callable]) -> dict[str, Callable]:
    """Build a name → callable dispatch map for the tool executor."""
    return {fn.__name__: fn for fn in tools}
