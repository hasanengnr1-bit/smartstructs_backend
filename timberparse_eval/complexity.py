"""Reviewer-entered drawing descriptors for later stratified analysis.

Keep observable dimensions separate; an arbitrary combined complexity score
would hide differences in scope, print quality, and cross-sheet reading.
"""

CHOICES = {
    "study_split": {"unassigned", "pilot", "development", "held_out"},
    "project_type": {"unknown", "new_construction", "renovation", "mixed"},
    "pdf_type": {"unknown", "born_digital", "scanned", "mixed"},
    "legibility": {"unknown", "clear", "mixed", "difficult"},
    "schedule_lookup": {"unknown", "none", "some", "extensive"},
    "cross_sheet_lookup": {"unknown", "none", "some", "extensive"},
    "member_schedule": {"unknown", "yes", "no"},
}
TEXT = {"office_code", "complexity_notes"}
NUMBERS = {"stories", "roof_planes"}


def empty_profile():
    return {
        "study_split": "unassigned",
        "office_code": "",
        "project_type": "unknown",
        "stories": None,
        "roof_planes": None,
        "pdf_type": "unknown",
        "legibility": "unknown",
        "member_schedule": "unknown",
        "schedule_lookup": "unknown",
        "cross_sheet_lookup": "unknown",
        "complexity_notes": "",
    }


def validate_profile(value):
    if not isinstance(value, dict) or set(value) - set(empty_profile()):
        raise ValueError("Invalid plan profile fields")
    result = empty_profile()
    result.update(value)
    for key, allowed in CHOICES.items():
        if result[key] not in allowed:
            raise ValueError(f"Invalid {key} value")
    for key in NUMBERS:
        number = result[key]
        if number in (None, ""):
            result[key] = None
        elif isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= 100:
            raise ValueError(f"{key} must be a whole number from 1 to 100")
    for key in TEXT:
        if not isinstance(result[key], str) or len(result[key]) > (3000 if key == "complexity_notes" else 120):
            raise ValueError(f"Invalid {key}")
        result[key] = result[key].strip()
    return result
