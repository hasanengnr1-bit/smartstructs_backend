"""
load_combos.py
ASCE 7-22 ASD Load Combinations
"""

def get_load_combinations(D=0, L=0, Lr=0, S=0, W=0):
    """
    Returns a list of evaluated load combinations.
    Each combination is a dict: {'name': str, 'load': float, 'CD': float}
    """
    combos = []
    
    # CD parameters
    CD_D = 0.90
    CD_L = 1.00
    CD_Lr = 1.25
    CD_S = 1.15
    CD_W = 1.60
    
    # ASCE 7-22 §2.4.1. Snow appears as 0.7S in the ASD combinations because
    # 7-22 adopted strength-level ground snow loads (§2.4.1 note). The "or"
    # alternatives (Lr or 0.7S or R) are enumerated as separate combos, which
    # envelopes the governing case.
    S7 = 0.7 * S   # ASD snow coefficient per ASCE 7-22 §2.4.1

    # 1. D
    combos.append({'name': '1.0D', 'load': 1.0*D, 'CD': CD_D})

    # 2. D + L
    combos.append({'name': '1.0D + 1.0L', 'load': 1.0*D + 1.0*L, 'CD': CD_L})

    # 3. D + (Lr or 0.7S or R)
    combos.append({'name': '1.0D + 1.0Lr', 'load': 1.0*D + 1.0*Lr, 'CD': CD_Lr})
    combos.append({'name': '1.0D + 0.7S', 'load': 1.0*D + S7, 'CD': CD_S})

    # 4. D + 0.75L + 0.75(Lr or 0.7S or R)
    # (shortest-duration load in the combo governs CD)
    combos.append({'name': '1.0D + 0.75L + 0.75Lr', 'load': 1.0*D + 0.75*L + 0.75*Lr, 'CD': CD_Lr})
    combos.append({'name': '1.0D + 0.75L + 0.525S', 'load': 1.0*D + 0.75*L + 0.75*S7, 'CD': CD_S})

    # 5. D + 0.6W
    combos.append({'name': '1.0D + 0.6W', 'load': 1.0*D + 0.6*W, 'CD': CD_W})

    # 6. D + 0.75L + 0.75(0.6W) + 0.75(Lr or 0.7S or R)
    combos.append({'name': '1.0D + 0.45W + 0.75L + 0.75Lr', 'load': 1.0*D + 0.45*W + 0.75*L + 0.75*Lr, 'CD': CD_W})
    combos.append({'name': '1.0D + 0.45W + 0.75L + 0.525S', 'load': 1.0*D + 0.45*W + 0.75*L + 0.75*S7, 'CD': CD_W})

    # 7. 0.6D + 0.6W  (net uplift / overturning — wind opposing dead load)
    combos.append({'name': '0.6D + 0.6W', 'load': 0.6*D + 0.6*W, 'CD': CD_W})

    return combos
