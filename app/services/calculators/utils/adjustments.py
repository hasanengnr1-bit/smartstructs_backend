"""
adjustments.py
NDS adjustment factors functions.
"""
import math

def cl_factor(le: float, d: float, b: float, Emin_prime: float, Fb_star: float) -> float:
    """
    Beam Stability Factor (CL) per NDS 3.3.3.
    le: unbraced length (inches)
    d: depth (inches)
    b: breadth (inches)
    """
    if d <= b:
        return 1.0
        
    RB = math.sqrt((le * d) / (b**2))
    if RB > 50:
        return 0.0 # Exceeds NDS limit
    
    Fbe = 1.20 * Emin_prime / (RB**2)
    ratio = Fbe / Fb_star
    
    term1 = (1.0 + ratio) / 1.9
    val = term1**2 - (ratio / 0.95)
    if val < 0: val = 0
    cl = term1 - math.sqrt(val)
    return min(cl, 1.0)

def cp_factor(le_d: float, Emin_prime: float, Fc_star: float, c: float = 0.8) -> float:
    """
    Column Stability Factor (CP) per NDS 3.7.1.
    c = 0.8 for sawn lumber.
    le_d is the max slenderness ratio: le1/d1 or le2/d2
    """
    if le_d > 50:
        return 0.0 # Exceeds limit
        
    Fce = 0.822 * Emin_prime / (le_d**2)
    ratio = Fce / Fc_star
    
    term1 = (1.0 + ratio) / (2.0 * c)
    val = term1**2 - (ratio / c)
    if val < 0: val = 0
    cp = term1 - math.sqrt(val)
    return min(cp, 1.0)

def cb_factor(bearing_length: float) -> float:
    """Bearing Area Factor (CB) per NDS 3.10.4."""
    if bearing_length > 0 and bearing_length < 6.0:
        return (bearing_length + 0.375) / bearing_length
    return 1.0
