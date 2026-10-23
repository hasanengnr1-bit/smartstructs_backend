# """
# Structural Analysis & Design Schema v2
# =======================================

# Architecture:
#   1. PLAN EXTRACTION (AI/LLM) → Extracts geometry, member sizes, notes from plans
#   2. HAZARD LOOKUP (deterministic Python) → USGS + ASCE APIs for seismic/wind/snow
#   3. CALC ENGINE → Merges both → design checks per NDS/IBC/ASCE 7
#   4. HITL REVIEW → Engineer reviews & overrides

# Field annotations:
#   # SKIP_EXTRACTION: <reason> | default: <value>
#     → The AI extractor should NOT try to find this in drawings.
#       It either comes from the hazard lookup, the calc engine,
#       or has a safe default. Saves tokens.

#   # EXTRACT_IF_VISIBLE: <guidance>
#     → Only extract if explicitly shown on plans. Don't guess.

#   uncertain_areas / *_note fields → HITL review flags (keep on all models)
# """

# from pydantic import BaseModel, Field
# from typing import List, Optional, Literal


# # =====================================================================
# #  ENUMS & SHARED TYPES
# # =====================================================================

# SupportMaterial = Literal[
#     "DF-L", "SP", "SPF", "HF",
#     "concrete", "steel",
#     "LVL", "PSL", "LSL", "glulam"
# ]


# class LumberSpec(BaseModel):
#     species: Literal["DF-L", "SP", "SPF", "HF"] | None = None
#     grade: Literal["SS", "No.1", "No.2", "No.3", "Stud"] | None = None
#     species_grade_note: str

# class MemberSize(BaseModel):
#     """Parsed member size. AI extracts size_label string, calc engine parses to dimensions."""
#     size_label: str | None = None               # EXTRACT: "2x10", "3-1/2 x 11-7/8", "LVL 1-3/4x14"
#     nominal_width_in: int | None = None  # SKIP_EXTRACTION: parsed from size_label by calc engine
#     nominal_depth_in: int | None = None  # SKIP_EXTRACTION: parsed from size_label by calc engine


# # =====================================================================
# #  SECTION 1: HAZARD LOOKUP  (deterministic — NO AI needed)
# # =====================================================================
# #
# # The AI extractor provides: address → geocode → lat/lng
# # The engineer confirms: risk_category, site_class
# # Then a simple Python function calls the APIs below.
# # NONE of these fields are extracted from drawings.
# # =====================================================================

# class HazardLookupInput(BaseModel):
#     """
#     Input to the hazard lookup tool.
#     latitude/longitude: geocoded from project address
#     risk_category: from plans general notes or engineer input (most residential = "II")
#     site_class: from geotech report or "Default"
#     """
#     latitude: float
#     longitude: float
#     risk_category: Literal["I", "II", "III", "IV"] = "II"
#     site_class: Literal["Default", "A", "B", "BC", "C", "CD", "D", "DE", "E", "F"] = "Default"
#     asce7_edition: Literal["7-10", "7-16", "7-22"] = "7-22"


# class SeismicLookupResult(BaseModel):
#     """
#     From USGS free API (no key needed):
#     https://earthquake.usgs.gov/ws/designmaps/asce7-22.json
#       ?latitude={lat}&longitude={lng}&riskCategory={rc}&siteClass={sc}
#     """
#     ss: float | None = None                     # MCEr 0.2s spectral acceleration
#     s1: float | None = None                     # MCEr 1.0s spectral acceleration
#     sms: float | None = None                    # site-modified short period
#     sm1: float | None = None                    # site-modified 1-second
#     sds: float | None = None                    # design short period
#     sd1: float | None = None                    # design 1-second
#     sdc: str | None = None                      # seismic design category (A-F)
#     pgam: float | None = None                   # peak ground acceleration
#     t0: float | None = None                     # 0.2 × Ts
#     ts: float | None = None                     # SD1/SDS
#     tl: float | None = None                     # long-period transition


# class WindLookupResult(BaseModel):
#     """
#     From ASCE Hazard Tool API (paid, needs API key):
#     https://api-hazard.asce.org/...
#     Returns wind speed for all risk categories + region flags.
#     """
#     wind_speed_mph: float | None = None         # for the selected risk category
#     is_hurricane_prone: bool | None = None
#     is_wind_borne_debris: bool | None = None
#     is_special_wind_zone: bool | None = None


# class SnowLookupResult(BaseModel):
#     """From ASCE Hazard Tool API."""
#     ground_snow_load_psf: float | None = None   # pg for selected risk category
#     ground_snow_20yr_mri_psf: float | None = None
#     winter_wind_parameter: str | None = None
#     case_study_required: bool | None = None     # True → region needs site-specific study


# class HazardLookupResult(BaseModel):
#     """Complete output of the hazard lookup tool. All deterministic, no AI."""
#     input: HazardLookupInput
#     seismic: SeismicLookupResult | None = None
#     wind: WindLookupResult | None = None
#     snow: SnowLookupResult | None = None
#     lookup_timestamp: str | None = None
#     lookup_errors: List[str] | None = None


# # =====================================================================
# #  SECTION 2: PLAN-EXTRACTED PROJECT CONTEXT
# # =====================================================================

# class ProjectData(BaseModel):
#     project_name: str | None = None
#     address: str                                  # EXTRACT: from title block
#     latitude: float | None = None                        # SKIP_EXTRACTION: geocoded from address
#     longitude: float | None = None                       # SKIP_EXTRACTION: geocoded from address
#     stories: int | None = None                           # EXTRACT: from plans
#     building_use: Literal["residential", "commercial", "mixed_use"] | None = None  # EXTRACT_IF_VISIBLE
#     design_code: str | None = None                       # EXTRACT_IF_VISIBLE: from general notes ("IBC 2021", "IRC 2021")
#     design_method: Literal["ASD", "LRFD"] = "ASD"  # SKIP_EXTRACTION | default: "ASD" (standard for light-frame wood)
#     risk_category: Literal["I", "II", "III", "IV"] = "II"  # EXTRACT_IF_VISIBLE | default: "II" for residential
#     uncertain_areas: List[str] | None = None


# class BuildingGeometry(BaseModel):
#     """
#     Overall building envelope — extracted from arch plans.
#     Calc engine uses for wind/seismic force distribution.
#     """
#     length_ft: float | None = None              # EXTRACT: plan dimension parallel to ridge
#     width_ft: float | None = None               # EXTRACT: plan dimension perpendicular to ridge
#     mean_roof_height_ft: float | None = None    # EXTRACT_IF_VISIBLE: or calc engine computes from eave + ridge
#     eave_height_ft: float | None = None         # EXTRACT: from elevations
#     ridge_height_ft: float | None = None        # EXTRACT: from elevations
#     roof_type: Literal["gable", "hip", "flat", "mono_slope", "gambrel", "mansard"] | None = None
#     number_of_stories: int | None = None
#     story_heights_ft: List[float] | None = None        # EXTRACT: from sections/elevations, bottom-up
#     floor_area_per_story_sf: List[float] | None = None  # EXTRACT_IF_VISIBLE: or calc engine computes L×W
#     geometry_note: str | None = None
#     uncertain_areas: List[str] | None = None


# class PlanExtractedWindContext(BaseModel):
#     """
#     Wind info that CAN be read from plans or inferred from site.
#     Actual wind speed comes from hazard lookup — NOT extracted here.
#     """
#     exposure_category: Literal["B", "C", "D"] | None = None       # EXTRACT_IF_VISIBLE: from general notes
#     exposure_justification: str | None = None                      # EXTRACT_IF_VISIBLE: "suburban", "open terrain"
#     enclosure_classification: Literal["enclosed", "partially_enclosed", "open"] = "enclosed"  # SKIP_EXTRACTION | default: "enclosed"
#     topography_flat: bool = True                            # SKIP_EXTRACTION | default: True (Kzt=1.0)
#     uncertain_areas: List[str] | None = None


# class PlanExtractedSnowContext(BaseModel):
#     """
#     Snow context from plans. Ground snow load pg comes from hazard lookup.
#     These factors convert pg → flat roof snow pf. Usually not on drawings.
#     """
#     exposure_factor_Ce: float = 1.0      # SKIP_EXTRACTION | default: 1.0 (partially exposed)
#     thermal_factor_Ct: float = 1.0       # SKIP_EXTRACTION | default: 1.0 (heated structure)
#     importance_factor_Is: float = 1.0    # SKIP_EXTRACTION | default: 1.0 (Risk Cat II)
#     snow_note: str | None = None
#     uncertain_areas: List[str] | None = None


# class PlanExtractedSeismicContext(BaseModel):
#     """
#     Seismic info from structural plans / geotech report.
#     SDS, SD1, SDC come from the USGS API — NOT extracted here.
#     """
#     site_class: str | None = None                    # EXTRACT_IF_VISIBLE: from geotech report / general notes
#     site_class_source: str | None = None             # "geotech report", "assumed Default"
#     seismic_force_resisting_system: str = "Light-frame wood walls with wood structural panels"  # SKIP_EXTRACTION | default for residential
#     response_modification_R: float = 6.5            # SKIP_EXTRACTION | default: 6.5 (ASCE 7 Table 12.2-1 light-frame WSP)
#     overstrength_omega0: float = 3.0                # SKIP_EXTRACTION | default: 3.0
#     deflection_amplification_Cd: float = 4.0        # SKIP_EXTRACTION | default: 4.0
#     redundancy_rho: float = 1.0                     # SKIP_EXTRACTION | default: 1.0 (1.3 if irregular)
#     uncertain_areas: List[str] | None = None


# class SoilData(BaseModel):
#     """From geotech report — if available. Many residential projects don't have one."""
#     soil_bearing_pressure_psf: float | None = None         # EXTRACT_IF_VISIBLE: from geotech or general notes
#     soil_type_description: str | None = None               # EXTRACT_IF_VISIBLE
#     frost_line_depth_in: float | None = None               # EXTRACT_IF_VISIBLE: from general notes or local code
#     frost_line_note: str | None = None
#     uncertain_areas: List[str] | None = None


# # =====================================================================
# #  SECTION 3: STRUCTURAL MEMBERS  (AI-extracted from plans)
# # =====================================================================
# #
# # For each member model:
# #   - Fields the AI SHOULD extract are unmarked
# #   - Fields the AI should SKIP are marked
# #   - service_conditions / deflection_criteria → always SKIP, use defaults
# #   - ewp_spec → only populate if plans explicitly call out an EWP
# # =====================================================================

# # ----------------------------- Roof System ----------------------------

# class RoofRafter(BaseModel):
#     zone: str
#     size: str                                     # EXTRACT: "2x8", "2x10" etc.
#     number_of_plies: int = 1
#     lumber_spec: LumberSpec | None = None
#     clear_span_ft: float | None = None
#     clear_span_note: str = ""
#     overhang_in: float | None = None
#     overhang_note: str = ""
#     roof_pitch: str | None = None                        # EXTRACT: "6/12", "4/12" etc.
#     available_support_bearing_in: float | None = None
#     available_support_bearing_note: str = ""
#     support_material: SupportMaterial | None = None
#     spacing_in: float | None = None
#     roof_dead_load_psf: float | None = None
#     roof_live_load_psf: float = 20.0              # SKIP_EXTRACTION | default: 20 psf (IBC Table 1607.1)
#     roof_snow_load_psf: float | None = None              # SKIP_EXTRACTION: calc engine computes from hazard pg × Ce × Ct × Is × Cs
#     repetitive_member: bool = True                # SKIP_EXTRACTION | default: True (rafters are almost always repetitive)
#     uncertain_areas: List[str] | None = None


# class CeilingJoist(BaseModel):
#     zone: str
#     size: str
#     number_of_plies: int = 1
#     lumber_spec: LumberSpec | None = None
#     clear_span_ft: float | None = None
#     clear_span_note: str = ""
#     available_support_bearing_in: float | None = None
#     available_support_bearing_note: str = ""
#     support_material: SupportMaterial | None = None
#     spacing_in: float | None = None
#     ceiling_dead_load_psf: float | None = None
#     attic_live_load_psf: float | None = None             # EXTRACT_IF_VISIBLE: 10 psf no storage, 20 psf limited storage
#     attic_use: Literal["no_access", "limited_storage", "habitable"] = "limited_storage"
#     repetitive_member: bool = True                # SKIP_EXTRACTION | default: True
#     uncertain_areas: List[str] | None = None


# class RidgeBeam(BaseModel):
#     zone: str
#     size: str
#     number_of_plies: int
#     lumber_spec: LumberSpec | None = None
#     clear_span_ft: float | None = None
#     clear_span_note: str = ""
#     available_support_bearing_in: float | None = None
#     available_support_bearing_note: str = ""
#     support_material: SupportMaterial | None = None
#     tributary_width_ft: float | None = None
#     tributary_width_note: str = ""
#     roof_dead_load_psf: float | None = None
#     roof_live_load_psf: float = 20.0              # SKIP_EXTRACTION | default: 20 psf
#     roof_snow_load_psf: float | None = None              # SKIP_EXTRACTION: from hazard + calc engine
#     uncertain_areas: List[str] | None = None


# class HipValleyRafter(BaseModel):
#     zone: str
#     member_type: Literal["hip", "valley"]
#     size: str
#     number_of_plies: int
#     lumber_spec: LumberSpec | None = None
#     clear_span_ft: float | None = None
#     clear_span_note: str = ""
#     roof_pitch: str | None = None
#     available_support_bearing_in: float | None = None
#     available_support_bearing_note: str = ""
#     support_material: SupportMaterial | None = None
#     tributary_width_ft: float | None = None
#     tributary_width_note: str = ""
#     roof_dead_load_psf: float | None = None
#     roof_live_load_psf: float = 20.0              # SKIP_EXTRACTION | default: 20 psf
#     roof_snow_load_psf: float | None = None              # SKIP_EXTRACTION: from hazard + calc engine
#     uncertain_areas: List[str] | None = None


# class RoofDropBeam(BaseModel):
#     zone: str
#     size: str
#     number_of_plies: int
#     lumber_spec: LumberSpec | None = None
#     clear_span_ft: float | None = None
#     clear_span_note: str = ""
#     roof_pitch: str | None = None
#     available_support_bearing_in: float | None = None
#     available_support_bearing_note: str = ""
#     support_material: SupportMaterial | None = None
#     tributary_width_ft: float | None = None
#     tributary_width_note: str = ""
#     roof_dead_load_psf: float | None = None
#     roof_live_load_psf: float = 20.0              # SKIP_EXTRACTION | default: 20 psf
#     roof_snow_load_psf: float | None = None              # SKIP_EXTRACTION: from hazard + calc engine
#     uncertain_areas: List[str] | None = None


# class RoofFlushBeam(BaseModel):
#     zone: str
#     size: str
#     number_of_plies: int
#     lumber_spec: LumberSpec | None = None
#     clear_span_ft: float | None = None
#     clear_span_note: str = ""
#     available_support_bearing_in: float | None = None
#     available_support_bearing_note: str = ""
#     support_material: SupportMaterial | None = None
#     hanger_bucket_seat_depth_in: float | None = None
#     hanger_seat_depth_note: str = ""
#     hanger_carrier_material: str | None = None
#     tributary_width_ft: float | None = None
#     tributary_width_note: str = ""
#     roof_dead_load_psf: float | None = None
#     roof_live_load_psf: float = 20.0              # SKIP_EXTRACTION | default: 20 psf
#     roof_snow_load_psf: float | None = None              # SKIP_EXTRACTION: from hazard + calc engine
#     uncertain_areas: List[str] | None = None


# class RoofSystemData(BaseModel):
#     roof_rafters: List[RoofRafter]
#     ceiling_joists: List[CeilingJoist]
#     ridge_beams: List[RidgeBeam]
#     hip_valley_rafters: List[HipValleyRafter]
#     roof_drop_beams: List[RoofDropBeam]
#     roof_flush_beams: List[RoofFlushBeam]


# # ----------------------------- Floor System ----------------------------

# class FloorJoist(BaseModel):
#     zone: str
#     size: str
#     number_of_plies: int = 1
#     lumber_spec: LumberSpec | None = None
#     clear_span_ft: float | None = None
#     clear_span_note: str | None = ""
#     cantilever_ft: float | None = None
#     cantilever_note: str | None = None
#     available_support_bearing_in: float | None = None
#     available_support_bearing_note: str | None = ""
#     support_material: SupportMaterial | None = None
#     spacing_in: float | None = None
#     dead_load_psf: float | None = None
#     dead_load_note: str | None = None
#     floor_live_load_psf: float = 40.0             # SKIP_EXTRACTION | default: 40 psf (residential, IBC Table 1607.1)
#     floor_live_load_note: str | None = None
#     repetitive_member: bool = True                # SKIP_EXTRACTION | default: True
#     uncertain_areas: List[str] | None = None


# class FloorDropBeam(BaseModel):
#     zone: str
#     size: str
#     number_of_plies: int
#     lumber_spec: LumberSpec | None = None
#     clear_span_ft: float | None = None
#     clear_span_note: str | None = ""
#     available_support_bearing_in: float | None = None
#     available_support_bearing_note: str | None = ""
#     support_material: SupportMaterial | None = None
#     tributary_width_ft: float | None = None
#     tributary_width_note: str | None = ""
#     dead_load_psf: float | None = None
#     dead_load_note: str | None = None
#     floor_live_load_psf: float = 40.0             # SKIP_EXTRACTION | default: 40 psf
#     floor_live_load_note: str | None = None
#     uncertain_areas: List[str] | None = None


# class FloorFlushBeam(BaseModel):
#     zone: str
#     size: str
#     number_of_plies: int
#     lumber_spec: LumberSpec | None = None
#     clear_span_ft: float | None = None
#     clear_span_note: str | None = ""
#     available_support_bearing_in: float | None = None
#     available_support_bearing_note: str | None = ""
#     support_material: SupportMaterial | None = None
#     hanger_bucket_seat_depth_in: float | None = None
#     hanger_seat_depth_note: str | None = ""
#     hanger_carrier_material: str | None = None
#     tributary_width_ft: float | None = None
#     tributary_width_note: str | None = ""
#     dead_load_psf: float | None = None
#     dead_load_note: str | None = None
#     floor_live_load_psf: float = 40.0             # SKIP_EXTRACTION | default: 40 psf
#     floor_live_load_note: str | None = None
#     uncertain_areas: List[str] | None = None


# class FloorSystemData(BaseModel):
#     floor_joists: List[FloorJoist]
#     floor_drop_beams: List[FloorDropBeam]
#     floor_flush_beams: List[FloorFlushBeam]


# # ----------------------------- Footing System -------------------------

# class FootingProjectInfo(BaseModel):
#     concrete_fc_footings_psi: int = 2500          # EXTRACT_IF_VISIBLE | default: 2500 psi (common residential)
#     concrete_fc_slab_psi: int = 2500              # EXTRACT_IF_VISIBLE | default: 2500 psi
#     rebar_grade: str = "Grade 60"                 # SKIP_EXTRACTION | default: Grade 60
#     soil_bearing_pressure_psf: float | None = None  # EXTRACT: from geotech or general notes
#     soil_bearing_note: str = ""
#     frost_line_depth_in: float | None = None       # EXTRACT_IF_VISIBLE: from general notes
#     frost_line_note: str = ""
#     soils_engineer_required: bool | None = None
#     concrete_cover_to_soil_in: float = 3.0        # SKIP_EXTRACTION | default: 3" (ACI 318 §20.6.1)
#     uncertain_areas: List[str] | None = None


# class ContinuousStripFooting(BaseModel):
#     footing_mark: str
#     existing_condition: bool = False
#     new_load_applied: str = ""
#     location_description: str
#     supported_element: str
#     wall_type: Literal["exterior_bearing", "interior_bearing", "shear_wall", "non_bearing", "grade_beam"]
#     width_in: float | None = None
#     width_note: str = ""
#     depth_in: float | None = None
#     depth_note: str = ""
#     length_ft: float | None = None
#     length_note: str = ""
#     top_of_footing_below_grade_in: float | None = None
#     bottom_of_footing_below_grade_in: float | None = None
#     embedment_note: str = ""
#     frost_compliance: str = ""
#     longitudinal_rebar_size: int | None = None           # EXTRACT: bar number (#4 = 4)
#     longitudinal_rebar_count: int | None = None
#     transverse_rebar_size: int | None = None
#     transverse_rebar_spacing_in: float | None = None
#     rebar_orientation: str | None = None                 # SKIP_EXTRACTION | default: None — calc engine determines
#     rebar_faces: str | None = None                       # SKIP_EXTRACTION | default: None
#     rebar_lap_length_in: float | None = None             # SKIP_EXTRACTION: calc engine computes per ACI | default: None
#     concrete_cover_in: float = 3.0                # SKIP_EXTRACTION | default: 3"
#     anchor_bolt_dia_in: float = 0.5               # EXTRACT_IF_VISIBLE | default: 1/2" (IRC R403.1.6)
#     anchor_bolt_embedment_in: float = 7.0         # EXTRACT_IF_VISIBLE | default: 7" (IRC R403.1.6)
#     anchor_bolt_spacing_in: float = 72.0          # EXTRACT_IF_VISIBLE | default: 72" o.c. (IRC R403.1.6)
#     anchor_bolt_corner_spacing_in: float = 12.0   # SKIP_EXTRACTION | default: 12" from corners
#     anchor_bolt_end_of_plate_max_in: float = 12.0  # SKIP_EXTRACTION | default: 12" from plate ends
#     plate_washer_required: bool | None = None
#     plate_washer_spec: str = ""
#     sill_plate_size: str = ""
#     sill_plate_treated: bool = True               # SKIP_EXTRACTION | default: True (code required)
#     footing_note: str = ""
#     uncertain_areas: List[str] | None = None


# class PadFooting(BaseModel):
#     footing_mark: str
#     existing_condition: bool = False
#     new_load_applied: str = ""
#     location_description: str
#     supported_element: str
#     width_in: float | None = None
#     length_in: float | None = None
#     depth_in: float | None = None
#     dimension_note: str = ""
#     top_of_footing_below_grade_in: float | None = None
#     bottom_of_footing_below_grade_in: float | None = None
#     embedment_note: str = ""
#     frost_compliance: str = ""
#     rebar_size: int | None = None
#     rebar_spacing_in: float | None = None
#     rebar_orientation: str | None = None                 # SKIP_EXTRACTION | default: None
#     rebar_faces: str | None = None                       # SKIP_EXTRACTION | default: None
#     rebar_lap_length_in: float | None = None             # SKIP_EXTRACTION: calc engine computes
#     concrete_cover_in: float = 3.0                # SKIP_EXTRACTION | default: 3"
#     post_base_model: str = ""
#     post_base_anchor_type: Literal["cast_in", "epoxy_set"] | None = None
#     post_size: str = ""
#     footing_note: str = ""
#     uncertain_areas: List[str] | None = None


# class GradeBeam(BaseModel):
#     footing_mark: str
#     existing_condition: bool = False
#     new_load_applied: str = ""
#     location_description: str
#     supported_element: str
#     bearing_on: Literal["soil", "piers", "piles"] | None = None
#     width_in: float | None = None
#     depth_in: float | None = None
#     length_ft: float | None = None
#     dimension_note: str = ""
#     top_of_beam_below_grade_in: float | None = None
#     bottom_of_beam_below_grade_in: float | None = None
#     frost_compliance: str = ""
#     rebar_size: int | None = None
#     rebar_count: int | None = None
#     rebar_orientation: str | None = None                 # SKIP_EXTRACTION | default: None
#     rebar_faces: str | None = None                       # SKIP_EXTRACTION | default: None
#     rebar_lap_length_in: float | None = None             # SKIP_EXTRACTION: calc engine computes
#     concrete_cover_in: float = 3.0                # SKIP_EXTRACTION | default: 3"
#     footing_note: str = ""
#     uncertain_areas: List[str] | None = None


# class SlabOnGrade(BaseModel):
#     zone: str
#     existing_condition: bool = False
#     thickness_in: float = 4.0                     # EXTRACT_IF_VISIBLE | default: 4"
#     thickness_note: str = ""
#     reinforcement_type: Literal["rebar", "wwf", "fiber", "none"] | None = None
#     rebar_size: int | None = None
#     rebar_spacing_in: float | None = None
#     rebar_orientation: str | None = None                 # SKIP_EXTRACTION | default: None
#     wwf_designation: str | None = None
#     vapor_barrier_mils: int = 10                  # SKIP_EXTRACTION | default: 10 mil
#     sub_base_description: str = ""
#     sub_base_depth_in: float | None = None
#     control_joint_spacing_ft: float | None = None        # SKIP_EXTRACTION: calc engine uses 2-3× slab thickness rule
#     monolithic_with_footing: bool | None = None
#     slab_note: str = ""
#     uncertain_areas: List[str] | None = None


# class HoldownAnchor(BaseModel):
#     holdown_model: str
#     anchor_rod: str
#     embedment_in: float | None = None
#     location: str
#     supported_wall_mark: str
#     installation_timing: str = ""
#     holdown_note: str = ""
#     uncertain_areas: List[str] | None = None


# class FootingSystemData(BaseModel):
#     project_info: FootingProjectInfo
#     continuous_strip_footings: List[ContinuousStripFooting]
#     pad_footings: List[PadFooting]
#     grade_beams: List[GradeBeam]
#     slab_on_grade: List[SlabOnGrade]
#     holdown_anchors: List[HoldownAnchor]


# # ----------------------------- Post System ----------------------------

# class StandalonePost(BaseModel):
#     post_mark: str
#     existing_condition: bool = False
#     new_load_applied: str = ""
#     location_description: str
#     post_type: Literal["solid", "built_up"] | None = None
#     functional_type: Literal[
#         "bearing", "holdown", "corner",
#     ] | None = None
#     post_size: str                                # EXTRACT: "4x4", "6x6", etc.
#     number_of_plies: int | None = None
#     species: str = ""
#     grade: str = ""
#     species_grade_note: str = ""
#     height_ft: float | None = None
#     height_note: str = ""
#     unbraced_length_ft: float | None = None
#     unbraced_length_note: str = ""
#     effective_length_factor_Ke: float = 1.0       # SKIP_EXTRACTION | default: 1.0 (pin-pin, conservative for most cases)
#     bracing_condition: str = ""
#     tributary_area_sf: float | None = None
#     tributary_area_note: str = ""
#     point_load_lbs: float | None = None
#     point_load_note: str = ""
#     roof_dead_load_psf: float | None = None
#     roof_live_load_psf: float | None = None
#     roof_snow_load_psf: float | None = None              # SKIP_EXTRACTION: from hazard + calc engine
#     floor_dead_load_psf: float | None = None
#     floor_live_load_psf: float | None = None
#     base_connector_model: str = ""
#     base_bearing_surface: Literal[
#         "concrete_footing", "concrete_slab",
#         "wood_beam", "steel_beam", "grade"
#     ] | None = None
#     base_anchor_type: Literal["cast_in", "epoxy_set", "bolt_through"] | None = None
#     base_anchor_fastener: str = ""
#     top_connector_model: str = ""
#     top_bearing_condition: str = ""
#     holdown_model: str = ""
#     holdown_anchor_rod: str = ""
#     holdown_note: str = ""
#     post_note: str = ""
#     uncertain_areas: List[str] | None = None


# class PostData(BaseModel):
#     standalone_posts: List[StandalonePost]


# # ----------------------------- Shear Wall System ----------------------

# class BracedWallLine(BaseModel):
#     bwl_id: str
#     direction: Literal["X", "Y"] | None = None
#     story_level: str
#     bwl_total_length_ft: float | None = None
#     total_braced_length_ft: float | None = None
#     braced_panel_ids: List[str]
#     bwl_spacing_to_adjacent_ft: float | None = None
#     drag_strut_member: str | None = None
#     drag_strut_connector: str | None = None
#     bwl_note: str = ""
#     uncertain_areas: List[str] | None = None


# class ShearWall(BaseModel):
#     sw_mark: str
#     bwl_id: str
#     story_level: str
#     system_type: str
#     framing_species: str | None = None
#     pier_length_ft: float | None = None
#     wall_height_ft: float | None = None
#     aspect_ratio: float | None = None                    # SKIP_EXTRACTION: calc engine computes height/length
#     sheathing_type: str
#     sheathing_thickness_in: float | None = None
#     sheathing_faces: str = Literal["one_side" , "two_sides"]  
#     blocking: str = ""
#     edge_nail_spacing_in: float | None = None
#     field_nail_spacing_in: float | None = None
#     boundary_nail_spacing_in: float | None = None
#     nail_size: str = ""
#     requires_3x_framing: bool = False             # SKIP_EXTRACTION: calc engine determines from nail spacing
#     stud_size: str = ""
#     stud_spacing_in: float | None = None
#     holdown_model: str = ""
#     holdown_anchor_rod: str = ""
#     holdown_force_lbs: float | None = None               # SKIP_EXTRACTION: calc engine computes from lateral analysis
#     support_connector: str = ""
#     anchor_bolt_dia_in: float | None = None
#     anchor_bolt_spacing_in: float | None = None
#     sill_plate_transfer: str = ""
#     top_plate_transfer: str = ""
#     tabulated_unit_shear_plf: float | None = None        # SKIP_EXTRACTION: calc engine looks up from SDPWS Table 4.3A/B
#     sw_note: str = ""
#     uncertain_areas: List[str] | None = None


# class NailingZone(BaseModel):
#     zone_label: str
#     nail_spacing_in: float | None = None
#     area_description: str = ""
#     uncertain_areas: List[str] | None = None


# class Diaphragm(BaseModel):
#     level: str
#     diaphragm_type: str = ""
#     sheathing_type: str = ""
#     sheathing_thickness_in: float | None = None
#     nailing_zones: List[NailingZone] | None = None
#     chord_member: str | None = None
#     collector_lines: List[str] | None = None
#     diaphragm_note: str = ""
#     uncertain_areas: List[str] | None = None


# class ShearWallData(BaseModel):
#     braced_wall_lines: List[BracedWallLine]
#     shear_walls: List[ShearWall]
#     diaphragms: List[Diaphragm]


# # ----------------------------- Wall System ----------------------------

# class StudWall(BaseModel):
#     zone: str
#     wall_type: Literal["exterior_bearing", "interior_bearing", "non_bearing_partition", "shear_wall"]
#     stud_size: str                                # EXTRACT: "2x4", "2x6"
#     number_of_plies: int = 1
#     lumber_spec: LumberSpec | None = None
#     stud_height_ft: float | None = None
#     stud_height_note: str = ""
#     spacing_in: float = 16.0                      # EXTRACT_IF_VISIBLE | default: 16" o.c.
#     wall_length_ft: float | None = None
#     wall_length_note: str = ""
#     top_plate: Literal["single", "double"] = "double"  # EXTRACT_IF_VISIBLE | default: double
#     top_plate_size: str | None = None
#     bottom_plate_size: str | None = None
#     bottom_plate_treated: bool | None = None             # EXTRACT_IF_VISIBLE: True if on concrete
#     sheathing_type: str | None = None
#     sheathing_thickness_in: float | None = None
#     braced_wall_panel: bool | None = None
#     bracing_method: str | None = None                    # EXTRACT_IF_VISIBLE: "WSP", "LIB", "PBS", etc.
#     holdown_connector: str | None = None
#     support_material: SupportMaterial | None = None
#     axial_load_lbs: float | None = None                  # SKIP_EXTRACTION: calc engine computes from load takedown
#     lateral_wind_load_psf: float | None = None           # SKIP_EXTRACTION: calc engine fills from wind C&C pressures
#     tributary_width_ft: float | None = None
#     wall_dead_load_psf: float | None = None
#     repetitive_member: bool = True                # SKIP_EXTRACTION | default: True
#     stud_note: str = ""
#     uncertain_areas: List[str] | None = None


# class Header(BaseModel):
#     zone: str
#     opening_type: Literal["window", "door", "sliding_door", "garage_door", "pass_through", "other"]
#     opening_mark: str | None = None
#     number_of_stories_above: int | None = None
#     rough_opening_width_in: float | None = None
#     rough_opening_height_in: float | None = None
#     rough_opening_note: str = ""
#     header_size: str | None = None                       # EXTRACT: "2x12", "4x12", etc.
#     number_of_plies: int | None = None
#     lumber_spec: LumberSpec | None = None
#     header_clear_span_ft: float | None = None
#     header_clear_span_note: str = ""
#     bearing_wall: bool = True
#     available_bearing_in: float | None = None
#     available_bearing_note: str = ""
#     jack_studs_per_side: int | None = None
#     king_studs_per_side: int | None = None
#     support_material: SupportMaterial | None = None
#     tributary_width_ft: float | None = None
#     tributary_width_note: str = ""
#     point_load_lbs: float | None = None
#     point_load_source: str | None = None
#     cripple_studs_above: bool | None = None
#     cripple_studs_below: bool | None = None
#     floor_live_load_psf: float | None = None
#     roof_load_on_header_psf: float | None = None
#     lateral_wind_load_psf: float | None = None           # SKIP_EXTRACTION: calc engine fills from wind analysis
#     header_note: str = ""
#     uncertain_areas: List[str] | None = None


# class TopPlate(BaseModel):
#     zone: str
#     configuration: Literal["single", "double"] = "double"
#     size: str = ""
#     support_material: SupportMaterial | None = None
#     splice_connector: str | None = None
#     plate_note: str = ""
#     uncertain_areas: List[str] | None = None


# class BottomPlate(BaseModel):
#     zone: str
#     size: str = ""
#     pressure_treated: bool | None = None
#     support_material: SupportMaterial | None = None
#     anchor_bolt_spacing_in: float = 72.0          # EXTRACT_IF_VISIBLE | default: 72" (IRC R403.1.6)
#     plate_note: str = ""
#     uncertain_areas: List[str] | None = None


# class WallSystemData(BaseModel):
#     stud_walls: List[StudWall]
#     headers: List[Header]
#     top_plates: List[TopPlate]
#     bottom_plates: List[BottomPlate]


# # =====================================================================
# #  SECTION 4: TOP-LEVEL STATE
# # =====================================================================

# class AgentState(BaseModel):
#     file_uri: Optional[str] = None
#     file_name: Optional[str] = None

#     # Use lightweight LLM
#     project_data: Optional[ProjectData] = None
#     building_geometry: Optional[BuildingGeometry] = None
#     soil_data: Optional[SoilData] = None

#     # --- Hazard lookup (deterministic, from USGS + ASCE APIs — no AI) ---
#     hazard_lookup_input: Optional[HazardLookupInput] = None
#     hazard_lookup_result: Optional[HazardLookupResult] = None

#     # --- Plan-extracted environmental context (AI reads what's on plans, rest is defaults) ---
#     wind_context: Optional[PlanExtractedWindContext] = None
#     snow_context: Optional[PlanExtractedSnowContext] = None
#     seismic_context: Optional[PlanExtractedSeismicContext] = None

#     # --- Structural systems (AI-extracted from plans) ---
#     roof_system: Optional[RoofSystemData] = None
#     floor_system: Optional[FloorSystemData] = None
#     footing: Optional[FootingSystemData] = None
#     post: Optional[PostData] = None
#     shear_wall: Optional[ShearWallData] = None
#     wall_system: Optional[WallSystemData] = None


# # --------------------------------------------------------------------------------------------------------------------------
# # --------------------------------------------------------------------------------------------------------------------------
# # --------------------------------------------------------------------------------------------------------------------------


# from pydantic import BaseModel
# from typing import List, Optional, Literal

# from core.utils import CanonicalMemberSize, EWPProductType


# # =====================================================================
# #  ENUMS & SHARED TYPES
# # =====================================================================

# SupportMaterial = Literal[
#     "DF-L", "SP", "SPF", "HF",
#     "concrete", "steel",
#     "LVL", "PSL", "LSL", "glulam"
# ]

# class LumberSpec(BaseModel):
#     species: Literal["DF-L", "SP", "SPF", "HF", "LVL", "PSL", "LSL", "glulam"] | None = None
#     grade: Literal["SS", "No.1", "No.2", "No.3", "Stud", "1.55E", "2.0E", "2.2E", "24F-V4"] | None = None
#     species_grade_note: str

# class EWPSpec(BaseModel):
#     product_type: EWPProductType | None = None
#     Fb_psi: float | None = None
#     Fv_psi: float | None = None
#     Fc_perp_psi: float | None = None
#     E_psi: float | None = None
#     E_min_psi: float | None = None

# class MemberSize(BaseModel):
#     """Parsed member size. AI extracts size_label string, calc engine parses to dimensions."""
#     size_label: CanonicalMemberSize | None = None  # EXTRACT: canonical "2x10" or "1.75x11.875"
#     nominal_width_in: int | None = None  # SKIP_EXTRACTION: parsed from size_label by calc engine
#     nominal_depth_in: int | None = None  # SKIP_EXTRACTION: parsed from size_label by calc engine


# # =====================================================================
# # STRUCTURAL MEMBERS  (AI-extracted from plans)
# # =====================================================================
# #
# # For each member model:
# #   - Fields the AI SHOULD extract are unmarked
# #   - Fields the AI should SKIP are marked
# #   - service_conditions / deflection_criteria → always SKIP, use defaults
# #   - ewp_spec → only populate if plans explicitly call out an EWP
# # =====================================================================

# # ----------------------------- Roof System ----------------------------

# class RoofRafter(BaseModel):
#     zone: str
#     size: CanonicalMemberSize
#     number_of_plies: int = 1
#     lumber_spec: LumberSpec | None = None
#     ewp_spec: EWPSpec | None = None
#     clear_span_ft: float | None = None
#     clear_span_note: str = ""
#     overhang_in: float | None = None
#     overhang_note: str = ""
#     roof_pitch: str | None = None                        # EXTRACT: "6/12", "4/12" etc.
#     available_support_bearing_in: float | None = None
#     available_support_bearing_note: str = ""
#     support_material: SupportMaterial | None = None
#     spacing_in: float | None = None
#     # roof_dead_load_psf: float | None = None
#     # roof_live_load_psf (20 psf default), roof_snow_load_psf (hazard lookup) and
#     # repetitive_member (always True) are injected by the calc engine — not extracted.
#     uncertain_areas: List[str] | None = None


# class CeilingJoist(BaseModel):
#     zone: str
#     size: CanonicalMemberSize
#     number_of_plies: int = 1
#     lumber_spec: LumberSpec | None = None
#     ewp_spec: EWPSpec | None = None
#     clear_span_ft: float | None = None
#     clear_span_note: str = ""
#     available_support_bearing_in: float | None = None
#     available_support_bearing_note: str = ""
#     support_material: SupportMaterial | None = None
#     spacing_in: float | None = None
#     ceiling_dead_load_psf: float | None = None
#     attic_live_load_psf: float | None = None             # EXTRACT_IF_VISIBLE: 10 psf no storage, 20 psf limited storage
#     attic_use: Literal["no_access", "limited_storage", "habitable"] = "limited_storage"
#     # repetitive_member (always True) is injected by the calc engine — not extracted.
#     uncertain_areas: List[str] | None = None


# class RidgeBeam(BaseModel):
#     zone: str
#     size: CanonicalMemberSize
#     number_of_plies: int
#     # lumber_spec: LumberSpec | None = None
#     ewp_spec: EWPSpec | None = None
#     clear_span_ft: float | None = None
#     clear_span_note: str = ""
#     available_support_bearing_in: float | None = None
#     available_support_bearing_note: str = ""
#     support_material: SupportMaterial | None = None
#     tributary_width_ft: float | None = None
#     tributary_width_note: str = ""
#     # roof_dead_load_psf: float | None = None
#     # roof_live_load_psf (20 psf default) and roof_snow_load_psf (hazard lookup)
#     # are injected by the calc engine — not extracted.
#     uncertain_areas: List[str] | None = None


# class HipValleyRafter(BaseModel):
#     zone: str
#     member_type: Literal["hip", "valley"]
#     size: CanonicalMemberSize
#     number_of_plies: int
#     lumber_spec: LumberSpec | None = None
#     ewp_spec: EWPSpec | None = None
#     clear_span_ft: float | None = None
#     clear_span_note: str = ""
#     roof_pitch: str | None = None
#     available_support_bearing_in: float | None = None
#     available_support_bearing_note: str = ""
#     support_material: SupportMaterial | None = None
#     tributary_width_ft: float | None = None
#     tributary_width_note: str = ""
#     # roof_dead_load_psf: float | None = None
#     # roof_live_load_psf (20 psf default) and roof_snow_load_psf (hazard lookup)
#     # are injected by the calc engine — not extracted.
#     uncertain_areas: List[str] | None = None


# class RoofDropBeam(BaseModel):
#     zone: str
#     size: CanonicalMemberSize
#     number_of_plies: int
#     lumber_spec: LumberSpec | None = None
#     ewp_spec: EWPSpec | None = None
#     clear_span_ft: float | None = None
#     clear_span_note: str = ""
#     roof_pitch: str | None = None
#     available_support_bearing_in: float | None = None
#     available_support_bearing_note: str = ""
#     support_material: SupportMaterial | None = None
#     tributary_width_ft: float | None = None
#     tributary_width_note: str = ""
#     # roof_dead_load_psf: float | None = None
#     # roof_live_load_psf (20 psf default) and roof_snow_load_psf (hazard lookup)
#     # are injected by the calc engine — not extracted.
#     uncertain_areas: List[str] | None = None


# class RoofFlushBeam(BaseModel):
#     zone: str
#     size: CanonicalMemberSize
#     number_of_plies: int
#     lumber_spec: LumberSpec | None = None
#     ewp_spec: EWPSpec | None = None
#     clear_span_ft: float | None = None
#     clear_span_note: str = ""
#     available_support_bearing_in: float | None = None
#     available_support_bearing_note: str = ""
#     support_material: SupportMaterial | None = None
#     hanger_bucket_seat_depth_in: float | None = None
#     hanger_seat_depth_note: str = ""
#     hanger_carrier_material: str | None = None
#     tributary_width_ft: float | None = None
#     tributary_width_note: str = ""
#     # roof_dead_load_psf: float | None = None
#     # roof_live_load_psf (20 psf default) and roof_snow_load_psf (hazard lookup)
#     # are injected by the calc engine — not extracted.
#     uncertain_areas: List[str] | None = None


# class RoofSystemData(BaseModel):
#     roof_rafters: List[RoofRafter]
#     ceiling_joists: List[CeilingJoist]
#     ridge_beams: List[RidgeBeam]
#     hip_valley_rafters: List[HipValleyRafter]
#     roof_drop_beams: List[RoofDropBeam]
#     roof_flush_beams: List[RoofFlushBeam]


# # ----------------------------- Floor System ----------------------------

# class FloorJoist(BaseModel):
#     zone: str
#     size: CanonicalMemberSize
#     number_of_plies: int = 1
#     lumber_spec: LumberSpec | None = None
#     ewp_spec: EWPSpec | None = None
#     clear_span_ft: float | None = None
#     clear_span_note: str | None = ""
#     cantilever_ft: float | None = None
#     cantilever_note: str | None = None
#     available_support_bearing_in: float | None = None
#     available_support_bearing_note: str | None = ""
#     support_material: SupportMaterial | None = None
#     spacing_in: float | None = None
#     # dead_load_psf: float | None = None
#     # dead_load_note: str | None = None
#     # floor_live_load_psf (40 psf residential default) and repetitive_member (always True)
#     # are injected by the calc engine — not extracted.
#     uncertain_areas: List[str] | None = None


# class FloorDropBeam(BaseModel):
#     zone: str
#     size: CanonicalMemberSize
#     number_of_plies: int
#     lumber_spec: LumberSpec | None = None
#     ewp_spec: EWPSpec | None = None
#     clear_span_ft: float | None = None
#     clear_span_note: str | None = ""
#     available_support_bearing_in: float | None = None
#     available_support_bearing_note: str | None = ""
#     support_material: SupportMaterial | None = None
#     tributary_width_ft: float | None = None
#     tributary_width_note: str | None = ""
#     # dead_load_psf: float | None = None
#     # dead_load_note: str | None = None
#     # floor_live_load_psf (40 psf default) is injected by the calc engine — not extracted.
#     uncertain_areas: List[str] | None = None


# class FloorFlushBeam(BaseModel):
#     zone: str
#     size: CanonicalMemberSize
#     number_of_plies: int
#     lumber_spec: LumberSpec | None = None
#     ewp_spec: EWPSpec | None = None
#     clear_span_ft: float | None = None
#     clear_span_note: str | None = ""
#     available_support_bearing_in: float | None = None
#     available_support_bearing_note: str | None = ""
#     support_material: SupportMaterial | None = None
#     # hanger_bucket_seat_depth_in: float | None = None
#     # hanger_seat_depth_note: str | None = ""
#     # hanger_carrier_material: str | None = None
#     tributary_width_ft: float | None = None
#     tributary_width_note: str | None = ""
#     # dead_load_psf: float | None = None
#     # dead_load_note: str | None = None
#     # floor_live_load_psf (40 psf default) is injected by the calc engine — not extracted.
#     uncertain_areas: List[str] | None = None


# class FloorSystemData(BaseModel):
#     floor_joists: List[FloorJoist]
#     floor_drop_beams: List[FloorDropBeam]
#     floor_flush_beams: List[FloorFlushBeam]


# # ----------------------------- Footing System -------------------------

# class FootingProjectInfo(BaseModel):
#     concrete_fc_footings_psi: int = 2500          # EXTRACT_IF_VISIBLE | default: 2500 psi (common residential)
#     concrete_fc_slab_psi: int = 2500              # EXTRACT_IF_VISIBLE | default: 2500 psi
#     soil_bearing_pressure_psf: float | None = None  # EXTRACT: from geotech or general notes
#     soil_bearing_note: str = ""
#     frost_line_depth_in: float | None = None       # EXTRACT_IF_VISIBLE: from general notes
#     frost_line_note: str = ""
#     soils_engineer_required: bool | None = None
#     # rebar_grade (Grade 60) and concrete_cover_to_soil_in (3", ACI 318 §20.6.1)
#     # are injected by the calc engine — not extracted.
#     uncertain_areas: List[str] | None = None


# class ContinuousStripFooting(BaseModel):
#     footing_mark: str
#     existing_condition: bool = False
#     new_load_applied: str = ""
#     location_description: str
#     # supported_element: str
#     # wall_type: Literal["exterior_bearing", "interior_bearing", "shear_wall", "non_bearing", "grade_beam"]
#     width_in: float | None = None
#     width_note: str = ""
#     depth_in: float | None = None
#     depth_note: str = ""
#     # length_ft: float | None = None
#     # length_note: str = ""
#     # top_of_footing_below_grade_in: float | None = None
#     # bottom_of_footing_below_grade_in: float | None = None
#     # embedment_note: str = ""
#     # frost_compliance: str = ""
#     # longitudinal_rebar_size: int | None = None           # EXTRACT: bar number (#4 = 4)
#     # longitudinal_rebar_count: int | None = None
#     transverse_rebar_size: int | None = None
#     transverse_rebar_spacing_in: float | None = None
#     # anchor_bolt_dia_in: float = 0.5               # EXTRACT_IF_VISIBLE | default: 1/2" (IRC R403.1.6)
#     # anchor_bolt_embedment_in: float = 7.0         # EXTRACT_IF_VISIBLE | default: 7" (IRC R403.1.6)
#     # anchor_bolt_spacing_in: float = 72.0          # EXTRACT_IF_VISIBLE | default: 72" o.c. (IRC R403.1.6)
#     # plate_washer_required: bool | None = None
#     # plate_washer_spec: str = ""
#     sill_plate_size: CanonicalMemberSize | None = None
#     # rebar_orientation, rebar_faces, rebar_lap_length_in, concrete_cover_in (3"),
#     # anchor_bolt_corner_spacing_in (12"), anchor_bolt_end_of_plate_max_in (12")
#     # and sill_plate_treated (True) are injected by the calc engine — not extracted.
#     footing_note: str = ""
#     uncertain_areas: List[str] | None = None


# class PadFooting(BaseModel):
#     footing_mark: str
#     existing_condition: bool = False
#     new_load_applied: str = ""
#     location_description: str
#     supported_element: str
#     width_in: float | None = None
#     length_in: float | None = None
#     depth_in: float | None = None
#     dimension_note: str = ""
#     # top_of_footing_below_grade_in: float | None = None
#     # bottom_of_footing_below_grade_in: float | None = None
#     embedment_note: str = ""
#     # frost_compliance: str = ""
#     rebar_size: int | None = None
#     rebar_spacing_in: float | None = None
#     # rebar_orientation, rebar_faces, rebar_lap_length_in and concrete_cover_in (3")
#     # are injected by the calc engine — not extracted.
#     # post_base_model: str = ""
#     # post_base_anchor_type: Literal["cast_in", "epoxy_set"] | None = None
#     post_size: CanonicalMemberSize | None = None
#     footing_note: str = ""
#     uncertain_areas: List[str] | None = None


# class GradeBeam(BaseModel):
#     footing_mark: str
#     existing_condition: bool = False
#     new_load_applied: str = ""
#     location_description: str
#     supported_element: str
#     bearing_on: Literal["soil", "piers", "piles"] | None = None
#     width_in: float | None = None
#     depth_in: float | None = None
#     length_ft: float | None = None
#     dimension_note: str = ""
#     top_of_beam_below_grade_in: float | None = None
#     bottom_of_beam_below_grade_in: float | None = None
#     frost_compliance: str = ""
#     rebar_size: int | None = None
#     rebar_count: int | None = None
#     # rebar_orientation, rebar_faces, rebar_lap_length_in and concrete_cover_in (3")
#     # are injected by the calc engine — not extracted.
#     footing_note: str = ""
#     uncertain_areas: List[str] | None = None


# class SlabOnGrade(BaseModel):
#     zone: str
#     existing_condition: bool = False
#     thickness_in: float = 4.0                     # EXTRACT_IF_VISIBLE | default: 4"
#     thickness_note: str = ""
#     reinforcement_type: Literal["rebar", "wwf", "fiber", "none"] | None = None
#     rebar_size: int | None = None
#     rebar_spacing_in: float | None = None
#     wwf_designation: str | None = None
#     sub_base_description: str = ""
#     sub_base_depth_in: float | None = None
#     # rebar_orientation, vapor_barrier_mils (10 mil) and control_joint_spacing_ft
#     # (2-3× slab thickness rule) are injected by the calc engine — not extracted.
#     monolithic_with_footing: bool | None = None
#     slab_note: str = ""
#     uncertain_areas: List[str] | None = None


# class HoldownAnchor(BaseModel):
#     holdown_model: str
#     anchor_rod: str
#     embedment_in: float | None = None
#     location: str
#     supported_wall_mark: str
#     installation_timing: str = ""
#     holdown_note: str = ""
#     uncertain_areas: List[str] | None = None


# class FootingSystemData(BaseModel):
#     # project_info: FootingProjectInfo
#     continuous_strip_footings: List[ContinuousStripFooting]
#     pad_footings: List[PadFooting]
#     # grade_beams: List[GradeBeam]
#     # slab_on_grade: List[SlabOnGrade]
#     # holdown_anchors: List[HoldownAnchor]


# # ----------------------------- Post System ----------------------------

# class StandalonePost(BaseModel):
#     post_mark: str
#     existing_condition: bool = False
#     # new_load_applied: str = ""
#     location_description: str
#     post_type: Literal["solid", "built_up"] | None = None
#     functional_type: Literal[
#         "bearing", "holdown", "corner",
#     ] | None = None
#     post_size: CanonicalMemberSize
#     number_of_plies: int | None = None
#     species: Literal["DF-L", "SP", "SPF", "HF", "LVL", "PSL", "LSL", "glulam"] | None = None
#     grade: Literal["SS", "No.1", "No.2", "No.3", "Stud", "1.55E", "2.0E", "2.2E", "24F-V4"] | None = None
#     ewp_spec: EWPSpec | None = None
#     species_grade_note: str = ""
#     height_ft: float | None = None
#     height_note: str = ""
#     unbraced_length_ft: float | None = None
#     unbraced_length_note: str = ""
#     # effective_length_factor_Ke (1.0, pin-pin) is injected by the calc engine — not extracted.
#     bracing_condition: str = ""
#     tributary_area_sf: float | None = None
#     tributary_area_note: str = ""
#     point_load_lbs: float | None = None
#     point_load_note: str = ""
#     # roof_dead_load_psf: float | None = None
#     # roof_live_load_psf: float | None = None
#     # roof_snow_load_psf (hazard lookup) is injected by the calc engine — not extracted.
#     # floor_dead_load_psf: float | None = None
#     # floor_live_load_psf: float | None = None
#     base_connector_model: str = ""
#     base_bearing_surface: Literal[
#         "concrete_footing", "concrete_slab",
#         "wood_beam", "steel_beam", "grade"
#     ] | None = None
#     base_anchor_type: Literal["cast_in", "epoxy_set", "bolt_through"] | None = None
#     # base_anchor_fastener: str = ""
#     # top_connector_model: str = ""
#     # top_bearing_condition: str = ""
#     # holdown_model: str = ""
#     # holdown_anchor_rod: str = ""
#     # holdown_note: str = ""
#     post_note: str = ""
#     uncertain_areas: List[str] | None = None


# class PostData(BaseModel):
#     standalone_posts: List[StandalonePost]


# # ----------------------------- Shear Wall System ----------------------

# class WallOpening(BaseModel):
#     width_ft: float | None = None
#     height_ft: float | None = None
#     sill_height_ft: float | None = None        # sheathed depth below opening


# class ShearWall(BaseModel):
#     sw_mark: str
#     swl_id: str
#     story_level: str
#     system_type: str
#     # Design method: "segmented" (default — each full-height pier checked
#     # individually), "perforated" (SDPWS 4.3.5.3 Co method), or "ftao"
#     # (force transfer around openings — straps/blocking detailed at openings).
#     shear_wall_method: Literal["segmented", "perforated", "ftao"] | None = None
#     # For perforated / FTAO walls: full-height pier lengths left-to-right and
#     # the openings between them (n openings -> n+1 piers).
#     pier_lengths_ft: List[float] | None = None
#     openings: List[WallOpening] | None = None
#     framing_species: str | None = None
#     pier_length_ft: float | None = None
#     wall_height_ft: float | None = None
#     # aspect_ratio (height/length) is computed by the calc engine — not extracted.
#     sheathing_type: str
#     sheathing_thickness_in: float | None = None
#     sheathing_faces: Literal["one_side", "two_sides"] | None = None
#     # blocking: str = ""
#     edge_nail_spacing_in: float | None = None
#     # field_nail_spacing_in: float | None = None
#     boundary_nail_spacing_in: float | None = None
#     nail_size: Literal["10d", "8d"] = None
#     stud_size: CanonicalMemberSize | None = None
#     stud_spacing_in: float | None = None
#     holdown_model: str = ""
#     # holdown_anchor_rod: str = ""
#     # support_connector: str = ""
#     # anchor_bolt_dia_in: float | None = None
#     # anchor_bolt_spacing_in: float | None = None
#     # sill_plate_transfer: str = ""
#     # top_plate_transfer: str = ""
#     # requires_3x_framing, holdown_force_lbs and tabulated_unit_shear_plf (SDPWS
#     # Table 4.3A/B) are computed by the calc engine — not extracted.
#     # sw_note: str = ""
#     uncertain_areas: List[str] | None = None


# class NailingZone(BaseModel):
#     zone_label: str
#     nail_spacing_in: float | None = None
#     area_description: str = ""
#     uncertain_areas: List[str] | None = None


# class Diaphragm(BaseModel):
#     level: str
#     diaphragm_type: str = ""
#     sheathing_type: str = ""
#     sheathing_thickness_in: float | None = None
#     nailing_zones: List[NailingZone] | None = None
#     chord_member: str | None = None
#     collector_lines: List[str] | None = None
#     diaphragm_note: str = ""
#     uncertain_areas: List[str] | None = None


# class ShearWallData(BaseModel):
#     shear_walls: List[ShearWall]
#     # diaphragms: List[Diaphragm]


# # ----------------------------- Masonry Wall System --------------------

# class MasonryWall(BaseModel):
#     """A reinforced masonry (CMU/clay) wall panel for TMS 402 design.

#     Out-of-plane lateral demand (wind psf, SDS) comes from the hazard
#     lookup / wind context via the calc engine — not extracted per wall.
#     """
#     wall_mark: str
#     story_level: str = ""
#     masonry_type: Literal["cmu", "clay", "other"] = "cmu"
#     nominal_thickness_in: float | None = None        # 7.625 for 8" CMU
#     wall_height_ft: float | None = None
#     panel_length_ft: float | None = None
#     # f'm net compressive strength (psi); or unit strength to derive it
#     fm_psi: float | None = None
#     unit_compressive_strength_psi: float | None = None
#     grouting: Literal["fully_grouted", "partially_grouted", "ungrouted"] | None = None
#     grout_spacing_in: float | None = None            # for partial grout
#     mortar_type: Literal["M", "S", "N", "O"] | None = None
#     # Vertical reinforcement
#     vertical_bar_size: str = ""                      # "#5", "#6", ...
#     vertical_bar_spacing_in: float | None = None
#     rebar_grade_ksi: float | None = None             # 60 typ.
#     # Horizontal (joint/bond-beam) reinforcement — needed for in-plane shear
#     horizontal_bar_size: str = ""                    # "#3", "#4", ...
#     horizontal_bar_spacing_in: float | None = None
#     # Support / loading context
#     support_condition: str = ""                      # "pinned top and bottom" etc.
#     axial_dead_plf: float | None = None              # roof/floor DL at top
#     axial_live_plf: float | None = None
#     parapet_height_ft: float | None = None
#     is_loadbearing: bool | None = None
#     seismic_participation: Literal["participating", "nonparticipating"] | None = None
#     # For participating masonry shear walls: line grouping + direction so the
#     # calc engine can distribute in-plane story shear (same scheme as wood SW).
#     swl_id: str = ""
#     direction: Literal["X", "Y"] | None = None
#     masonry_note: str = ""
#     uncertain_areas: List[str] | None = None

# # ----------------------------- Wall System ----------------------------

# class StudWall(BaseModel):
#     zone: str
#     # wall_type: Literal["exterior_bearing", "interior_bearing", "non_bearing_partition", "shear_wall"]
#     stud_size: CanonicalMemberSize
#     # number_of_plies: int = 1
#     lumber_spec: LumberSpec | None = None
#     ewp_spec: EWPSpec | None = None
#     stud_height_ft: float | None = None
#     stud_height_note: str = ""
#     spacing_in: float = 16.0                      # EXTRACT_IF_VISIBLE | default: 16" o.c.
#     wall_length_ft: float | None = None
#     wall_length_note: str = ""
#     # top_plate: Literal["single", "double"] = "double"  # EXTRACT_IF_VISIBLE | default: double
#     # top_plate_size: CanonicalMemberSize | None = None
#     # bottom_plate_size: CanonicalMemberSize | None = None
#     # bottom_plate_treated: bool | None = None             # EXTRACT_IF_VISIBLE: True if on concrete
#     # sheathing_type: str | None = None
#     # sheathing_thickness_in: float | None = None
#     # braced_wall_panel: bool | None = None
#     # bracing_method: str | None = None                    # EXTRACT_IF_VISIBLE: "WSP", "LIB", "PBS", etc.
#     # holdown_connector: str | None = None
#     support_material: SupportMaterial | None = None
#     tributary_width_ft: float | None = None
#     wall_dead_load_psf: float | None = None
#     # axial_load_lbs (load takedown), lateral_wind_load_psf (wind C&C pressures)
#     # and repetitive_member (always True) are injected by the calc engine — not extracted.
#     stud_note: str = ""
#     uncertain_areas: List[str] | None = None


# class Header(BaseModel):
#     zone: str
#     # opening_type: Literal["window", "door", "sliding_door", "garage_door", "pass_through", "other"]
#     opening_mark: str | None = None
#     # number_of_stories_above: int | None = None
#     # rough_opening_width_in: float | None = None
#     # rough_opening_height_in: float | None = None
#     # rough_opening_note: str = ""
#     header_size: CanonicalMemberSize | None = None
#     number_of_plies: int | None = None
#     lumber_spec: LumberSpec | None = None
#     ewp_spec: EWPSpec | None = None
#     header_clear_span_ft: float | None = None
#     header_clear_span_note: str = ""
#     # bearing_wall: bool = True
#     available_bearing_in: float | None = None
#     available_bearing_note: str = ""
#     # jack_studs_per_side: int | None = None
#     # king_studs_per_side: int | None = None
#     support_material: SupportMaterial | None = None
#     tributary_width_ft: float | None = None
#     tributary_width_note: str = ""
#     point_load_lbs: float | None = None
#     # point_load_source: str | None = None
#     # cripple_studs_above: bool | None = None
#     # cripple_studs_below: bool | None = None
#     # floor_live_load_psf: float | None = None
#     roof_load_on_header_psf: float | None = None
#     # lateral_wind_load_psf (wind analysis) is injected by the calc engine — not extracted.
#     header_note: str = ""
#     uncertain_areas: List[str] | None = None


# class TopPlate(BaseModel):
#     zone: str
#     configuration: Literal["single", "double"] = "double"
#     size: CanonicalMemberSize | None = None
#     support_material: SupportMaterial | None = None
#     splice_connector: str | None = None
#     plate_note: str = ""
#     uncertain_areas: List[str] | None = None


# class BottomPlate(BaseModel):
#     zone: str
#     size: CanonicalMemberSize | None = None
#     pressure_treated: bool | None = None
#     support_material: SupportMaterial | None = None
#     anchor_bolt_spacing_in: float = 72.0          
#     plate_note: str = ""
#     uncertain_areas: List[str] | None = None


# class WallSystemData(BaseModel):
#     stud_walls: List[StudWall]
#     headers: List[Header]
#     # top_plates: List[TopPlate]
#     # bottom_plates: List[BottomPlate]
#     masonry_walls: List[MasonryWall]


# # ----------------------------- Visualization --------------------------

# class VisualPoint(BaseModel):
#     x_ft: float | None = None
#     y_ft: float | None = None
#     label: str | None = None


# class VisualDimension(BaseModel):
#     label: str
#     value_ft: float | None = None
#     direction: Literal["X", "Y", "vertical", "diagonal", "unknown"] = "unknown"
#     source_note: str = ""
#     uncertain: bool = False


# class VisualFootprint(BaseModel):
#     level: str = "main"
#     overall_length_ft: float | None = None
#     overall_width_ft: float | None = None
#     polygon: List[VisualPoint] | None = None
#     source_note: str = ""
#     uncertain_areas: List[str] | None = None


# class VisualLevel(BaseModel):
#     name: str
#     elevation_ft: float | None = None
#     story_height_ft: float | None = None
#     source_note: str = ""


# class VisualElevationView(BaseModel):
#     view_name: str
#     eave_height_ft: float | None = None
#     ridge_height_ft: float | None = None
#     wall_height_ft: float | None = None
#     roof_pitch: str | None = None
#     source_note: str = ""
#     uncertain_areas: List[str] | None = None


# class VisualRidgeLine(BaseModel):
#     label: str
#     level: str | None = None
#     orientation: Literal["N-S", "E-W", "diagonal", "unknown"] = "unknown"
#     start: VisualPoint | None = None
#     end: VisualPoint | None = None
#     height_ft: float | None = None
#     source_note: str = ""
#     uncertain: bool = False


# class VisualRoofPlane(BaseModel):
#     label: str
#     roof_type: Literal["gable", "hip", "flat", "mono_slope", "gambrel", "mansard", "unknown"] = "unknown"
#     pitch: str | None = None
#     boundary: List[VisualPoint] | None = None
#     ridge_label: str | None = None
#     source_note: str = ""
#     uncertain_areas: List[str] | None = None


# class VisualDrawingContext(BaseModel):
#     """
#     Small drawing-only extraction for the visualizer.
#     Structural members are reused from the already extracted schemas.
#     """
#     drawing_scale: str | None = None
#     north_arrow: str | None = None
#     plan_orientation_note: str | None = None
#     dimensions: List[VisualDimension]
#     levels: List[VisualLevel]
#     footprints: List[VisualFootprint]
#     elevations: List[VisualElevationView]
#     ridge_lines: List[VisualRidgeLine]
#     roof_planes: List[VisualRoofPlane]
#     visual_summary: str = ""
#     uncertain_areas: List[str] | None = None


# class VisualOverlayItem(BaseModel):
#     item_type: str
#     system: str
#     label: str
#     level: str | None = None
#     size: CanonicalMemberSize | None = None
#     location_description: str | None = None
#     span_ft: float | None = None
#     spacing_in: float | None = None
#     note: str = ""


# class VisualizationData(BaseModel):
#     context: VisualDrawingContext | None = None
#     overlays: List[VisualOverlayItem]
#     source_systems: List[str]
#     visualizer_note: str = ""


# # =====================================================================
# #  SECTION 4: TOP-LEVEL STATE
# # =====================================================================

# class AgentState(BaseModel):
#     # --- Structural systems (AI-extracted from plans) ---
#     roof_system: Optional[RoofSystemData] = None
#     floor_system: Optional[FloorSystemData] = None
#     footing: Optional[FootingSystemData] = None
#     post: Optional[PostData] = None
#     shear_wall: Optional[ShearWallData] = None
#     wall_system: Optional[WallSystemData] = None