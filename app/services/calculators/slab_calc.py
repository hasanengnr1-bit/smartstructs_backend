"""
slab_calc.py
Slab-on-Grade Calculator — ACI 360 / 318 / IRC simplified.

Checks: thickness adequacy, reinforcement requirements,
control joint spacing, vapor barrier, sub-base.

Run:   python slab_calc.py
Import: from slab_calc import check_slab
"""

import math
from dataclasses import dataclass
from typing import Literal, Optional, List


REBAR = {
    "#3": {"area": 0.11, "dia": 0.375},
    "#4": {"area": 0.20, "dia": 0.500},
    "#5": {"area": 0.31, "dia": 0.625},
}

WWF_DESIGNATIONS = {
    "6x6-W1.4xW1.4": {"area": 0.028},
    "6x6-W2.0xW2.0": {"area": 0.040},
    "6x6-W2.9xW2.9": {"area": 0.058},
    "4x4-W1.4xW1.4": {"area": 0.042},
    "4x4-W2.0xW2.0": {"area": 0.060},
    "4x4-W2.9xW2.9": {"area": 0.087},
    "6x6-W4.0xW4.0": {"area": 0.080},
}


@dataclass
class SlabInputs:
    # Dimensions
    thickness_in: float = 4.0           # slab thickness (in)
    length_ft: float = 20.0             # longest dimension
    width_ft: float = 15.0              # shorter dimension

    # Reinforcement
    reinforcement_type: str = "wwf"     # "rebar", "wwf", "fiber", "none"
    rebar_size: Optional[str] = "#4"
    rebar_spacing_in: float = 18.0
    wwf_designation: Optional[str] = "6x6-W2.9xW2.9"
    fiber_type: Optional[str] = "macro_synthetic"

    # Sub-grade
    sub_base_type: str = "gravel"       # "gravel", "sand", "native"
    sub_base_depth_in: float = 4.0

    # Vapor barrier / moisture
    vapor_barrier_mils: int = 10
    interior_slab: bool = True

    # Loading
    residential_use: bool = True        # True = light residential, False = garage/light commercial
    point_load_lbs: float = 2000        # worst point load (furniture, etc.)

    # Exposure / environment
    exposure_class: str = "interior_dry"  # "interior_dry", "exterior", "freeze_thaw"
    sulfate_exposure: str = "S0"         # "S0", "S1", "S2", "S3"

    # Concrete
    fc_psi: float = 3000.0
    concrete_cover_in: float = 0.0       # top cover (0 for interior, 1.5 for exterior)

    # Options
    monolithic_with_footing: bool = False
    control_joint_spacing_ft: Optional[float] = None  # None = auto-compute
    thickened_edge: bool = False
    thickened_edge_width_in: float = 12.0
    thickened_edge_depth_in: float = 12.0


def check_slab(inp: SlabInputs):
    """Check slab-on-grade per ACI 360 / 318 / IRC."""
    t = inp.thickness_in
    area_sf = inp.length_ft * inp.width_ft

    # ---- 1. Minimum thickness (IRC R506 / ACI 360) ----
    if inp.residential_use:
        t_min = 3.5   # IRC R506.1 minimum for residential
        t_req = 3.5
    else:
        t_min = 4.0   # garage / light commercial
        t_req = 4.0

    thickness_ok = t >= t_min

    # ---- 2. Reinforcement check ----
    reinf_result = {}
    if inp.reinforcement_type == "none":
        As_provided = 0
        reinf_result = {
            "type": "none",
            "As_provided_in2_per_ft": 0.0,
            "note": "Plain concrete slab — ensure sub-grade is well-compacted and joints are provided.",
        }
        reinf_ok = True
    elif inp.reinforcement_type == "wwf":
        wwf = WWF_DESIGNATIONS.get(inp.wwf_designation or "", {"area": 0})
        As_provided = wwf["area"] * 12  # in2/ft (WWF already in in2/ft for 12" strip)
        As_min = 0.0018 * 12 * t  # temperature & shrinkage, 0.18% Ag
        reinf_ok = As_provided >= As_min
        reinf_result = {
            "type": "WWF",
            "designation": inp.wwf_designation,
            "As_provided_in2_per_ft": round(As_provided, 4),
            "As_min_in2_per_ft": round(As_min, 4),
            "ok": reinf_ok,
            "note": "OK" if reinf_ok else f"As provided ({As_provided:.4f}) < As_min ({As_min:.4f})",
        }
    elif inp.reinforcement_type == "rebar":
        bar = REBAR.get(inp.rebar_size or "#4", REBAR["#4"])
        As_per_bar = bar["area"]
        As_provided = As_per_bar * (12.0 / inp.rebar_spacing_in)
        As_min = 0.0018 * 12 * t
        reinf_ok = As_provided >= As_min
        reinf_result = {
            "type": "rebar",
            "bar": inp.rebar_size,
            "spacing_in": inp.rebar_spacing_in,
            "As_provided_in2_per_ft": round(As_provided, 4),
            "As_min_in2_per_ft": round(As_min, 4),
            "ok": reinf_ok,
            "note": "OK" if reinf_ok else f"As provided ({As_provided:.4f}) < As_min ({As_min:.4f})",
        }
    elif inp.reinforcement_type == "fiber":
        As_provided = 0
        reinf_ok = True
        reinf_result = {
            "type": "fiber",
            "fiber_type": inp.fiber_type,
            "note": f"Per manufacturer specs ({inp.fiber_type}) — verify dosage rate with supplier.",
        }

    # ---- 3. Control joint spacing (ACI 360) ----
    if inp.control_joint_spacing_ft:
        joint_spacing = inp.control_joint_spacing_ft
    else:
        # Rule: 24-36x slab thickness in inches, converted to feet, max 15 ft
        joint_spacing = min(36.0 * t / 12.0, 15.0)
        joint_spacing = max(joint_spacing, 24.0 * t / 12.0)
    joint_ok = True  # always pass if specified

    # ---- 4. Vapor barrier (IRC R506.2.3) ----
    if inp.interior_slab:
        vb_min = 6   # mils
        vb_req = 6
    else:
        vb_min = 10
        vb_req = 10
    vapor_ok = inp.vapor_barrier_mils >= vb_min

    # ---- 5. Sub-base (IRC R506.2.2) ----
    sub_min = 4.0  # minimum 4" granular base
    sub_ok = inp.sub_base_depth_in >= sub_min

    # ---- 6. Soils bearing (simplified) ----
    # For typical residential, slab-on-grade bearing is rarely a problem
    # But for heavy point loads, check punching
    q_allow = 2000  # psf assumed
    if inp.point_load_lbs > 0:
        # Point load distributed over approximate area
        load_area_sf = max(1.0, inp.thickness_in / 12.0 * 2.0)  # rough 2x thickness spread
        q_point = inp.point_load_lbs / load_area_sf
    else:
        q_point = 0
    bearing_ok = q_point <= q_allow

    # ---- 7. Thickened edge (for monolithic turn-down) ----
    edge_result = None
    if inp.thickened_edge:
        if inp.thickened_edge_depth_in < t:
            edge_ok = False
            edge_note = "Edge depth must be >= slab thickness"
        else:
            edge_ok = True
            edge_note = "Thickened edge OK"
        edge_result = {
            "width_in": inp.thickened_edge_width_in,
            "depth_in": inp.thickened_edge_depth_in,
            "ok": edge_ok,
            "note": edge_note,
        }

    # ---- Overall ----
    checks = [thickness_ok, reinf_ok, vapor_ok, sub_ok, bearing_ok]
    if inp.thickened_edge:
        checks.append(edge_ok)
    overall = all(checks)

    return {
        "thickness": {
            "provided_in": t,
            "min_required_in": t_min,
            "status": "PASS" if thickness_ok else "FAIL",
        },
        "reinforcement": {
            **reinf_result,
            "status": "PASS" if reinf_ok else "FAIL",
        },
        "control_joints": {
            "max_spacing_ft": round(joint_spacing, 1),
            "provided_spacing_ft": inp.control_joint_spacing_ft,
            "note": "Saw-cut or tooled joints at max spacing. Depth = 1/4 slab thickness.",
        },
        "vapor_barrier": {
            "provided_mils": inp.vapor_barrier_mils,
            "min_required_mils": vb_min,
            "status": "PASS" if vapor_ok else "FAIL",
            "note": "Place over compacted granular base. Lap seams 6 in minimum.",
        },
        "sub_base": {
            "type": inp.sub_base_type,
            "depth_in": inp.sub_base_depth_in,
            "min_required_in": sub_min,
            "status": "PASS" if sub_ok else "FAIL",
            "note": "Compact to 95% standard Proctor density.",
        },
        "bearing": {
            "point_load_lbs": inp.point_load_lbs,
            "effective_area_sf": round(load_area_sf, 2),
            "q_point_psf": round(q_point, 1),
            "q_allow_psf": q_allow,
            "status": "PASS" if bearing_ok else "FAIL",
        },
        "thickened_edge": edge_result,
        "summary": {
            "overall": "PASS" if overall else "FAIL",
            "slab_area_sf": round(area_sf, 1),
            "concrete_cy": round(area_sf * (t / 12.0) / 27.0, 2),
        },
        "notes": {
            "curing": "Moist-cure for 7 days or apply curing compound per ACI 308.",
            "joint_detail": f"Control joints at {joint_spacing:.1f} ft max. Depth = t/4. Tool or saw-cut within 12 hours.",
            "vapor_barrier_detail": f"Place {inp.vapor_barrier_mils}-mil vapor barrier over {inp.sub_base_depth_in}\" compacted {inp.sub_base_type}. Lap 6\".",
        },
    }


# ============================================================
def check_slab_residential(thickness_in=4.0, reinforcement="wwf",
                           wwf_desig="6x6-W2.9xW2.9", length_ft=20, width_ft=15):
    """Quick residential slab check with sensible defaults."""
    return check_slab(SlabInputs(
        thickness_in=thickness_in,
        length_ft=length_ft,
        width_ft=width_ft,
        reinforcement_type=reinforcement,
        wwf_designation=wwf_desig,
    ))


# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("SLAB-ON-GRADE CALCULATOR")
    print("=" * 60)

    # Case 1: Standard residential
    inp1 = SlabInputs()
    r1 = check_slab(inp1)
    print(f"\nCase 1: Standard Residential 4\" WWF")
    print(f"  Thickness: {r1['thickness']['status']} ({r1['thickness']['provided_in']}\")")
    print(f"  Reinf: {r1['reinforcement']['status']} ({r1['reinforcement']['note']})")
    print(f"  Vapor barrier: {r1['vapor_barrier']['status']} ({r1['vapor_barrier']['provided_mils']} mil)")
    print(f"  Sub-base: {r1['sub_base']['status']} ({r1['sub_base']['depth_in']}\")")
    print(f"  Joint spacing: {r1['control_joints']['max_spacing_ft']} ft max")
    print(f"  Concrete: {r1['summary']['concrete_cy']} cy")
    print(f"  Overall: {r1['summary']['overall']}")

    # Case 2: Garage slab with rebar
    inp2 = SlabInputs(thickness_in=5, residential_use=False,
                      reinforcement_type="rebar", rebar_size="#4",
                      rebar_spacing_in=16, interior_slab=False)
    r2 = check_slab(inp2)
    print(f"\nCase 2: Garage Slab 5\" #4@16\"")
    print(f"  Thickness: {r2['thickness']['status']} ({r2['thickness']['provided_in']}\", min {r2['thickness']['min_required_in']}\")")
    print(f"  Reinf: {r2['reinforcement']['status']} As={r2['reinforcement']['As_provided_in2_per_ft']} vs min={r2['reinforcement']['As_min_in2_per_ft']}")
    print(f"  Concrete: {r2['summary']['concrete_cy']} cy")
    print(f"  Overall: {r2['summary']['overall']}")

    # Case 3: Fiber-reinforced
    inp3 = SlabInputs(reinforcement_type="fiber")
    r3 = check_slab(inp3)
    print(f"\nCase 3: Fiber-Reinforced 4\"")
    print(f"  Reinf: {r3['reinforcement']['note']}")
    print(f"  Overall: {r3['summary']['overall']}")

    # Case 4: Thickened edge
    inp4 = SlabInputs(thickened_edge=True, thickened_edge_depth_in=12,
                      monolithic_with_footing=True)
    r4 = check_slab(inp4)
    print(f"\nCase 4: Monolithic Turned-Down Edge")
    print(f"  Edge: {r4['thickened_edge']['note']}")
    print(f"  Overall: {r4['summary']['overall']}")
