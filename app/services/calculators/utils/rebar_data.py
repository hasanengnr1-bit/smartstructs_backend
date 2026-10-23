"""
rebar_data.py
Standard ASTM A615 Grade 60 Reinforcing Bar properties.
"""

REBAR_PROPS = {
    "#3": {"diameter": 0.375, "area": 0.11},
    "#4": {"diameter": 0.500, "area": 0.20},
    "#5": {"diameter": 0.625, "area": 0.31},
    "#6": {"diameter": 0.750, "area": 0.44},
    "#7": {"diameter": 0.875, "area": 0.60},
    "#8": {"diameter": 1.000, "area": 0.79}
}

def get_rebar_area(size: str):
    return REBAR_PROPS.get(size, REBAR_PROPS["#4"])["area"]

def get_rebar_diam(size: str):
    return REBAR_PROPS.get(size, REBAR_PROPS["#4"])["diameter"]
