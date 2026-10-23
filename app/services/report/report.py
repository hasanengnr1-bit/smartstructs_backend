"""
report.py
Tedds-style calculation report generator — self-contained HTML (and optional
PDF). Renders a full structural calculation package showing every formula,
intermediate value, and code reference, modeled after Tekla Tedds output.

Produces multi-sheet output:
  Sheet 1: Cover / Summary — project info, hazard values, member schedule
  Sheet 2+: One sheet per member with full calculation narrative

Public API:
  build_html_report(extracted_data, analysis_result, *, options=None) -> str
  render_pdf(html) -> bytes | None
"""

from __future__ import annotations

import html as _html
import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


# ------------------------------------------------------------------ helpers
def _e(v: Any) -> str:
    return _html.escape("" if v is None else str(v))


def _n(v: Any, nd: int = 2, dash: str = "-") -> str:
    if v is None or v == "":
        return dash
    try:
        f = float(v)
    except (TypeError, ValueError):
        return _e(str(v))
    if f == int(f) and abs(f) < 1e12:
        return f"{int(f):,}"
    return f"{f:,.{nd}f}"


def _pct(v: Any) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):.0f}%"
    except (TypeError, ValueError):
        return _e(str(v))


def _status_class(s: str) -> str:
    u = str(s or "").upper()
    if u in ("PASS", "OK"):
        return "res-pass"
    if u in ("FAIL", "NG"):
        return "res-fail"
    return "res-err"


def _check_icon(status: str) -> str:
    u = str(status or "").upper()
    if u in ("PASS", "OK"):
        return '<span class="pchk">&#10003;</span>'
    return '<span class="pchk fail-chk">&#10007;</span>'


def _pass_line(status: str, text: str) -> str:
    u = str(status or "").upper()
    cls = "pass" if u in ("PASS", "OK") else "pass fail-pass"
    return f'<div class="{cls}">{_check_icon(status)}{_e(u)} &mdash; {text}</div>'


# ------------------------------------------------------------------ row builders
def _row(desc: str, val: str, indent: bool = False) -> str:
    cls = ' class="row indent"' if indent else ' class="row"'
    return f'<div{cls}><div class="d">{desc}</div><div class="v">{val}</div></div>'


def _row_formula(desc: str, formula_html: str, indent: bool = False) -> str:
    cls = ' class="row indent"' if indent else ' class="row"'
    return f'<div{cls}><div class="d">{desc}</div><div class="v">{formula_html}</div></div>'


def _coderef(text: str) -> str:
    return f'<div class="coderef">{text}</div>'


def _nds_edition(project: Dict[str, Any]) -> str:
    """NDS edition per the governing code: IBC 2024 adopts NDS 2024."""
    return "NDS 2024" if "2024" in str(project.get("design_code") or "") else "NDS 2018"


def _heading(text: str, first: bool = False) -> str:
    cls = "h first" if first else "h"
    return f'<div class="{cls}">{_e(text)}</div>'


def _note(text: str) -> str:
    return f'<div class="note-line">{text}</div>'


# ------------------------------------------------------------------ sheet wrapper
def _sheet_header(opts: Dict[str, Any], project: Dict[str, Any]) -> str:
    firm = _e(opts.get("firm") or "SmartStructs")
    job = _e(opts.get("job_ref") or project.get("project_name") or "-")
    proj = _e(project.get("project_name") or opts.get("project_name") or "-")
    engr = _e(opts.get("engineer") or "-")
    date = _e(opts.get("date") or datetime.now().strftime("%Y-%m-%d"))
    rev = _e(opts.get("revision") or "0")
    return f"""<div class="hdr">
  <div class="hdr-left">
    <div><div class="wm">{firm}</div>
    <div class="wm-sub">Structural Engineering</div></div>
  </div>
  <div class="hdr-meta">
    <div class="mi"><span class="mk">Project</span><span class="mv">{proj}</span></div>
    <div class="mi"><span class="mk">Job ref.</span><span class="mv">{job}</span></div>
    <div class="mi"><span class="mk">Rev.</span><span class="mv">{rev}</span></div>
  </div>
</div>
<div class="hdr-rule"></div>
<div class="signoff-strip">
  <div class="so"><span class="sk">Prepared by</span><span class="sv">{engr}</span></div>
  <div class="so"><span class="sk">Date</span><span class="sv">{date}</span></div>
  <div class="so"><span class="sk">Checked by</span><span class="sv">&mdash;</span></div>
</div>"""


# Page-number placeholders — substituted with sequential numbers after all
# sheets are built and paginated (a long sheet may span several pages).
_PG_TOKEN = "⟦PG⟧"
_PGTOT_TOKEN = "⟦PGTOT⟧"


def _sheet_footer(
    opts: Dict[str, Any], project: Dict[str, Any], sheet_num: int, total_sheets: int
) -> str:
    firm = _e(opts.get("firm") or "SmartStructs")
    proj = _e(project.get("project_name") or "")
    job = _e(opts.get("job_ref") or proj or "")
    rev = _e(opts.get("revision") or "0")
    return (
        f'<div class="foot"><span>{firm} &middot; Structural Calculation Package</span>'
        f"<span>{proj} &middot; {job} &middot; Rev {rev}</span>"
        f"<span>Sheet {_PG_TOKEN} of {_PGTOT_TOKEN}</span></div>"
    )


# ---- pagination: split long sheet bodies into page-height chunks ----
_PAGE_BUDGET_PX = 880   # usable content height per letter page at screen scale


def _split_top_level(body: str) -> List[str]:
    """Split body HTML into its top-level <div>/<table> blocks."""
    chunks: List[str] = []
    pos = depth = 0
    for m in re.finditer(r"<(/?)(?:div|table)\b", body):
        if m.group(1):
            depth -= 1
            if depth == 0:
                end = body.find(">", m.start()) + 1
                chunks.append(body[pos:end])
                pos = end
        else:
            depth += 1
    tail = body[pos:].strip()
    if tail:
        chunks.append(tail)
    return chunks


def _est_px(chunk: str) -> int:
    """Rough rendered height of one top-level block."""
    head = chunk[:40]
    if chunk.startswith("<table"):
        return 14 + 26 * max(chunk.count("<tr"), 1)
    if 'class="calc-title' in head:
        return 24
    if 'class="calc-sub' in head:
        return 18
    if 'class="h' in head:
        return 34
    if 'class="coderef' in head:
        return 24
    if 'class="note-line' in head:
        return 18
    if 'class="pass' in head:
        return 22
    if 'class="row' in head:
        return 20 if len(chunk) < 400 else 38
    return 24 + 26 * chunk.count("<tr")


def _is_orphan(chunk: str) -> bool:
    """Headings/code refs must not be the last block on a page."""
    head = chunk[:40]
    return 'class="h' in head or 'class="coderef' in head or 'class="calc-title' in head


def _wrap_sheet(header: str, body: str, footer: str, extra_cls: str = "") -> str:
    """Wrap a sheet, breaking the body across multiple pages when it exceeds
    the page height (header and footer repeat on every page, Tedds-style)."""
    cls = f"sheet {extra_cls}".strip()
    pages: List[List[str]] = []
    cur: List[str] = []
    used = 0
    for ch in _split_top_level(body):
        h = _est_px(ch)
        if cur and used + h > _PAGE_BUDGET_PX:
            carried: List[str] = []
            while cur and _is_orphan(cur[-1]):
                carried.insert(0, cur.pop())
            if cur:
                pages.append(cur)
                cur, used = carried, sum(_est_px(c) for c in carried)
        cur.append(ch)
        used += h
    if cur:
        pages.append(cur)

    out = []
    for i, pg in enumerate(pages):
        cont = ('<div class="note-line" style="text-align:right;">&hellip; continued</div>'
                if i else "")
        out.append(f'<div class="{cls}">{header}<div class="body">{cont}{"".join(pg)}</div>{footer}</div>')
    return "\n".join(out)


# Source-types that are timber (NDS) flexural/axial members. Shared by the
# cover design-basis rows and the closing disclaimer so both describe the same
# set of modules actually present in the package.
_TIMBER_SOURCE_TYPES = {
    "roof_rafter", "ceiling_joist", "ridge_beam", "hip_valley_rafter",
    "roof_drop_beam", "roof_flush_beam", "floor_joist", "floor_drop_beam",
    "floor_flush_beam", "header", "stud_wall", "post",
}

# Some result keys name the demand, not the limit state. The support-reaction
# check is really bearing / compression-perpendicular-to-grain (NDS §3.10).
_CHECK_DISPLAY_NAMES = {
    "Member Reaction (lbs)": "Bearing (Fc⟂)",
}


def _check_display_name(name: str) -> str:
    return _CHECK_DISPLAY_NAMES.get(name, name)


# ------------------------------------------------------------------ SHEET 1: Cover/Summary
def _build_cover_sheet(
    extracted: Dict[str, Any],
    analysis: Dict[str, Any],
    opts: Dict[str, Any],
    project: Dict[str, Any],
    total_sheets: int,
) -> str:
    header = _sheet_header(opts, project)
    summ = analysis.get("summary", {})

    code = project.get("design_code") or "IBC 2024"
    # The calc engine runs ASD wood combos unconditionally — always report the
    # method actually used, never echo a stored project preference.
    method = "ASD"
    risk = project.get("risk_category") or "II"

    hz = extracted.get("hazard_lookup_result") or {}
    seis = hz.get("seismic") or {} if isinstance(hz, dict) else {}
    sdc = seis.get("sdc") or "—"  # literal char: this value gets HTML-escaped

    # Cite only the standards / scope actually exercised by this package
    items_all = analysis.get("items", [])
    types = {it.get("source_type") for it in items_all}
    timber_types = _TIMBER_SOURCE_TYPES
    has_lateral_content = bool(analysis.get("lateral")) or "shear_wall" in types
    has_gravity_content = bool(types) and not (types <= {"shear_wall", "diaphragm"})

    if has_gravity_content and has_lateral_content:
        scope_txt = "gravity &amp; lateral member design"
    elif has_lateral_content:
        scope_txt = "lateral member design"
    elif has_gravity_content:
        scope_txt = "gravity member design"
    else:
        scope_txt = "structural member design"

    body = ""
    body += '<div class="calc-title">STRUCTURAL CALCULATION PACKAGE</div>'
    body += f'<div class="calc-sub">Residential structure &mdash; {scope_txt}</div>'

    body += _heading("Design basis", first=True)
    body += _row("Governing building code", f"<b>{_e(code)}</b>")
    body += _row("Loads", "<b>ASCE 7</b>")
    if types & timber_types:
        body += _row("Timber design standard / method",
                     f"{_nds_edition(project)} &mdash; <b>{_e(method)}</b>")
    if "i_joist" in types:
        body += _row("I-joists", "Weyerhaeuser TJ-4000 / <b>ICC-ES ESR-1153</b>")
    if "masonry_wall" in types:
        body += _row("Masonry design standard / method",
                     "TMS 402/602-16 &mdash; <b>Strength</b>")
    if types & {"continuous_footing", "pad_footing", "slab"}:
        body += _row("Concrete design standard", "<b>ACI 318-19</b>")
    if "shear_wall" in types:
        body += _row("Wood shear walls", "<b>SDPWS 2021</b> / NDS")
    if seis or has_lateral_content:
        body += _row(
            "Risk category / Seismic design category", f"{_e(risk)} / <b>{_e(sdc)}</b>"
        )

    # Hazard summary rows
    if seis:
        body += _heading("Site hazard values")
        wind = (hz.get("wind") or {}) if isinstance(hz, dict) else {}
        snow = (hz.get("snow") or {}) if isinstance(hz, dict) else {}
        body += _row("S<sub>DS</sub>", f"<b>{_n(seis.get('sds'), 3)}</b> g")
        body += _row("S<sub>D1</sub>", f"<b>{_n(seis.get('sd1'), 3)}</b> g")
        body += _row(
            "Basic wind speed V", f"<b>{_n(wind.get('wind_speed_mph'), 0)}</b> mph"
        )
        hz_input = (hz.get("input") or {}) if isinstance(hz, dict) else {}
        exposure_used = hz_input.get("exposure_category")
        if exposure_used:
            body += _row("Wind exposure category", f"<b>{_e(str(exposure_used))}</b>")
        body += _row(
            "Ground snow p<sub>g</sub>",
            f"<b>{_n(snow.get('snow_psf'), 0)}</b> psf",
        )

    # Member schedule table
    body += _heading("Member schedule")
    rows_html = ""
    items = analysis.get("items", [])
    gov_item = None
    gov_util = -1.0
    for it in items:
        u = it.get("max_utilization")
        pct_val = u if isinstance(u, (int, float)) else None
        s = str(it.get("status") or "").upper()
        scls = _status_class(s)
        gov_check_name = ""
        if it.get("checks"):
            gov_check: dict[str, Any] = max(
                it["checks"],
                key=lambda c: (
                    float(c.get("utilization") or 0)
                    if isinstance(c.get("utilization"), (int, float))
                    else 0
                ),
                default={},
            )
            gov_check_name = _check_display_name(gov_check.get("name") or "")
            if gov_check.get("combo"):
                gov_check_name += f" ({gov_check['combo']})"
        rows_html += (
            f"""<tr{"" if s != "FAIL" else ' class="gov"'}>"""
            f'<td>{_e(it.get("label"))}</td>'
            f'<td>{_e(it.get("application"))}</td>'
            f'<td>{_e(it.get("size") or "-")}</td>'
            f"<td>{_e(gov_check_name)}</td>"
            f'<td class="r">{_pct(pct_val)}</td>'
            f'<td class="r {scls}">{s}</td></tr>'
        )
        if pct_val is not None and pct_val > gov_util:
            gov_util = pct_val
            gov_item = it

    if items:
        body += f"""<table class="dt"><thead><tr>
<th style="width:22%">Member</th><th>Check type</th><th>Solution</th>
<th>Governing check</th><th class="r">Max util.</th><th class="r">Result</th>
</tr></thead><tbody>{rows_html}</tbody></table>"""
        total = summ.get("total_items", 0)
        passing = summ.get("passing", 0)
        gov_label = _e(gov_item.get("label")) if gov_item else "-"
        gov_check = ""
        if gov_item and gov_item.get("checks"):
            _gc = max(
                gov_item["checks"],
                key=lambda c: (float(c.get("utilization") or 0)
                               if isinstance(c.get("utilization"), (int, float)) else 0),
                default={},
            )
            gov_check = _e(_check_display_name(_gc.get("name") or ""))
        gov_txt = (
            f" Governing case &mdash; {gov_label}"
            + (f", {gov_check}" if gov_check else "")
            + f" at {_pct(gov_util)}."
            if gov_item and gov_util >= 0 else ""
        )
        body += _note(
            f"<b>Result:</b> {passing} of {total} candidate section(s) pass all "
            f"strength and serviceability limit states.{gov_txt}"
        )
    else:
        body += _note(
            "No individual member checks in this package &mdash; see the "
            "following sheets for load analysis results."
        )

    footer = _sheet_footer(opts, project, 1, total_sheets)
    return _wrap_sheet(header, body, footer)


# ------------------------------------------------------------------ SHEET 2: Site Hazard Data
def _hazard_sheet(
    hazard: Dict[str, Any],
    opts: Dict[str, Any],
    project: Dict[str, Any],
    sheet_num: int,
    total_sheets: int,
) -> str:
    header = _sheet_header(opts, project)
    wind = hazard.get("wind") or {}
    snow = hazard.get("snow") or {}
    seis = hazard.get("seismic") or {}

    body = '<div class="calc-title">SITE HAZARD DATA</div>'
    body += '<div class="calc-sub">ASCE 7-22 &middot; USGS Design Maps / ASCE Hazard Tool</div>'

    body += _heading("Site", first=True)
    if project.get("latitude") is not None:
        body += _row("Latitude / Longitude",
                     f"<b>{_n(project.get('latitude'), 5)}</b> / <b>{_n(project.get('longitude'), 5)}</b>")
    body += _row("Risk category", f"<b>{_e(project.get('risk_category') or 'II')}</b>")
    body += _row("Site class", f"<b>{_e(project.get('site_class') or 'Default')}</b>")

    body += _heading("Wind")
    body += _coderef("Basic wind speed &mdash; ASCE 7-22 Fig. 26.5-1")
    body += _row("Basic (ultimate) wind speed",
                 f"V = <b>{_n(wind.get('wind_speed_mph'), 0)}</b> mph (3-s gust, Exposure C, 33 ft)")

    body += _heading("Snow")
    body += _coderef("Ground snow load &mdash; ASCE 7-22 &sect;7.2")
    body += _row("Ground snow load", f"p<sub>g</sub> = <b>{_n(snow.get('snow_psf'), 0)}</b> psf")

    body += _heading("Seismic")
    body += _coderef("Mapped &amp; design accelerations &mdash; ASCE 7-22 &sect;11.4 (USGS)")
    seis_rows = [
        ("MCE<sub>R</sub> short-period acceleration", "S<sub>S</sub>", seis.get("ss"), "g"),
        ("MCE<sub>R</sub> 1-second acceleration", "S<sub>1</sub>", seis.get("s1"), "g"),
        ("Site-modified short-period", "S<sub>MS</sub>", seis.get("sms"), "g"),
        ("Site-modified 1-second", "S<sub>M1</sub>", seis.get("sm1"), "g"),
        ("Design short-period", "S<sub>DS</sub> = 2/3 &middot; S<sub>MS</sub>", seis.get("sds"), "g"),
        ("Design 1-second", "S<sub>D1</sub> = 2/3 &middot; S<sub>M1</sub>", seis.get("sd1"), "g"),
        ("Peak ground acceleration", "PGA<sub>M</sub>", seis.get("pgam"), "g"),
        ("Transition period", "T<sub>L</sub>", seis.get("tl"), "s"),
        ("Short transition", "T<sub>0</sub>", seis.get("t0"), "s"),
        ("Transition", "T<sub>S</sub> = S<sub>D1</sub>/S<sub>DS</sub>", seis.get("ts"), "s"),
    ]
    for desc, sym, val, unit in seis_rows:
        if val is not None:
            body += _row(desc, f"{sym} = <b>{_n(val, 3)}</b> {unit}")
    if seis.get("sdc"):
        body += _row("Seismic design category", f"SDC = <b>{_e(seis.get('sdc'))}</b>")
    body += _note(
        "Hazard values retrieved from the USGS Design Maps web service and the "
        "ASCE Hazard Tool for the coordinates above."
    )

    footer = _sheet_footer(opts, project, sheet_num, total_sheets)
    return _wrap_sheet(header, body, footer, "member-sheet")


# ------------------------------------------------------------------ SHEET 3: Lateral Load Analysis
def _zone_table(rows: List[Dict[str, Any]]) -> str:
    """Tedds-style zone pressure/force table."""
    body_rows = "".join(
        f'<tr><td>{_e(z.get("zone"))}</td>'
        f'<td class="r">{_n(z.get("z_ref_ft"), 2)}</td>'
        f'<td class="r">{_n(z.get("cpe"), 2)}</td>'
        f'<td class="r">{_n(z.get("qp_psf"), 2)}</td>'
        f'<td class="r">{_n(z.get("p_psf"), 2)}</td>'
        f'<td class="r">{_n(z.get("area_sf"), 2)}</td>'
        f'<td class="r">{_n((z.get("F_lbs") or 0) / 1000.0, 2)}</td></tr>'
        for z in rows
    )
    return f"""<table class="dt"><thead><tr>
<th>Zone</th><th class="r">Ref. height (ft)</th><th class="r">c<sub>pe</sub></th>
<th class="r">q<sub>p</sub> (psf)</th><th class="r">Net p (psf)</th>
<th class="r">A<sub>ref</sub> (ft&sup2;)</th><th class="r">F<sub>w</sub> (kips)</th>
</tr></thead><tbody>{body_rows}</tbody></table>"""


def _wind_directional_html(pkg: Dict[str, Any]) -> str:
    """Full ASCE 7-22 Ch. 27 directional wind package, Tedds-style:
    building data → requirements → gust factor → velocity pressures →
    per-case zone tables → overall loading."""
    bld = pkg.get("building") or {}
    req = pkg.get("requirements") or {}
    gust = pkg.get("gust") or {}

    body = _heading("Wind loading — ASCE 7-22 directional procedure (Ch. 27)")

    body += _coderef("Building data")
    body += _row("Type of roof", f"<b>{_e(bld.get('roof_type'))}</b>")
    body += _row("Length of building", f"b = <b>{_n(bld.get('length_ft'), 2)}</b> ft")
    body += _row("Width of building", f"d = <b>{_n(bld.get('width_ft'), 2)}</b> ft")
    body += _row("Height to eaves", f"H = <b>{_n(bld.get('eave_height_ft'), 2)}</b> ft")
    body += _row("Pitch of roof", f"&theta; = <b>{_n(bld.get('roof_pitch_deg'), 1)}</b>&deg;")
    body += _row("Mean height", f"h = <b>{_n(bld.get('mean_roof_height_ft'), 2)}</b> ft")

    body += _coderef("General wind load requirements")
    body += _row("Basic wind speed", f"V = <b>{_n(req.get('V_mph'), 1)}</b> mph")
    body += _row("Risk category", f"<b>{_e(req.get('risk_category'))}</b>")
    body += _row("Directionality factor (Table 26.6-1)", f"K<sub>d</sub> = <b>{_n(req.get('Kd'), 2)}</b>")
    body += _row(
        "Ground elevation factor",
        f"z<sub>gl</sub> = {_n(req.get('ground_elevation_ft'), 0)} ft &rarr; "
        f"K<sub>e</sub> = exp(&minus;0.0000362 &middot; z<sub>gl</sub>) = <b>{_n(req.get('Ke'), 2)}</b>",
    )
    body += _row("Exposure category (cl. 26.7.3)", f"<b>{_e(req.get('exposure'))}</b>")
    body += _row("Enclosure classification (cl. 26.12)", f"<b>{_e(req.get('enclosure'))}</b>")
    body += _row(
        "Internal pressure coef. (Table 26.13-1)",
        f"GC<sub>pi</sub> = <b>&plusmn;{_n(req.get('GCpi'), 2)}</b>",
    )

    body += _coderef("Gust effect factor for rigid structures &mdash; &sect;26.11")
    body += _row(
        "Terrain constants (Table 26.11-1)",
        f"&#8467; = <b>{_n(gust.get('l_ft'), 1)}</b> ft, c = <b>{_n(gust.get('c'), 2)}</b>, "
        f"&epsilon;&#772; = <b>{_n(gust.get('eps'), 3)}</b>, z<sub>min</sub> = <b>{_n(gust.get('zmin_ft'), 1)}</b> ft",
    )
    body += _row(
        "Peak factors",
        f"g<sub>Q</sub> = g<sub>v</sub> = <b>{_n(gust.get('gQ'), 1)}</b>",
    )
    body += _row_formula(
        "Equivalent height",
        f"z&#772; = max(0.6&middot;h, z<sub>min</sub>) = <b>{_n(gust.get('z_bar_ft'), 2)}</b> ft",
    )
    body += _row_formula(
        "Turbulence intensity (Eq. 26.11-7)",
        f"I<sub>z&#772;</sub> = c &middot; (33/z&#772;)<sup>1/6</sup> = <b>{_n(gust.get('Iz'), 3)}</b>",
    )
    body += _row_formula(
        "Integral length scale (Eq. 26.11-9)",
        f"L<sub>z&#772;</sub> = &#8467; &middot; (z&#772;/33)<sup>&epsilon;&#772;</sup> = <b>{_n(gust.get('Lz_ft'), 2)}</b> ft",
    )
    body += _row_formula(
        "Background response (Eq. 26.11-8)",
        f"Q = &radic;(1 / (1 + 0.63&middot;((min(b,d)+h)/L<sub>z&#772;</sub>)<sup>0.63</sup>)) "
        f"= <b>{_n(gust.get('Q'), 3)}</b>",
    )
    body += _row_formula(
        "Gust effect factor (Eq. 26.11-6)",
        f"G = 0.925 &middot; (1 + 1.7&middot;g<sub>Q</sub>&middot;I<sub>z&#772;</sub>&middot;Q) / "
        f"(1 + 1.7&middot;g<sub>v</sub>&middot;I<sub>z&#772;</sub>) = <b>{_n(gust.get('G'), 3)}</b>",
    )
    body += _row(
        "Minimum design loads (cl. 27.1.5)",
        f"p<sub>min,wall</sub> = <b>{_n(pkg.get('min_wall_psf'), 0)}</b> psf, "
        f"p<sub>min,roof</sub> = <b>{_n(pkg.get('min_roof_psf'), 0)}</b> psf",
    )
    body += _row("Topography", f"K<sub>zt</sub> = <b>{_n(req.get('Kzt'), 2)}</b>")

    body += _coderef("Velocity pressure &mdash; Eq. 26.10-1")
    body += _row_formula(
        "Velocity pressure equation",
        "q<sub>z</sub> = 0.00256 &middot; K<sub>z</sub> &middot; K<sub>zt</sub> &middot; "
        "K<sub>e</sub> &middot; V&sup2;",
    )
    vp_rows = "".join(
        f'<tr><td class="r">{_n(v.get("z_ft"), 2)}</td>'
        f'<td class="r">{_n(v.get("Kz"), 3)}</td>'
        f'<td class="r">{_n(v.get("qz_psf"), 2)}</td></tr>'
        for v in (pkg.get("velocity_pressures") or [])
    )
    body += f"""<table class="dt"><thead><tr>
<th class="r">z (ft)</th><th class="r">K<sub>z</sub> (Table 26.10-1)</th><th class="r">q<sub>z</sub> (psf)</th>
</tr></thead><tbody>{vp_rows}</tbody></table>"""
    body += _row(
        "Peak velocity pressure, internal (as roof press.)",
        f"q<sub>i</sub> = <b>{_n(pkg.get('qi_psf'), 2)}</b> psf",
    )
    body += _row_formula(
        "Net pressure / force",
        "p = K<sub>d</sub>&middot;q&middot;G&middot;c<sub>pe</sub> &minus; "
        "K<sub>d</sub>&middot;q<sub>i</sub>&middot;GC<sub>pi</sub>; &nbsp; "
        "F<sub>w</sub> = p &middot; A<sub>ref</sub>",
    )

    for case in (pkg.get("load_cases") or []):
        ov = case.get("overall") or {}
        body += _heading(f"Load case — {_e(case.get('name'))}")
        body += _coderef("Roof zones")
        body += _zone_table(case.get("roof_zones") or [])
        body += _row(
            "Roof force resultants",
            f"F<sub>w,v</sub> = <b>{_n((case.get('roof_total_vertical_lbs') or 0) / 1000.0, 2)}</b> kips, "
            f"F<sub>w,h</sub> = <b>{_n((case.get('roof_total_horizontal_lbs') or 0) / 1000.0, 2)}</b> kips",
        )
        body += _coderef("Wall zones")
        body += _zone_table(case.get("wall_zones") or [])
        body += _coderef("Overall loading")
        body += _row(
            "Projected areas",
            f"A<sub>vert,wall</sub> = <b>{_n(ov.get('A_vert_wall_sf'), 2)}</b> ft&sup2;, "
            f"A<sub>vert,roof</sub> = <b>{_n(ov.get('A_vert_roof_sf'), 2)}</b> ft&sup2;",
        )
        body += _row_formula(
            "Minimum overall loading (cl. 27.1.5)",
            f"F<sub>min</sub> = 16&middot;A<sub>wall</sub> + 8&middot;A<sub>roof</sub> = "
            f"<b>{_n((ov.get('F_min_lbs') or 0) / 1000.0, 2)}</b> kips",
        )
        body += _row_formula(
            "Overall horizontal loading",
            f"F<sub>total</sub> = max(F<sub>ww</sub> &minus; F<sub>lw</sub> + F<sub>w,h</sub>, F<sub>min</sub>) = "
            f"max({_n((ov.get('F_windward_lbs') or 0) / 1000.0, 2)} &minus; "
            f"({_n((ov.get('F_leeward_lbs') or 0) / 1000.0, 2)}) + "
            f"{_n((ov.get('F_roof_horiz_lbs') or 0) / 1000.0, 2)}, "
            f"{_n((ov.get('F_min_lbs') or 0) / 1000.0, 2)}) = "
            f"<b>{_n((ov.get('F_total_lbs') or 0) / 1000.0, 2)}</b> kips"
            + (" &nbsp;(minimum governs)" if ov.get("governed_by_minimum") else ""),
        )

    summ = pkg.get("summary") or {}
    if summ:
        body += _heading("Wind loading summary")
        rows = ""
        for key, label in (("wind_0", "Wind 0&deg; (normal to ridge)"),
                           ("wind_90", "Wind 90&deg; (parallel to ridge)")):
            s = summ.get(key) or {}
            rows += (
                f"<tr><td>{label}</td>"
                f"<td>{_e(s.get('governing_case'))}</td>"
                f'<td class="r">{_n((s.get("F_total_lbs") or 0) / 1000.0, 2)}</td>'
                f'<td class="r">{_n((s.get("max_roof_uplift_lbs") or 0) / 1000.0, 2)}</td></tr>'
            )
        body += f"""<table class="dt"><thead><tr>
<th>Direction</th><th>Governing case</th><th class="r">F<sub>total</sub> (kips)</th>
<th class="r">Roof uplift (kips)</th>
</tr></thead><tbody>{rows}</tbody></table>"""
    return body


def _lateral_sheet(
    lateral: Dict[str, Any],
    opts: Dict[str, Any],
    project: Dict[str, Any],
    sheet_num: int,
    total_sheets: int,
) -> str:
    header = _sheet_header(opts, project)
    meta = lateral.get("computed") or {}
    seis = meta.get("seismic") or {}
    wind = meta.get("wind") or {}
    story_shears = lateral.get("story_shears") or {}
    line_shears = lateral.get("line_shears") or {}

    body = '<div class="calc-title">LATERAL LOAD ANALYSIS</div>'
    body += '<div class="calc-sub">ASCE 7-22 &middot; seismic ELF (&sect;12.8) &amp; wind MWFRS directional (Ch. 26/27)</div>'

    heights = meta.get("story_heights_ft") or []
    weights = meta.get("weights_psf") or {}
    if meta:
        body += _heading("Building parameters", first=True)
        if meta.get("length_ft"):
            body += _row(
                "Plan dimensions",
                f"L &times; B = <b>{_n(meta.get('length_ft'), 1)}</b> &times; "
                f"<b>{_n(meta.get('width_ft'), 1)}</b> ft",
            )
        body += _row("Number of stories", f"<b>{_n(meta.get('stories'), 0)}</b>")
    if heights:
        body += _row("Story heights", f"<b>{', '.join(_n(h, 1) for h in heights)}</b> ft")
    if meta:
        body += _row("Mean roof height", f"h = <b>{_n(meta.get('mean_roof_height_ft'), 1)}</b> ft")
    if weights:
        body += _row(
            "Assumed dead loads",
            f"roof <b>{_n(weights.get('roof_dead'), 0)}</b> / floor "
            f"<b>{_n(weights.get('floor_dead'), 0)}</b> / wall "
            f"<b>{_n(weights.get('wall_dead'), 0)}</b> psf",
        )

    # --- Seismic ELF with formulas ---
    if seis:
        hn = sum(float(h) for h in heights) if heights else None
        body += _heading("Seismic base shear — Equivalent Lateral Force")
        body += _coderef("Approximate period &mdash; ASCE 7-22 &sect;12.8.2.1")
        body += _row_formula(
            "Fundamental period",
            f"T<sub>a</sub> = 0.02 &middot; h<sub>n</sub><sup>0.75</sup>"
            + (f" = 0.02 &middot; {_n(hn, 1)}<sup>0.75</sup>" if hn else "")
            + f" = <b>{_n(seis.get('Ta_s'), 3)}</b> s",
        )
        body += _coderef("Seismic response coefficient &mdash; ASCE 7-22 &sect;12.8.1.1")
        body += _row("Response modification factor", f"R = <b>{_n(seis.get('R'), 1)}</b> (light-frame WSP walls)")
        body += _row("Importance factor", f"I<sub>e</sub> = <b>{_n(seis.get('Ie'), 2)}</b>")
        body += _row("Redundancy factor", f"&rho; = <b>{_n(seis.get('rho'), 1)}</b>")
        body += _row_formula(
            "Response coefficient",
            f"C<sub>s</sub> = S<sub>DS</sub> / (R/I<sub>e</sub>) = <b>{_n(seis.get('Cs'), 4)}</b>"
            " &nbsp;(with &sect;12.8.1.1 upper/lower bounds applied)",
        )
        body += _coderef("Base shear &mdash; ASCE 7-22 Eq. 12.8-1")
        body += _row("Effective seismic weight", f"W = <b>{_n(seis.get('W_lbs'), 0)}</b> lbs")
        body += _row_formula(
            "Base shear (strength)",
            f"V = C<sub>s</sub> &middot; W = {_n(seis.get('Cs'), 4)} &middot; {_n(seis.get('W_lbs'), 0)} "
            f"= <b>{_n(seis.get('V_base_lbs'), 0)}</b> lbs",
        )
        body += _row("Vertical distribution exponent", f"k = <b>{_n(seis.get('k'), 2)}</b>")
        body += _row_formula(
            "ASD-level story shear", "V<sub>ASD</sub> = 0.7 &middot; &rho; &middot; V<sub>x</sub> &nbsp;(&sect;2.4.5)"
        )

    # --- Wind: full Ch. 27 directional procedure (Tedds-style) ---
    pkg = wind.get("directional") or {}
    if pkg:
        body += _wind_directional_html(pkg)
    elif wind:
        body += _heading("Wind — MWFRS directional procedure")
        body += _row("Basic wind speed", f"V = <b>{_n(wind.get('V_mph'), 0)}</b> mph")
        body += _row("Exposure category", f"<b>{_e(wind.get('exposure') or 'B')}</b>")
        for d in ("X", "Y"):
            wd = wind.get(d) or {}
            if not wd:
                continue
            body += _row_formula(
                f"{d}-direction: velocity pressure",
                f"q<sub>h</sub> = <b>{_n(wd.get('qh_psf'), 2)}</b> psf "
                f"(C<sub>p,lw</sub> = {_n(wd.get('Cp_leeward'), 2)})",
            )

    # --- Story shear table ---
    if story_shears:
        body += _heading("Story shears (ASD level)")
        rows = ""
        for story in sorted(story_shears, key=lambda s: int(s) if str(s).isdigit() else 0):
            dirs = story_shears[story] or {}
            x = dirs.get("X") or {}
            y = dirs.get("Y") or {}
            rows += (
                f"<tr><td>Story {_e(story)}</td>"
                f'<td class="r">{_n(x.get("Vs"), 0)}</td><td class="r">{_n(x.get("Vw"), 0)}</td>'
                f'<td class="r">{_n(y.get("Vs"), 0)}</td><td class="r">{_n(y.get("Vw"), 0)}</td></tr>'
            )
        body += f"""<table class="dt"><thead>
<tr><th rowspan="2">Level</th><th colspan="2" class="r">X-direction (lbs)</th><th colspan="2" class="r">Y-direction (lbs)</th></tr>
<tr><th class="r">V<sub>s</sub> (0.7E)</th><th class="r">V<sub>w</sub> (0.6W)</th><th class="r">V<sub>s</sub> (0.7E)</th><th class="r">V<sub>w</sub> (0.6W)</th></tr>
</thead><tbody>{rows}</tbody></table>"""

    # --- Line shear distribution ---
    if line_shears:
        body += _heading("Distribution to shear wall lines")
        body += _note(
            "Story shear distributed to parallel lines by sheathed length, "
            "enveloped with an equal-share-per-line minimum."
        )
        rows = ""
        for key, rec in line_shears.items():
            story, _, line = str(key).partition("|")
            rows += (
                f"<tr><td>{_e(story)}</td><td>{_e(line.upper())}</td>"
                f'<td>{_e(rec.get("direction") or "-")}</td>'
                f'<td class="r">{_n(rec.get("total_length_ft"), 1)}</td>'
                f'<td class="r">{_n(rec.get("Vs"), 0)}</td>'
                f'<td class="r">{_n(rec.get("Vw"), 0)}</td></tr>'
            )
        body += f"""<table class="dt"><thead><tr>
<th>Story</th><th>Line</th><th>Dir.</th><th class="r">Length (ft)</th>
<th class="r">V<sub>s</sub> (lbs)</th><th class="r">V<sub>w</sub> (lbs)</th>
</tr></thead><tbody>{rows}</tbody></table>"""

    # --- Component & cladding pressures ---
    candc = lateral.get("component_pressures") or []
    if candc:
        body += _heading("Component & cladding wind pressures")
        body += _coderef("C&amp;C walls &mdash; ASCE 7-22 Fig. 30.3-1 (GC<sub>pi</sub> = &plusmn;0.18, 16 psf min)")
        rows = "".join(
            f'<tr><td>{_e(c.get("label") or f"Component {i + 1}")}</td>'
            f'<td class="r">{_n(c.get("effective_wind_area_sf"), 1)}</td>'
            f'<td class="r">{_n(c.get("zone"), 0)}</td>'
            f'<td class="r">{_n(c.get("qh_psf"), 2)}</td>'
            f'<td class="r">{_n(c.get("design_pressure_psf_strength"), 1)}</td>'
            f'<td class="r">{_n(c.get("design_pressure_psf_asd"), 1)}</td></tr>'
            for i, c in enumerate(candc)
        )
        body += f"""<table class="dt"><thead><tr>
<th>Component</th><th class="r">Eff. area (sf)</th><th class="r">Zone</th>
<th class="r">q<sub>h</sub> (psf)</th><th class="r">p (strength, psf)</th><th class="r">p (ASD, psf)</th>
</tr></thead><tbody>{rows}</tbody></table>"""

    # --- Assumptions ---
    assumptions = meta.get("assumptions") or []
    if assumptions:
        body += _heading("Assumptions")
        for a in assumptions:
            body += _note(f"&bull; {_e(a)}")

    # --- Design assumptions ---
    body += _heading("Design assumptions")
    for lim in (
        "Analysis basis: regular, enclosed low-rise construction (mean roof "
        "height &le; 60 ft) with flexible diaphragms, per ASCE 7-22 &sect;12.3.1.1.",
        "Wind: MWFRS directional procedure (Ch. 27) — rigid-structure gust "
        "factor per &sect;26.11, velocity pressure per height (Table 26.10-1), "
        "zoned wall/roof pressure coefficients (Fig. 27.3-1), four load cases "
        "(wind 0&deg;/90&deg; &times; GC<sub>pi</sub>&plusmn;) with the "
        "&sect;27.1.5 16/8 psf minimums; a gable ridge is assumed parallel to "
        "the long plan dimension. Member out-of-plane pressures per "
        "Fig. 30.3-1 (zone 4, GC<sub>pi</sub> = &plusmn;0.18, 16 psf minimum).",
        "Seismic: Equivalent Lateral Force per &sect;12.8 with the approximate "
        "period (Eq. 12.8-8); R, I<sub>e</sub>, and &rho; per project input.",
        "Story shear is distributed to parallel wall lines by sheathed length, "
        "enveloped with an equal-share-per-line minimum.",
        "Edge-zone (zone 5) components, torsional load cases, and site-specific "
        "irregularities to be confirmed by the engineer of record.",
    ):
        body += _note(f"&bull; {lim}")

    footer = _sheet_footer(opts, project, sheet_num, total_sheets)
    return _wrap_sheet(header, body, footer, "member-sheet")


# ------------------------------------------------------------------ TIMBER BEAM SHEET
def _timber_beam_sheet(
    item: Dict[str, Any],
    raw: Dict[str, Any],
    opts: Dict[str, Any],
    project: Dict[str, Any],
    sheet_num: int,
    total_sheets: int,
) -> str:
    header = _sheet_header(opts, project)
    checks = {c["name"]: c for c in (item.get("checks") or [])}
    size = item.get("size") or raw.get("size") or ""
    plies = raw.get("plies") or 1
    system = raw.get("system") or item.get("system") or ""
    application = raw.get("application") or item.get("application") or ""

    body = '<div class="calc-title">TIMBER FLEXURAL MEMBER DESIGN</div>'
    body += f'<div class="calc-sub">{_nds_edition(project)} &middot; in accordance with the ASD method</div>'

    body += _heading("Member details", first=True)
    body += _row("Member type", _e(f"{system} {application}"))
    body += _row("Trial section", f"<b>{_e(size)}</b>")
    if plies and plies > 1:
        body += _row("Number of plies", f"<b>{plies}</b>")

    # Full derivation trail (available when the calc engine exported intermediates)
    inter = raw.get("intermediate") or {}
    geo = inter.get("geometry") or {}
    sect = inter.get("section") or {}
    mat = inter.get("material") or {}
    factors = inter.get("adjustment_factors") or {}
    adjusted = inter.get("adjusted_values") or {}
    loads = inter.get("loads") or {}
    combos = inter.get("combos") or []
    brg = inter.get("bearing") or {}
    defl = inter.get("deflection") or {}

    if geo:
        body += _heading("Geometry")
        body += _row("Clear span", f"L<sub>clear</sub> = <b>{_n(geo.get('L_clear_ft'), 2)}</b> ft")
        _b1 = brg.get("bearing_R1_in")
        _b2 = brg.get("bearing_R2_in")
        if _b1 is not None and _b2 is not None:
            span_expr = (
                f"L = L<sub>clear</sub> + &frac12;(l<sub>b1</sub> + l<sub>b2</sub>)/12 = "
                f"{_n(geo.get('L_clear_ft'), 2)} + &frac12;({_n(_b1, 2)}&Prime; + {_n(_b2, 2)}&Prime;)/12 "
                f"= <b>{_n(geo.get('L_span_ft'), 3)}</b> ft"
            )
        else:
            span_expr = f"L = <b>{_n(geo.get('L_span_ft'), 3)}</b> ft"
        body += _row_formula("Design span (c/c bearing)", span_expr)
        if geo.get("overhang_left_ft") or geo.get("overhang_right_ft"):
            body += _row(
                "Overhangs (left / right)",
                f"<b>{_n(geo.get('overhang_left_ft'), 2)}</b> / "
                f"<b>{_n(geo.get('overhang_right_ft'), 2)}</b> ft",
            )
        if geo.get("pitch_rise_per_12"):
            body += _row("Roof pitch", f"<b>{_n(geo.get('pitch_rise_per_12'), 1)}</b>:12")
        if geo.get("spacing_in"):
            body += _row("Spacing", f"s = <b>{_n(geo.get('spacing_in'), 1)}</b> in o.c.")
        body += _row("Tributary width", f"w<sub>trib</sub> = <b>{_n(geo.get('trib_width_ft'), 2)}</b> ft")

    if sect:
        body += _heading("Section properties" + (f" ({plies} plies)" if plies and plies > 1 else ""))
        body += _row("Breadth &times; depth", f"b &times; d = <b>{_n(sect.get('b_in'), 2)}</b> &times; <b>{_n(sect.get('d_in'), 3)}</b> in")
        body += _row_formula("Area", f"A = b &middot; d = <b>{_n(sect.get('A_in2'), 2)}</b> in&sup2;")
        body += _row_formula("Section modulus", f"S = b &middot; d&sup2;/6 = <b>{_n(sect.get('S_in3'), 2)}</b> in&sup3;")
        body += _row_formula("Moment of inertia", f"I = b &middot; d&sup3;/12 = <b>{_n(sect.get('I_in4'), 2)}</b> in&#8308;")

    if mat:
        body += _heading("Material — reference design values")
        body += _row("Species / grade", f"<b>{_e(mat.get('species'))} {_e(mat.get('grade'))}</b>")
        body += _row("Bending", f"F<sub>b</sub> = <b>{_n(mat.get('Fb_psi'), 0)}</b> psi")
        body += _row("Shear", f"F<sub>v</sub> = <b>{_n(mat.get('Fv_psi'), 0)}</b> psi")
        body += _row("Modulus of elasticity", f"E = <b>{_n(mat.get('E_psi'), 0)}</b> psi")
        body += _row(
            "Support compression &perp;",
            f"F<sub>c&perp;</sub> = <b>{_n(mat.get('Fc_perp_support_psi'), 0)}</b> psi ({_e(brg.get('support_species') or '-')})",
        )

    is_ewp = any(
        x in str(mat.get("species") or "")
        for x in ("LVL", "LSL", "PSL", "Glulam", "EWP")
    )

    if factors:
        if is_ewp:
            body += _heading("Adjustment factors — SCL / manufacturer values")
            body += _row("Load duration (governing bending combo)", f"C<sub>D</sub> = <b>{_n(factors.get('CD_moment'), 2)}</b>")
            body += _row("Volume / depth factor (bending)", f"C<sub>V</sub> = <b>{_n(factors.get('CF_fb'), 3)}</b>")
        else:
            body += _heading("Adjustment factors — NDS Table 4.3.1")
            body += _row("Load duration (governing bending combo)", f"C<sub>D</sub> = <b>{_n(factors.get('CD_moment'), 2)}</b>")
            body += _row("Size factor (bending / shear)", f"C<sub>F</sub> = <b>{_n(factors.get('CF_fb'), 3)}</b> / <b>{_n(factors.get('CF_fv'), 3)}</b>")
        body += _row("Repetitive member", f"C<sub>r</sub> = <b>{_n(factors.get('Cr'), 2)}</b>")
        body += _row(
            "Beam stability (continuous compression-edge bracing assumed)",
            f"C<sub>L</sub> = <b>{_n(factors.get('CL'), 2)}</b>",
        )
        if adjusted:
            body += _row_formula(
                "Adjusted bending",
                f"F<sub>b</sub>&prime; = F<sub>b</sub> &middot; C<sub>D</sub> &middot; C<sub>F</sub> &middot; C<sub>r</sub> &middot; C<sub>L</sub> = "
                f"{_n(mat.get('Fb_psi'), 0)} &middot; {_n(factors.get('CD_moment'), 2)} &middot; {_n(factors.get('CF_fb'), 3)} &middot; "
                f"{_n(factors.get('Cr'), 2)} &middot; {_n(factors.get('CL'), 2)} = <b>{_n(adjusted.get('Fb_prime_psi'), 0)}</b> psi",
                indent=True,
            )
            body += _row_formula(
                "Adjusted shear",
                f"F<sub>v</sub>&prime; = F<sub>v</sub> &middot; C<sub>D</sub> &middot; C<sub>F</sub> = "
                f"<b>{_n(adjusted.get('Fv_prime_psi'), 0)}</b> psi",
                indent=True,
            )
        body += _note(
            "Factors not shown are taken as unity: C<sub>M</sub> = C<sub>t</sub> = "
            "C<sub>i</sub> = C<sub>fu</sub> = 1.0 (dry service, &le;100&deg;F, un-incised, "
            "no flat-use). Member assumed un-notched with no field-drilled holes; "
            "verify against actual conditions."
        )

    if loads:
        body += _heading("Load buildup")
        area_parts = []
        for sym, key in (("D", "D_psf"), ("L", "L_psf"), ("L<sub>r</sub>", "Lr_psf"), ("S", "S_psf")):
            if loads.get(key):
                area_parts.append(f"{sym} = {_n(loads[key], 1)} psf")
        if area_parts:
            body += _row("Area loads", " &nbsp;&middot;&nbsp; ".join(area_parts))
        body += _row_formula(
            "Dead line load",
            f"w<sub>D</sub> = D &middot; w<sub>trib</sub> + w<sub>self</sub> = "
            f"{_n(loads.get('D_psf'), 1)} &middot; {_n(geo.get('trib_width_ft'), 2)} + "
            f"{_n(loads.get('self_weight_plf'), 2)} = <b>{_n(loads.get('w_D_plf'), 1)}</b> plf",
        )
        for sym, key in (("L", "w_L_plf"), ("L<sub>r</sub>", "w_Lr_plf"), ("S", "w_S_plf")):
            if loads.get(key):
                body += _row(f"{sym} line load", f"w<sub>{sym}</sub> = <b>{_n(loads[key], 1)}</b> plf")

    if combos:
        body += _coderef("ASD load combinations &mdash; ASCE 7-22 &sect;2.4.1 (IBC 2024 &sect;1605.1)")
        # Combos whose transient terms (L/Lr/S/W) are all zero collapse onto the
        # pure-dead load (e.g. a roof beam has no floor L). Show the distinct
        # combinations only, so the table doesn't list several identical rows.
        d_only = next((c.get("w_plf") for c in combos if c.get("name") == "1.0D"), None)
        shown, suppressed = [], 0
        for c in combos:
            if c.get("name") != "1.0D" and d_only is not None and abs((c.get("w_plf") or 0) - d_only) <= 0.05:
                suppressed += 1
                continue
            shown.append(c)
        rows = "".join(
            f'<tr><td>{_e(c.get("name"))}</td><td class="r">{_n(c.get("w_plf"), 1)}</td>'
            f'<td class="r">{_n(c.get("CD"), 2)}</td></tr>'
            for c in shown
        )
        body += f"""<table class="dt"><thead><tr>
<th>Combination</th><th class="r">w (plf)</th><th class="r">C<sub>D</sub></th>
</tr></thead><tbody>{rows}</tbody></table>"""
        if suppressed:
            body += (
                '<div class="note-line">'
                f"{suppressed} further combination(s) omitted &mdash; their L / L<sub>r</sub> / S / W "
                "terms are zero for this member, reducing them to 1.0D above."
                "</div>"
            )

    # Extract data from checks
    moment_chk = checks.get("Moment (Ft-lbs)", {})
    shear_chk = checks.get("Shear (lbs)", {})
    reaction_chk = checks.get("Member Reaction (lbs)", {})
    tl_defl_chk = checks.get("Total Load Defl (in)", {})
    ll_defl_chk = checks.get("Live Load Defl. (in)", {})

    M_actual = moment_chk.get("actual")
    M_allowed = moment_chk.get("allowed")
    M_util = moment_chk.get("utilization")
    M_combo = moment_chk.get("combo")

    V_actual = shear_chk.get("actual")
    V_allowed = shear_chk.get("allowed")
    V_util = shear_chk.get("utilization")

    R_actual = reaction_chk.get("actual")
    R_allowed = reaction_chk.get("allowed")
    R_util = reaction_chk.get("utilization")

    body += _heading("Loading")
    if M_combo:
        body += _row("Governing load combination", f"<b>{_e(M_combo)}</b>")
    body += _row(
        "Load duration factor C<sub>D</sub>", f"<b>{_n(moment_chk.get('ldf'), 2)}</b>"
    )

    # Bending
    body += _coderef("Bending &mdash; NDS &sect;3.3")
    if M_actual is not None:
        body += _row("Applied moment", f"M = <b>{_n(M_actual, 0)}</b> ft&middot;lb")
    if M_allowed is not None:
        body += _row(
            "Adjusted allowable moment",
            f"M' = F<sub>b</sub>&prime; &middot; S / 12 = <b>{_n(M_allowed, 0)}</b> ft&middot;lb",
        )
    if M_util is not None:
        u_val = float(M_util) / 100.0
        body += _row_formula(
            "Utilization",
            f"M / M' = {_n(M_actual, 0)} / {_n(M_allowed, 0)} = " f"<b>{u_val:.2f}</b>",
            indent=True,
        )
        status = "PASS" if float(M_util) <= 100 else "FAIL"
        body += _pass_line(
            status,
            (
                "Adjusted allowable bending stress exceeds applied stress"
                if status == "PASS"
                else "Applied bending stress exceeds allowable"
            ),
        )

    # Shear
    body += _coderef("Shear &mdash; NDS &sect;3.4")
    if V_actual is not None:
        body += _row(
            "Applied shear (at d from support)", f"V = <b>{_n(V_actual, 0)}</b> lbs"
        )
    if V_allowed is not None:
        body += _row(
            "Adjusted allowable shear",
            f"V' = F<sub>v</sub>&prime; &middot; A / 1.5 = <b>{_n(V_allowed, 0)}</b> lbs",
        )
    if V_util is not None:
        u_val = float(V_util) / 100.0
        body += _row_formula("Utilization", f"V / V' = <b>{u_val:.2f}</b>", indent=True)
        status = "PASS" if float(V_util) <= 100 else "FAIL"
        body += _pass_line(
            status,
            (
                "Adjusted allowable shear exceeds applied shear"
                if status == "PASS"
                else "Applied shear exceeds allowable"
            ),
        )

    # Bearing / Reaction
    body += _coderef("Bearing &mdash; NDS &sect;3.10")
    if R_actual is not None:
        body += _row("Maximum support reaction", f"R = <b>{_n(R_actual, 0)}</b> lbs")
    _bl = brg.get("bearing_R1_in")
    if _bl is not None and sect.get("b_in"):
        body += _row(
            "Bearing length / area",
            f"l<sub>b</sub> = <b>{_n(_bl, 2)}</b> in &nbsp;&middot;&nbsp; "
            f"A<sub>brg</sub> = b &middot; l<sub>b</sub> = {_n(sect.get('b_in'), 2)} &middot; {_n(_bl, 2)} "
            f"= <b>{_n(sect.get('b_in') * _bl, 2)}</b> in&sup2;",
        )
    if R_allowed is not None:
        body += _row(
            "Allowable bearing capacity",
            f"R<sub>allow</sub> = F<sub>c&perp;</sub>&prime; &middot; A<sub>brg</sub> = "
            f"<b>{_n(R_allowed, 0)}</b> lbs",
        )
    if R_util is not None:
        u_val = float(R_util) / 100.0
        body += _row_formula(
            "Utilization", f"R / R<sub>allow</sub> = <b>{u_val:.2f}</b>", indent=True
        )
        status = "PASS" if float(R_util) <= 100 else "FAIL"
        body += _pass_line(
            status,
            (
                "Bearing capacity adequate"
                if status == "PASS"
                else "Bearing capacity exceeded"
            ),
        )

    # Deflection
    body += _coderef("Deflection &mdash; IBC Table 1604.3")
    ll_denom = defl.get("LL_denom") or 360
    tl_denom = defl.get("TL_denom") or 240
    if defl:
        # The engine divides the flat-beam deflection by cos²θ for pitched
        # members (vertical-component convention) — show that factor so the
        # printed value follows from the printed formula.
        slope_term = ""
        if geo.get("pitch_rise_per_12"):
            slope_term = " / cos&sup2;&theta;"
        # For SCL/EWP the engine adds a shear-deflection term (Δv = 1.2wL²/(8GA),
        # G = E'/16) — show it so the printed value follows from the formula.
        shear_term = (
            " + 1.2 &middot; w &middot; L&sup2; / (8 &middot; G &middot; A), &nbsp;G = E&prime;/16"
            if is_ewp else ""
        )
        body += _row_formula(
            "Deflection model",
            "&Delta; = 5 &middot; w &middot; L&#8308; / (384 &middot; E&prime; &middot; I)"
            f"{shear_term}{slope_term}"
            " &nbsp;(&minus; end-moment relief at overhangs)",
        )
    if ll_defl_chk.get("actual") is not None:
        ll_a = float(ll_defl_chk["actual"])
        ll_al = float(ll_defl_chk["allowed"])
        body += _row(
            "Live-load deflection",
            f"&Delta;<sub>LL</sub> = <b>{_n(ll_a, 3)}</b> in "
            f"&le; L/{_n(ll_denom, 0)} = <b>{_n(ll_al, 3)}</b> in",
        )
    if tl_defl_chk.get("actual") is not None:
        tl_a = float(tl_defl_chk["actual"])
        tl_al = float(tl_defl_chk["allowed"])
        body += _row(
            "Total-load deflection",
            f"&Delta;<sub>TL</sub> = <b>{_n(tl_a, 3)}</b> in "
            f"&le; L/{_n(tl_denom, 0)} = <b>{_n(tl_al, 3)}</b> in",
        )
    tl_status = "PASS" if (tl_defl_chk.get("utilization") or 0) <= 100 else "FAIL"
    ll_status = "PASS" if (ll_defl_chk.get("utilization") or 0) <= 100 else "FAIL"
    defl_ok = tl_status == "PASS" and ll_status == "PASS"
    body += _pass_line(
        "PASS" if defl_ok else "FAIL",
        (
            "Deflections within code limits"
            if defl_ok
            else "Deflection exceeds code limits"
        ),
    )

    footer = _sheet_footer(opts, project, sheet_num, total_sheets)
    return _wrap_sheet(header, body, footer, "member-sheet")


# ------------------------------------------------------------------ STUD WALL SHEET
def _stud_wall_sheet(
    item: Dict[str, Any],
    raw: Dict[str, Any],
    opts: Dict[str, Any],
    project: Dict[str, Any],
    sheet_num: int,
    total_sheets: int,
) -> str:
    header = _sheet_header(opts, project)
    checks = {c["name"]: c for c in (item.get("checks") or [])}

    body = '<div class="calc-title">WALL STUD DESIGN</div>'
    body += f'<div class="calc-sub">{_nds_edition(project)} &middot; combined axial + lateral wind (ASD)</div>'

    body += _heading("Member details", first=True)
    body += _row("Member type", _e(item.get("application") or "Stud wall"))
    body += _row("Trial section", f"<b>{_e(item.get('size'))}</b>")

    comp_chk = checks.get("Axial Compression (lbs)", {})
    brg_chk = checks.get("Plate Bearing (lbs)", {})
    mom_chk = checks.get("Lateral Moment (ft-lbs)", {})
    shear_chk = checks.get("Lateral Shear (lbs)", {})
    comb_chk = checks.get("Bending/Compression", {})
    defl_chk = checks.get("Total Deflection (in)", {})
    slend_chk = checks.get("Slenderness", {})

    if slend_chk:
        body += _heading("Slenderness check")
        body += _row(
            "Slenderness ratio",
            f"l<sub>e</sub>/d = <b>{_n(slend_chk.get('actual'), 1)}</b> "
            f"&le; {_n(slend_chk.get('allowed'), 0)}",
        )
        status = "PASS" if (slend_chk.get("utilization") or 0) <= 100 else "FAIL"
        body += _pass_line(status, "Slenderness within limits")

    # Compression
    body += _coderef("Axial compression &mdash; NDS &sect;3.6")
    if comp_chk:
        body += _row(
            "Applied axial load", f"P = <b>{_n(comp_chk.get('actual'), 0)}</b> lbs"
        )
        body += _row(
            "Adjusted allowable compressive capacity",
            f"P' = F<sub>c</sub>&prime; &middot; C<sub>P</sub> &middot; A = "
            f"<b>{_n(comp_chk.get('allowed'), 0)}</b> lbs",
        )
        if comp_chk.get("utilization") is not None:
            u = float(comp_chk["utilization"]) / 100.0
            body += _row_formula("Utilization", f"P / P' = <b>{u:.2f}</b>", indent=True)
            status = "PASS" if float(comp_chk["utilization"]) <= 100 else "FAIL"
            body += _pass_line(
                status,
                (
                    "Axial capacity adequate"
                    if status == "PASS"
                    else "Axial capacity exceeded"
                ),
            )

    # Bending
    body += _coderef("Lateral bending &mdash; NDS &sect;3.3")
    if mom_chk:
        body += _row(
            "Applied moment (wind + eccentricity)",
            f"M = <b>{_n(mom_chk.get('actual'), 0)}</b> ft&middot;lb",
        )
        body += _row(
            "Adjusted allowable moment",
            f"M' = F<sub>b</sub>&prime; &middot; S / 12 = "
            f"<b>{_n(mom_chk.get('allowed'), 0)}</b> ft&middot;lb",
        )

    # Combined interaction
    body += _coderef("Combined bending &amp; compression &mdash; NDS Eq. 3.9-3")
    if comb_chk:
        body += _row_formula(
            "Interaction equation",
            "(f<sub>c</sub>/F<sub>c</sub>&prime;)&sup2; + "
            "f<sub>b</sub>/[F<sub>b</sub>&prime;(1 &minus; f<sub>c</sub>/F<sub>cE</sub>)]",
        )
        u = comb_chk.get("utilization")
        if u is not None:
            body += _row_formula(
                "Utilization", f"<b>{float(u)/100:.3f}</b> &le; 1.0", indent=True
            )
            status = "PASS" if float(u) <= 100 else "FAIL"
            body += _pass_line(
                status,
                (
                    "Combined stress ratio within limits"
                    if status == "PASS"
                    else "Combined stress ratio exceeds 1.0"
                ),
            )

    # Deflection
    body += _coderef("Deflection &mdash; IBC Table 1604.3")
    if defl_chk:
        body += _row(
            "Lateral deflection (service wind)",
            f"&Delta; = <b>{_n(defl_chk.get('actual'), 3)}</b> in "
            f"&le; L/360 = <b>{_n(defl_chk.get('allowed'), 3)}</b> in",
        )
        status = "PASS" if (defl_chk.get("utilization") or 0) <= 100 else "FAIL"
        body += _pass_line(
            status,
            (
                "Deflection within limits"
                if status == "PASS"
                else "Deflection exceeds code limit"
            ),
        )

    footer = _sheet_footer(opts, project, sheet_num, total_sheets)
    return _wrap_sheet(header, body, footer, "member-sheet")


# ------------------------------------------------------------------ MASONRY SHEET
def _masonry_sheet(
    item: Dict[str, Any],
    raw: Dict[str, Any],
    opts: Dict[str, Any],
    project: Dict[str, Any],
    sheet_num: int,
    total_sheets: int,
) -> str:
    header = _sheet_header(opts, project)
    checks = {c["name"]: c for c in (item.get("checks") or [])}

    section = raw.get("section") or {}
    loads = raw.get("loads") or {}
    interaction = raw.get("interaction") or {}
    combo_utils = raw.get("combo_utilizations") or {}
    is_section_mode = "biaxial" in str(raw.get("method") or "").lower()

    body = '<div class="calc-title">MASONRY WALL PANEL DESIGN</div>'
    method_sub = (
        "TMS 402/602-16 &middot; strength design &middot; biaxial section (in-plane + out-of-plane)"
        if is_section_mode
        else "TMS 402/602-16 &middot; in accordance with the strength design method"
    )
    body += f'<div class="calc-sub">{method_sub}</div>'

    body += _heading("Masonry wall panel details", first=True)
    body += _note(
        "Reinforced single-wythe wall &mdash; pinned top and bottom for out-of-plane loads."
    )
    body += _row("Wall thickness", f"t = <b>{_n(section.get('t_in'), 3)}</b> in")

    # Check summary table (covers both panel and section modes)
    raw_results = raw.get("results") or {}
    summary_rows = ""
    for name, chk in raw_results.items():
        if not isinstance(chk, dict) or chk.get("Result") is None:
            continue
        u = chk.get("Result")
        scls = "res-pass" if (u or 0) <= 100 else "res-fail"
        summary_rows += (
            f"<tr><td>{_e(name)}</td>"
            f'<td class="r">{_n(chk.get("Actual"), 2)}</td>'
            f'<td class="r">{_n(chk.get("Allowed"), 2)}</td>'
            f'<td class="r {scls}">{_pct(u)}</td></tr>'
        )
    if summary_rows:
        body += _heading("Design check summary")
        body += f"""<table class="dt"><thead><tr>
<th>Check</th><th class="r">Demand</th><th class="r">Capacity</th><th class="r">Util.</th>
</tr></thead><tbody>{summary_rows}</tbody></table>"""

    # Section properties (keys differ between panel and section modes —
    # render only what this run produced; no dash-filled rows)
    body += _heading("Section properties (per ft)")
    body += _row(
        "Cross-sectional area",
        f"A = <b>{_n(section.get('A_in2_per_ft'), 1)}</b> in&sup2;/ft",
    )
    I_val = section.get("I_in4_per_ft") or section.get("I_oop_in4_per_ft")
    if I_val is not None:
        body += _row(
            "Moment of inertia",
            f"I = t&sup3; / 12 = <b>{_n(I_val, 1)}</b> in&#8308;/ft",
        )
    body += _row(
        "Section modulus",
        f"S = I / c = <b>{_n(section.get('S_in3_per_ft'), 1)}</b> in&sup3;/ft",
    )
    body += _row(
        "Radius of gyration",
        f"r = &radic;(I / A) = <b>{_n(section.get('r_in'), 3)}</b> in",
    )
    if section.get("slenderness_kh_r") is not None:
        body += _row(
            "Slenderness ratio",
            f"K&middot;h / r = <b>{_n(section.get('slenderness_kh_r'), 2)}</b> &lt; 99",
        )
    if section.get("Ix_in4") is not None:
        body += _row(
            "In-plane gross inertia",
            f"I<sub>x</sub> = <b>{_n(section.get('Ix_in4'), 0)}</b> in&#8308; "
            f"(r<sub>x</sub> = {_n(section.get('rx_in'), 1)} in)",
        )

    # Material properties
    body += _heading("Material properties")
    body += _row(
        "Compressive strength",
        f"f&prime;<sub>m</sub> = <b>{_n(section.get('Em_psi', 0) / 900 if section.get('Em_psi') else None, 0)}</b> psi",
    )
    body += _row(
        "Modulus of elasticity",
        f"E<sub>m</sub> = 900 &middot; f&prime;<sub>m</sub> = "
        f"<b>{_n(section.get('Em_psi'), 0)}</b> psi",
    )
    body += _row(
        "Modulus of rupture",
        f"f<sub>r</sub> = <b>{_n(section.get('fr_psi'), 0)}</b> psi",
    )
    if section.get("w_self_psf") is not None:
        body += _row(
            "Self-weight", f"w<sub>SW</sub> = <b>{_n(section.get('w_self_psf'), 2)}</b> psf"
        )

    # Reinforcement
    body += _heading("Reinforcement details")
    body += _row(
        "Reinforcement area (vertical)",
        f"A<sub>s</sub> = <b>{_n(section.get('As_in2_per_ft'), 3)}</b> in&sup2;/ft",
    )
    if section.get("Av_in2_per_ft") is not None:
        body += _row(
            "Horizontal (shear) reinforcement",
            f"A<sub>v</sub> = <b>{_n(section.get('Av_in2_per_ft'), 3)}</b> in&sup2;/ft",
        )
    body += _row("Effective depth", f"d = <b>{_n(section.get('d_in'), 3)}</b> in")
    if section.get("modular_ratio_n") is not None:
        body += _row(
            "Modular ratio",
            f"n = E<sub>s</sub> / E<sub>m</sub> = <b>{_n(section.get('modular_ratio_n'), 3)}</b>",
        )

    # Loads (panel mode derives these; section mode takes explicit forces)
    if loads:
        body += _heading("Lateral & vertical loads")
        body += _row("Wind load on panel", f"W = <b>{_n(loads.get('wind_psf'), 0)}</b> psf")
        body += _row(
            "Seismic OOP factor",
            f"F<sub>p</sub> = 0.4 &middot; S<sub>DS</sub> &middot; I<sub>e</sub> = "
            f"<b>{_n(loads.get('Fp'), 3)}</b>",
        )
        body += _row("Seismic OOP load", f"E = <b>{_n(loads.get('E_psf'), 1)}</b> psf")

    # Load combinations
    if combo_utils:
        body += _coderef("IBC 2021 cl.1605.2 &mdash; Load combinations (utilization)")
        for combo_name, u_val in combo_utils.items():
            is_gov = combo_name == raw.get("governing_combo")
            body += _row(
                f"Comb. &mdash; {_e(combo_name)}",
                f"(<b>{_n(u_val, 3)}</b>)" + (" &nbsp;governs" if is_gov else ""),
            )

    # Axial check (section mode uses the 'Out-of-plane' check names)
    flex_chk = checks.get("Flexure (M / phi*Mn)", {}) or checks.get("Out-of-plane Flexure (M / phi*Mn)", {})
    axial_chk = checks.get("Axial (P / phi*Pn)", {}) or checks.get("Out-of-plane Axial (P / phi*Pn)", {})
    stress_chk = checks.get("Axial Stress (P/A / 0.2f'm)", {}) or checks.get("Out-of-plane Axial Stress", {})
    shear_chk = checks.get("Out-of-plane Shear (V / phi*Vn)", {})
    rebar_chk = checks.get("Reinforcement (As / As,max)", {})

    # Section-mode checks report kips / kip-ft; panel mode reports lb / lb-in
    unit_p = "kip/ft" if is_section_mode else "lb/ft"
    unit_m = "kip&middot;ft/ft" if is_section_mode else "lb&middot;in/ft"
    nd_pm = 2 if is_section_mode else 0

    if axial_chk or stress_chk:
        body += _coderef("Axial strength &mdash; TMS 402 &sect;9.3.4.1.1")
    if axial_chk:
        body += _row(
            "Factored axial load at mid-height",
            f"P<sub>u</sub> = <b>{_n(axial_chk.get('actual'), nd_pm)}</b> {unit_p}",
        )
        body += _row(
            "Design axial strength",
            f"&phi;P<sub>n</sub> = <b>{_n(axial_chk.get('allowed'), nd_pm)}</b> {unit_p}",
        )
        u = axial_chk.get("utilization")
        if u is not None:
            body += _row_formula(
                "Utilization",
                f"P / (&phi;P<sub>n</sub>) = <b>{float(u)/100:.3f}</b>",
                indent=True,
            )
            body += _pass_line(
                axial_chk.get("status", "PASS"), "Axial capacity adequate"
            )
    if stress_chk:
        body += _row(
            "Factored axial stress check",
            f"P/A = <b>{_n(stress_chk.get('actual'), 1)}</b> psi "
            f"&le; 0.2f&prime;<sub>m</sub> = <b>{_n(stress_chk.get('allowed'), 0)}</b> psi",
        )
        body += _pass_line(
            stress_chk.get("status", "PASS"),
            "Allowable axial stress exceeds factored stress",
        )

    # Flexure check
    body += _coderef("Combined axial &amp; flexure &mdash; TMS 402 &sect;9.3.5")
    if section.get("Mcr_lbin_per_ft") is not None:
        body += _row(
            "Cracking moment",
            f"M<sub>cr</sub> = S &middot; f<sub>r</sub> = "
            f"<b>{_n(section.get('Mcr_lbin_per_ft'), 0)}</b> lb&middot;in/ft",
        )
    elif section.get("Mcr_lbft_per_ft") is not None:
        body += _row(
            "Cracking moment",
            f"M<sub>cr</sub> = S &middot; f<sub>r</sub> = "
            f"<b>{_n(section.get('Mcr_lbft_per_ft'), 1)}</b> lb&middot;ft/ft",
        )
    if interaction.get("M_design_lbin_per_ft") is not None:
        body += _row(
            "2nd-order moment (P-&Delta; iteration)",
            f"M<sub>u</sub> = <b>{_n(interaction.get('M_design_lbin_per_ft'), 0)}</b> lb&middot;in/ft",
        )
        body += _row(
            "Mid-height deflection",
            f"&delta;<sub>u</sub> = <b>{_n(interaction.get('delta_in'), 4)}</b> in",
        )
    if flex_chk:
        body += _row(
            "Design flexural strength (out-of-plane)",
            f"&phi;M<sub>n</sub> = <b>{_n(flex_chk.get('allowed'), nd_pm)}</b> {unit_m}",
        )
        u = flex_chk.get("utilization")
        if u is not None:
            body += _row_formula(
                "Utilization",
                f"M<sub>u</sub> / &phi;M<sub>n</sub> = <b>{float(u)/100:.3f}</b>",
                indent=True,
            )
            body += _pass_line(
                flex_chk.get("status", "PASS"), "Flexural capacity adequate"
            )

    # Interaction diagram values (panel mode only)
    if interaction.get("c_balance_in") is not None:
        body += _heading("Interaction diagram")
        body += _row(
            "Balance-point depth",
            f"c<sub>bal</sub> = <b>{_n(interaction.get('c_balance_in'), 3)}</b> in",
        )
        body += _row(
            "Axial force at balance",
            f"P<sub>bal</sub> = <b>{_n(interaction.get('P_balance_lbf_per_ft'), 0)}</b> lb/ft",
        )
        body += _row(
            "Moment at balance",
            f"M<sub>bal</sub> = <b>{_n(interaction.get('M_balance_lbin_per_ft'), 0)}</b> lb&middot;in/ft",
        )

    # Shear — full derivation when the calc exported it
    body += _coderef("Out-of-plane shear &mdash; TMS 402 &sect;9.3.4.1.2")
    shear_det = raw.get("shear") or raw.get("shear_oop") or {}
    if shear_det:
        if shear_det.get("w_lat_psf") is not None:
            body += _row_formula(
                "Shear demand (simple-span reaction)",
                f"V = w &middot; h / 2 = {_n(shear_det.get('w_lat_psf'), 1)} &middot; h / 2 = "
                f"<b>{_n(shear_det.get('V_lbf_per_ft'), 0)}</b> lb/ft "
                f"({_e(shear_det.get('combo') or '')})",
            )
        else:
            body += _row("Shear demand", f"V = <b>{_n(shear_det.get('V_lbf_per_ft'), 0)}</b> lb/ft")
        body += _row(
            "Net shear area",
            f"A<sub>nv</sub> = b &middot; d = <b>{_n(shear_det.get('Anv_in2_per_ft'), 1)}</b> in&sup2;/ft "
            f"&nbsp;&middot;&nbsp; M/(V&middot;d<sub>v</sub>) = <b>{_n(shear_det.get('MoverVdv'), 2)}</b>",
        )
        body += _row_formula(
            "Masonry shear strength (Eq. 9-20)",
            f"V<sub>nm</sub> = [4 &minus; 1.75&middot;M/(Vd<sub>v</sub>)] &middot; A<sub>nv</sub> &middot; "
            f"&radic;f&prime;<sub>m</sub> + 0.25P<sub>u</sub> = <b>{_n(shear_det.get('Vnm_lbf_per_ft'), 0)}</b> lb/ft",
        )
        body += _row(
            "Grouting factor / strength reduction",
            f"&gamma;<sub>g</sub> = <b>{_n(shear_det.get('gamma_g'), 2)}</b>, "
            f"&phi; = <b>{_n(shear_det.get('phi'), 2)}</b>",
        )
        body += _row(
            "Design shear strength",
            f"&phi;V<sub>n</sub> = <b>{_n(shear_det.get('phiVn_lbf_per_ft'), 0)}</b> lb/ft",
        )
    elif shear_chk:
        body += _row(
            "Applied shear", f"V = <b>{_n(shear_chk.get('actual'), 0)}</b> lb/ft"
        )
        body += _row(
            "Design shear strength",
            f"&phi;V<sub>n</sub> = <b>{_n(shear_chk.get('allowed'), 0)}</b> lb/ft",
        )
    if shear_chk:
        u = shear_chk.get("utilization")
        if u is not None:
            body += _row_formula(
                "Utilization",
                f"V / &phi;V<sub>n</sub> = <b>{float(u)/100:.3f}</b>",
                indent=True,
            )
            body += _pass_line(
                shear_chk.get("status", "PASS"), "Shear capacity adequate"
            )

    # Section-mode extras: in-plane shear, shear friction, P-delta magnifier
    shear_in = raw.get("shear_inplane") or {}
    if shear_in:
        body += _coderef("In-plane shear &mdash; TMS 402 &sect;9.3.4.1.2")
        body += _row(
            "Shear demand", f"V<sub>u</sub> = <b>{_n(shear_in.get('V_lbf'), 0)}</b> lbs"
        )
        body += _row(
            "Net shear area",
            f"A<sub>nv</sub> = <b>{_n(shear_in.get('Anv_in2'), 0)}</b> in&sup2;, "
            f"d<sub>v</sub> = <b>{_n(shear_in.get('dv_ft'), 2)}</b> ft, "
            f"M/(V&middot;d<sub>v</sub>) = <b>{_n(shear_in.get('MoverVdv'), 3)}</b>",
        )
        body += _row_formula(
            "Masonry component (Eq. 9-20)",
            f"V<sub>nm</sub> = <b>{_n(shear_in.get('Vnm_lbf'), 0)}</b> lbs",
        )
        body += _row_formula(
            "Reinforcement component (Eq. 9-21)",
            f"V<sub>ns</sub> = 0.5 &middot; (A<sub>v</sub>/s) &middot; f<sub>y</sub> &middot; d<sub>v</sub> = "
            f"<b>{_n(shear_in.get('Vns_lbf'), 0)}</b> lbs "
            f"(A<sub>v</sub> = {_n(shear_in.get('Av_in2_per_ft'), 3)} in&sup2;/ft)",
        )
        body += _row(
            "Upper-bound cap (Eqs. 9-18/9-19)",
            f"V<sub>n</sub> &le; <b>{_n(shear_in.get('cap_coeff'), 2)}</b> &middot; A<sub>nv</sub> &middot; "
            f"&radic;f&prime;<sub>m</sub> &nbsp;&middot;&nbsp; &gamma;<sub>g</sub> = "
            f"<b>{_n(shear_in.get('gamma_g'), 2)}</b>",
        )
        body += _row(
            "Design shear strength",
            f"&phi;V<sub>n</sub> = <b>{_n(shear_in.get('phiVn_lbf'), 0)}</b> lbs",
        )
        inshear_chk = checks.get("In-plane Shear (V / phi*Vn)", {})
        if inshear_chk.get("utilization") is not None:
            body += _pass_line(
                inshear_chk.get("status", "PASS"), "In-plane shear capacity adequate"
            )

    friction = raw.get("shear_friction") or {}
    if friction and shear_in:
        body += _coderef("Shear friction &mdash; TMS 402 &sect;9.3.6.5")
        body += _row_formula(
            "Reinforced interface",
            f"V<sub>nf1</sub> = &mu; &middot; (A<sub>sp</sub>&middot;f<sub>y</sub> + P<sub>u</sub>) = "
            f"<b>{_n(friction.get('Vnf1_lbf'), 0)}</b> lbs (&mu; = {_n(friction.get('mu'), 2)})",
        )
        body += _row_formula(
            "Compression-zone limit",
            f"V<sub>nf2</sub> = 0.42 &middot; f&prime;<sub>m</sub> &middot; A<sub>nc</sub> = "
            f"<b>{_n(friction.get('Vnf2_lbf'), 0)}</b> lbs",
        )
        body += _row(
            "Design friction strength",
            f"&phi;V<sub>nf</sub> = <b>{_n(friction.get('phiVnf_lbf'), 0)}</b> lbs",
        )
        fric_chk = checks.get("In-plane Shear Friction (V / phi*Vnf)", {})
        if fric_chk.get("utilization") is not None:
            body += _pass_line(fric_chk.get("status", "PASS"), "Shear friction adequate")

    pdelta = raw.get("pdelta") or {}
    if pdelta:
        body += _coderef("Second-order effects &mdash; TMS 402 &sect;9.3.5.4 (moment magnifier)")
        if pdelta.get("Pe_lbf_per_ft"):
            body += _row_formula(
                "Euler load",
                f"P<sub>e</sub> = &pi;&sup2; &middot; E<sub>m</sub> &middot; I<sub>eff</sub> / h&sup2; = "
                f"<b>{_n(pdelta.get('Pe_lbf_per_ft'), 0)}</b> lb/ft "
                f"(I<sub>eff</sub> = {_n(pdelta.get('Ieff_in4_per_ft'), 1)} in&#8308;/ft)",
            )
        body += _row_formula(
            "Magnified out-of-plane moment",
            f"M<sub>u</sub> = &psi; &middot; M<sub>0</sub> = {_n(pdelta.get('magnification'), 3)} &middot; "
            f"{_n(pdelta.get('My_total_lbft_per_ft'), 1)} = "
            f"<b>{_n(pdelta.get('Mu_oop_lbft_per_ft'), 1)}</b> lb&middot;ft/ft",
        )

    # Reinforcement check
    if rebar_chk:
        body += _coderef("Maximum reinforcement &mdash; TMS 402 &sect;9.3.3.2")
        body += _row(
            "Provided reinforcement",
            f"A<sub>s</sub> = <b>{_n(rebar_chk.get('actual'), 3)}</b> in&sup2;/ft",
        )
        body += _row(
            "Maximum allowed",
            f"A<sub>s,max</sub> = <b>{_n(rebar_chk.get('allowed'), 3)}</b> in&sup2;/ft",
        )
        body += _pass_line(
            rebar_chk.get("status", "PASS"), "Reinforcement within limits"
        )

    footer = _sheet_footer(opts, project, sheet_num, total_sheets)
    return _wrap_sheet(header, body, footer, "member-sheet")


# ------------------------------------------------------------------ POST SHEET
def _post_sheet(
    item: Dict[str, Any],
    raw: Dict[str, Any],
    opts: Dict[str, Any],
    project: Dict[str, Any],
    sheet_num: int,
    total_sheets: int,
) -> str:
    header = _sheet_header(opts, project)
    checks = {c["name"]: c for c in (item.get("checks") or [])}

    body = '<div class="calc-title">TIMBER POST / COLUMN DESIGN</div>'
    body += f'<div class="calc-sub">{_nds_edition(project)} &middot; axial compression with optional lateral (ASD)</div>'

    body += _heading("Member details", first=True)
    body += _row("Member type", _e(item.get("application") or "Post"))
    body += _row("Trial section", f"<b>{_e(item.get('size'))}</b>")

    slend_chk = checks.get("Slenderness", {})
    comp_chk = checks.get("Axial Compression (lbs)", {})
    brg_chk = checks.get("Plate Bearing (lbs)", {})
    interact_chk = checks.get("Bending/Compression", {})

    if slend_chk:
        body += _heading("Slenderness")
        body += _row(
            "Slenderness ratio",
            f"l<sub>e</sub>/d = <b>{_n(slend_chk.get('actual'), 1)}</b> "
            f"&le; {_n(slend_chk.get('allowed'), 0)}",
        )
        body += _pass_line(
            "PASS" if (slend_chk.get("utilization") or 0) <= 100 else "FAIL",
            "Slenderness within limits",
        )

    body += _coderef("Axial compression &mdash; NDS &sect;3.6 / 3.7")
    if comp_chk:
        body += _row(
            "Applied axial load", f"P = <b>{_n(comp_chk.get('actual'), 0)}</b> lbs"
        )
        body += _row(
            "Adjusted compressive capacity",
            f"P' = F<sub>c</sub>&prime; &middot; C<sub>P</sub> &middot; A = "
            f"<b>{_n(comp_chk.get('allowed'), 0)}</b> lbs",
        )
        u = comp_chk.get("utilization")
        if u is not None:
            body += _row_formula(
                "Utilization", f"P / P' = <b>{float(u)/100:.2f}</b>", indent=True
            )
            body += _pass_line(
                comp_chk.get("status", "PASS"), "Compression capacity adequate"
            )

    if brg_chk:
        body += _coderef("Plate bearing &mdash; NDS &sect;3.10")
        body += _row("Applied load", f"P = <b>{_n(brg_chk.get('actual'), 0)}</b> lbs")
        body += _row(
            "Allowable bearing",
            f"R<sub>allow</sub> = <b>{_n(brg_chk.get('allowed'), 0)}</b> lbs",
        )
        u = brg_chk.get("utilization")
        if u is not None:
            body += _row_formula(
                "Utilization", f"<b>{float(u)/100:.2f}</b>", indent=True
            )
            body += _pass_line(brg_chk.get("status", "PASS"), "Bearing adequate")

    if interact_chk:
        body += _coderef("Combined bending &amp; compression &mdash; NDS Eq. 3.9-3")
        u = interact_chk.get("utilization")
        if u is not None:
            body += _row_formula(
                "Interaction ratio", f"<b>{float(u)/100:.3f}</b> &le; 1.0", indent=True
            )
            body += _pass_line(
                interact_chk.get("status", "PASS"), "Combined stress within limits"
            )

    footer = _sheet_footer(opts, project, sheet_num, total_sheets)
    return _wrap_sheet(header, body, footer, "member-sheet")


# ------------------------------------------------------------------ FOOTING SHEET
def _footing_sheet(
    item: Dict[str, Any],
    raw: Dict[str, Any],
    opts: Dict[str, Any],
    project: Dict[str, Any],
    sheet_num: int,
    total_sheets: int,
) -> str:
    header = _sheet_header(opts, project)
    checks = {c["name"]: c for c in (item.get("checks") or [])}

    body = '<div class="calc-title">CONCRETE FOOTING DESIGN</div>'
    body += '<div class="calc-sub">ACI 318-19 &middot; bearing, one-way shear, punching shear, flexure, minimum reinforcement</div>'

    details = raw.get("details") or {}

    body += _heading("Footing details", first=True)
    body += _row("Footing type", _e(item.get("application") or "Footing"))
    body += _row("Size", f"<b>{_e(item.get('size'))}</b>")
    if "continuous" in str(item.get("application") or "").lower():
        body += _note(
            "Strip footing &mdash; all loads, capacities, and reinforcement "
            "are per lineal foot of wall."
        )
    if details:
        body += _row(
            "Reinforcement",
            f"<b>{_e(details.get('rebar_size'))} @ {_n(details.get('rebar_spacing_in'), 1)}</b> in o.c. "
            f"(A<sub>s</sub> = {_n(details.get('As_prov_in2'), 3)} in&sup2;/ft, "
            f"d = {_n(details.get('d_in'), 2)} in, cover = {_n(details.get('cover_in'), 1)} in)",
        )
        body += _row(
            "Materials",
            f"f&prime;<sub>c</sub> = <b>{_n(details.get('fc_psi'), 0)}</b> psi, "
            f"f<sub>y</sub> = <b>{_n(details.get('fy_psi'), 0)}</b> psi",
        )
        body += _row(
            "Factored net pressure",
            f"q<sub>u</sub> = <b>{_n(details.get('qu_psf'), 1)}</b> psf "
            f"({_e(details.get('governing_combo') or '')})",
        )

    soil_chk = checks.get("Soil Bearing (psf)", {})
    ow_chk = checks.get("One-way Shear (lbs)", {})
    punch_chk = checks.get("Punching Shear (lbs)", {})
    flex_chk = checks.get("Flexural Capacity (in-lbs)", {})
    minreinf_chk = checks.get("Min Reinforcement (in2)", {})
    long_chk = checks.get("Longitudinal Reinforcement (in2)", {})

    if soil_chk:
        body += _coderef("Soil bearing &mdash; ASD")
        body += _row(
            "Applied soil pressure",
            f"q = (D + L + S + W<sub>self</sub>) / A = <b>{_n(soil_chk.get('actual'), 1)}</b> psf",
        )
        body += _row(
            "Allowable bearing pressure",
            f"q<sub>allow</sub> = <b>{_n(soil_chk.get('allowed'), 0)}</b> psf",
        )
        u = soil_chk.get("utilization")
        if u is not None:
            body += _row_formula(
                "Utilization",
                f"q / q<sub>allow</sub> = <b>{float(u)/100:.2f}</b>",
                indent=True,
            )
            body += _pass_line(soil_chk.get("status", "PASS"), "Soil bearing adequate")

    if ow_chk:
        body += _coderef("One-way shear &mdash; ACI 318 &sect;22.5")
        if details and float(ow_chk.get("actual") or 0) == 0 and details.get("shear_crit_in") == 0:
            body += _note(
                f"Critical section at d = {_n(details.get('d_in'), 2)} in from the support face "
                f"falls beyond the {_n(details.get('projection_in'), 1)} in footing projection "
                "&mdash; one-way shear does not govern."
            )
        body += _row(
            "Factored shear",
            f"V<sub>u</sub> = <b>{_n(ow_chk.get('actual'), 0)}</b> lbs",
        )
        body += _row(
            "Design shear capacity",
            f"&phi;V<sub>c</sub> = &phi; &middot; 2&radic;f&prime;<sub>c</sub> &middot; b &middot; d = "
            f"<b>{_n(ow_chk.get('allowed'), 0)}</b> lbs",
        )
        u = ow_chk.get("utilization")
        if u is not None:
            body += _row_formula(
                "Utilization",
                f"V<sub>u</sub> / &phi;V<sub>c</sub> = <b>{float(u)/100:.2f}</b>",
                indent=True,
            )
            body += _pass_line(ow_chk.get("status", "PASS"), "One-way shear adequate")

    if punch_chk:
        body += _coderef("Two-way (punching) shear &mdash; ACI 318 &sect;22.6")
        body += _row(
            "Factored shear",
            f"V<sub>u</sub> = <b>{_n(punch_chk.get('actual'), 0)}</b> lbs",
        )
        body += _row(
            "Design punching capacity",
            f"&phi;V<sub>c</sub> = &phi; &middot; 4&radic;f&prime;<sub>c</sub> &middot; b<sub>o</sub> &middot; d = "
            f"<b>{_n(punch_chk.get('allowed'), 0)}</b> lbs",
        )
        u = punch_chk.get("utilization")
        if u is not None:
            body += _row_formula(
                "Utilization", f"<b>{float(u)/100:.2f}</b>", indent=True
            )
            body += _pass_line(
                punch_chk.get("status", "PASS"), "Punching shear adequate"
            )

    if flex_chk:
        body += _coderef("Flexure &mdash; ACI 318 &sect;22.2")
        body += _row(
            "Factored moment",
            f"M<sub>u</sub> = <b>{_n(flex_chk.get('actual'), 0)}</b> in&middot;lbs",
        )
        body += _row(
            "Design flexural capacity",
            f"&phi;M<sub>n</sub> = &phi; &middot; A<sub>s</sub> &middot; f<sub>y</sub>(d &minus; a/2) = "
            f"<b>{_n(flex_chk.get('allowed'), 0)}</b> in&middot;lbs",
        )
        u = flex_chk.get("utilization")
        if u is not None:
            body += _row_formula(
                "Utilization", f"<b>{float(u)/100:.2f}</b>", indent=True
            )
            body += _pass_line(
                flex_chk.get("status", "PASS"), "Flexural capacity adequate"
            )

    if minreinf_chk:
        body += _coderef("Minimum (temperature &amp; shrinkage) reinforcement &mdash; ACI 318 &sect;24.4.3.2")
        h_in = details.get("thickness_in")
        body += _row_formula(
            "Required minimum",
            "A<sub>s,min</sub> = 0.0018 &middot; b &middot; h"
            + (f" = 0.0018 &middot; 12 &middot; {_n(h_in, 1)}" if h_in else "")
            + f" = <b>{_n(minreinf_chk.get('allowed'), 3)}</b> in&sup2;/ft",
        )
        body += _row(
            "Provided",
            f"A<sub>s</sub> = <b>{_n(minreinf_chk.get('actual'), 3)}</b> in&sup2;/ft",
        )
        u = minreinf_chk.get("utilization")
        if u is not None:
            body += _row_formula(
                "Utilization",
                f"A<sub>s,min</sub> / A<sub>s</sub> = <b>{float(u)/100:.2f}</b>",
                indent=True,
            )
            body += _pass_line(
                minreinf_chk.get("status", "PASS"),
                (
                    "Minimum reinforcement satisfied"
                    if minreinf_chk.get("status") in ("PASS", "OK")
                    else "Provided steel is below the code minimum &mdash; add bars or reduce spacing"
                ),
            )

    if long_chk:
        body += _coderef("Longitudinal reinforcement &mdash; T&amp;S on gross section")
        b_full = details.get("width_in")
        h_in = details.get("thickness_in")
        body += _row_formula(
            "Required minimum",
            "A<sub>s,min</sub> = 0.0018 &middot; b &middot; h"
            + (f" = 0.0018 &middot; {_n(b_full, 0)} &middot; {_n(h_in, 0)}" if b_full and h_in else "")
            + f" = <b>{_n(long_chk.get('allowed'), 3)}</b> in&sup2;",
        )
        n_long = details.get("n_long_bars")
        long_bar = details.get("long_bar_size")
        body += _row(
            "Provided (continuous, along wall)",
            (f"({_n(n_long, 0)}) {_e(long_bar)} = " if n_long and long_bar else "")
            + f"A<sub>s</sub> = <b>{_n(long_chk.get('actual'), 3)}</b> in&sup2;",
        )
        body += _pass_line(
            long_chk.get("status", "PASS"),
            (
                "Longitudinal reinforcement satisfied"
                if long_chk.get("status") in ("PASS", "OK")
                else "Add or upsize the continuous longitudinal bars"
            ),
        )
        body += _note(
            "Braced wall lines in SDC D<sub>0</sub>&ndash;D<sub>2</sub> additionally require "
            "one #4 top and one #4 bottom continuous bar per IRC R403.1.3."
        )

    footer = _sheet_footer(opts, project, sheet_num, total_sheets)
    return _wrap_sheet(header, body, footer, "member-sheet")


# ------------------------------------------------------------------ PAD FOOTING SHEET
def _pad_footing_sheet(
    item: Dict[str, Any],
    raw: Dict[str, Any],
    opts: Dict[str, Any],
    project: Dict[str, Any],
    sheet_num: int,
    total_sheets: int,
) -> str:
    header = _sheet_header(opts, project)

    soil = raw.get("soil_bearing") or {}
    ows = raw.get("one_way_shear") or {}
    punch = raw.get("punching_shear") or {}
    flex1 = raw.get("flexure_B1") or {}
    flex2 = raw.get("flexure_B2") or {}
    reinf1 = raw.get("reinforcement_B1") or {}
    reinf2 = raw.get("reinforcement_B2") or {}
    devel = raw.get("development_length") or {}
    thick = raw.get("thickness") or {}
    summ = raw.get("summary") or {}

    body = '<div class="calc-title">ISOLATED PAD FOOTING DESIGN</div>'
    body += '<div class="calc-sub">ACI 318-19 &middot; soil bearing, one-way &amp; punching shear, biaxial flexure, reinforcement &amp; development</div>'

    body += _heading("Footing details", first=True)
    body += _row("Label", f"<b>{_e(item.get('label'))}</b>")
    body += _row("Size", f"<b>{_e(item.get('size') or '-')}</b>")
    if thick:
        body += _row(
            "Thickness / effective depth",
            f"H = <b>{_n(thick.get('H_provided_in'), 1)}</b> in "
            f"(required {_n(thick.get('H_required_in'), 1)} in), "
            f"d = <b>{_n(thick.get('d_actual_in'), 2)}</b> in",
        )
    if reinf1:
        body += _row(
            "Reinforcement (B1 / B2 directions)",
            f"<b>{_e(reinf1.get('bar'))}</b>: {_n(reinf1.get('n_bars'), 0)} bars @ "
            f"{_n(reinf1.get('spacing_calc_in'), 1)} in &nbsp;/&nbsp; "
            f"<b>{_e(reinf2.get('bar'))}</b>: {_n(reinf2.get('n_bars'), 0)} bars @ "
            f"{_n(reinf2.get('spacing_calc_in'), 1)} in",
        )
    if summ.get("governing_combo"):
        body += _row("Governing LRFD combination", f"<b>{_e(summ.get('governing_combo'))}</b>")

    # --- Soil bearing ---
    body += _coderef("Soil bearing &mdash; ASD service loads")
    body += _row("Service load (D + L + S + self + surcharge)",
                 f"P = <b>{_n(soil.get('P_service_lb'), 0)}</b> lbs")
    body += _row_formula(
        "Required area",
        f"A<sub>req</sub> = P / q<sub>allow</sub> = {_n(soil.get('P_service_lb'), 0)} / "
        f"{_n(soil.get('q_allow_psf'), 0)} = <b>{_n(soil.get('area_req_ft2'), 2)}</b> ft&sup2; "
        f"&le; provided <b>{_n(soil.get('area_selected_ft2'), 2)}</b> ft&sup2;",
    )
    ecc = soil.get("eccentricity_ft") or 0
    if ecc:
        body += _row(
            "Eccentricity / pressure distribution",
            f"e = <b>{_n(ecc, 3)}</b> ft &rarr; q<sub>max</sub> = "
            f"<b>{_n(soil.get('q_max_psf'), 1)}</b>, q<sub>min</sub> = "
            f"<b>{_n(soil.get('q_min_psf'), 1)}</b> psf"
            + (" <b>(partial bearing)</b>" if soil.get("partial_bearing") else ""),
        )
    else:
        body += _row("Peak soil pressure", f"q<sub>max</sub> = <b>{_n(soil.get('q_max_psf'), 1)}</b> psf")
    body += _row_formula(
        "Utilization",
        f"q<sub>max</sub> / q<sub>allow</sub> = <b>{(soil.get('utilization') or 0)/100:.2f}</b>",
        indent=True,
    )
    body += _pass_line(soil.get("status", "PASS"), "Soil bearing adequate")

    # --- One-way shear ---
    body += _coderef("One-way shear &mdash; ACI 318 &sect;22.5 (critical section at d from column face)")
    body += _row("Factored shear", f"V<sub>u</sub> = <b>{_n(ows.get('Vu_lb'), 0)}</b> lbs "
                                   f"({_e(ows.get('combo') or '')})")
    body += _row_formula(
        "Design capacity",
        f"&phi;V<sub>c</sub> = &phi; &middot; 2&radic;f&prime;<sub>c</sub> &middot; b &middot; d = "
        f"<b>{_n(ows.get('phi_Vc_lb'), 0)}</b> lbs "
        f"(d required for shear alone: {_n(ows.get('d_req_in'), 2)} in)",
    )
    body += _row_formula(
        "Utilization", f"<b>{(ows.get('utilization') or 0)/100:.2f}</b>", indent=True
    )
    body += _pass_line(ows.get("status", "PASS"), "One-way shear adequate")

    # --- Punching shear ---
    body += _coderef("Two-way (punching) shear &mdash; ACI 318 &sect;22.6 (perimeter at d/2)")
    body += _row(
        "Critical perimeter",
        f"b<sub>o</sub> = <b>{_n(punch.get('bo_in'), 1)}</b> in, "
        f"&beta;<sub>c</sub> = <b>{_n(punch.get('beta_c'), 2)}</b>",
    )
    body += _row_formula(
        "Capacity candidates (Table 22.6.5.2)",
        f"4&radic;f&prime;<sub>c</sub>&middot;b<sub>o</sub>d = {_n(punch.get('Vc1'), 0)} &nbsp;&middot;&nbsp; "
        f"(2+4/&beta;<sub>c</sub>)&radic;f&prime;<sub>c</sub>&middot;b<sub>o</sub>d = {_n(punch.get('Vc2'), 0)} &nbsp;&middot;&nbsp; "
        f"(2+&alpha;<sub>s</sub>d/b<sub>o</sub>)&radic;f&prime;<sub>c</sub>&middot;b<sub>o</sub>d = {_n(punch.get('Vc3'), 0)} lbs",
    )
    body += _row(
        "Factored shear vs design capacity",
        f"V<sub>u</sub> = <b>{_n(punch.get('Vu_lb'), 0)}</b> lbs &le; "
        f"&phi;V<sub>c</sub> = &phi; &middot; min(V<sub>c1..3</sub>) = <b>{_n(punch.get('phi_Vc_lb'), 0)}</b> lbs",
    )
    body += _row_formula(
        "Utilization", f"<b>{(punch.get('utilization') or 0)/100:.2f}</b>", indent=True
    )
    body += _pass_line(punch.get("status", "PASS"), "Punching shear adequate")

    # --- Flexure, both directions ---
    body += _coderef("Flexure &mdash; ACI 318 &sect;22.2 / minimums &sect;7.6.1 &amp; &sect;9.6.1.3")
    flex_rows = ""
    for label, fx in (("B1", flex1), ("B2", flex2)):
        if not fx:
            continue
        flex_rows += (
            f"<tr><td>{label}</td>"
            f'<td class="r">{_n(fx.get("L_cant_ft"), 2)}</td>'
            f'<td class="r">{_n(fx.get("Mu_ftlb"), 0)}</td>'
            f'<td class="r">{_n(fx.get("As_calc_in2"), 3)}</td>'
            f'<td class="r">{_n(fx.get("As_min_TS_in2"), 3)}</td>'
            f'<td class="r">{_n(fx.get("As_min_beam_in2"), 3)}</td>'
            f'<td class="r"><b>{_n(fx.get("As_required_in2"), 3)}</b></td></tr>'
        )
    if flex_rows:
        body += f"""<table class="dt"><thead><tr>
<th>Dir.</th><th class="r">Cantilever (ft)</th><th class="r">M<sub>u</sub> (ft&middot;lb)</th>
<th class="r">A<sub>s,calc</sub> (in&sup2;)</th><th class="r">A<sub>s,min</sub> T&amp;S</th>
<th class="r">A<sub>s,min</sub> flexure</th><th class="r">A<sub>s,req</sub></th>
</tr></thead><tbody>{flex_rows}</tbody></table>"""
        body += _note(
            "A<sub>s,req</sub> is the governing of the strength requirement and both code minimums."
        )

    reinf_rows = ""
    for label, rf, fx in (("B1", reinf1, flex1), ("B2", reinf2, flex2)):
        if not rf:
            continue
        status = "PASS" if rf.get("ok", True) else "FAIL"
        reinf_rows += (
            f"<tr><td>{label}</td><td>{_e(rf.get('bar'))}</td>"
            f'<td class="r">{_n(rf.get("n_bars"), 0)}</td>'
            f'<td class="r">{_n(rf.get("spacing_calc_in"), 1)}</td>'
            f'<td class="r">{_n(rf.get("As_provided_in2"), 3)}</td>'
            f'<td class="r">{_n(fx.get("As_required_in2"), 3)}</td>'
            f'<td class="r {_status_class(status)}">{status}</td></tr>'
        )
    if reinf_rows:
        body += f"""<table class="dt"><thead><tr>
<th>Dir.</th><th>Bar</th><th class="r">Count</th><th class="r">Spacing (in)</th>
<th class="r">A<sub>s,prov</sub> (in&sup2;)</th><th class="r">A<sub>s,req</sub> (in&sup2;)</th><th class="r">Result</th>
</tr></thead><tbody>{reinf_rows}</tbody></table>"""
    flex_ok = all(r.get("ok", True) for r in (reinf1, reinf2) if r)
    body += _pass_line("PASS" if flex_ok else "FAIL",
                       "Flexural reinforcement adequate" if flex_ok
                       else "Provided reinforcement below requirement")

    # --- Development length ---
    if devel:
        body += _coderef("Bar development &mdash; ACI 318 &sect;25.4.2")
        body += _row(
            "Required straight development",
            f"l<sub>d</sub> = <b>{_n(devel.get('ld_req_in'), 1)}</b> in &le; available "
            f"<b>{_n(devel.get('available_B1_in'), 1)}</b> / "
            f"<b>{_n(devel.get('available_B2_in'), 1)}</b> in (B1 / B2)",
        )
        d_status = devel.get("status", "PASS")
        body += _pass_line(
            d_status,
            "Straight bar development adequate" if d_status in ("PASS", "OK")
            else "Straight development insufficient &mdash; use standard hooks (&sect;25.4.3) "
                 "or increase footing plan size",
        )

    # --- Thickness ---
    if thick:
        body += _coderef("Thickness sizing")
        body += _row(
            "Provided vs required",
            f"H = <b>{_n(thick.get('H_provided_in'), 1)}</b> in &ge; "
            f"H<sub>req</sub> = <b>{_n(thick.get('H_required_in'), 1)}</b> in",
        )
        body += _pass_line(thick.get("status", "PASS"), "Thickness adequate")

    overall = summ.get("overall", "PASS")
    body += _note(f"<b>Overall: {_e(overall)}</b>" + (
        "" if overall == "PASS" else " &mdash; see failing checks above."
    ))

    footer = _sheet_footer(opts, project, sheet_num, total_sheets)
    return _wrap_sheet(header, body, footer, "member-sheet")


# ------------------------------------------------------------------ SHEAR WALL SHEET
def _shear_wall_sheet(
    item: Dict[str, Any],
    raw: Dict[str, Any],
    opts: Dict[str, Any],
    project: Dict[str, Any],
    sheet_num: int,
    total_sheets: int,
) -> str:
    header = _sheet_header(opts, project)
    checks = {c["name"]: c for c in (item.get("checks") or [])}

    method = raw.get("method") or "SDPWS 2021"
    body = '<div class="calc-title">SHEAR WALL DESIGN</div>'
    body += f'<div class="calc-sub">{_e(method)} &middot; SDPWS 2021 / NDS / ASCE 7-22</div>'

    body += _heading("Wall details", first=True)
    body += _row("Wall identification", f"<b>{_e(item.get('label'))}</b>")
    body += _row("Method", f"<b>{_e(method)}</b>")

    seis_data = raw.get("seismic_shear") or {}
    wind_data = raw.get("wind_shear") or {}
    anchor_data = raw.get("anchor_bolts") or {}
    chord_data = raw.get("chord") or {}
    defl_data = raw.get("deflection") or {}
    notes = raw.get("notes") or {}
    wall = raw.get("wall") or {}
    material = raw.get("material") or {}
    collector = raw.get("collector") or {}
    plate_nailing = raw.get("bottom_plate_nailing") or {}
    results = raw.get("results") or {}

    if wall.get("height_ft"):
        body += _row("Wall height", f"h = <b>{_n(wall.get('height_ft'), 2)}</b> ft")
    if wall.get("length_ft"):
        body += _row("Wall length", f"L = <b>{_n(wall.get('length_ft'), 2)}</b> ft")
    if wall.get("n_segments"):
        body += _row(
            "Full-height segments",
            f"n = <b>{_n(wall.get('n_segments'), 0)}</b>, "
            f"&Sigma;b<sub>i</sub> = <b>{_n(wall.get('sum_bi_ft'), 2)}</b> ft",
        )
    wall_segs = wall.get("segments") or []
    if len(wall_segs) > 1 and isinstance(wall_segs[0], dict):
        seg_rows = "".join(
            f'<tr><td>{_e(sg.get("id"))}</td>'
            f'<td class="r">{_n(sg.get("b_ft"), 2)}</td>'
            f'<td class="r">{_n(sg.get("b_adj_ft"), 2)}</td>'
            f'<td class="r">{_n(sg.get("AR"), 2)}</td>'
            f'<td class="r">{_n(sg.get("AR_factor"), 3)}</td>'
            f'<td class="r {_status_class("PASS" if sg.get("AR_ok", True) else "FAIL")}">'
            f'{"OK" if sg.get("AR_ok", True) else "NG"}</td></tr>'
            for sg in wall_segs
        )
        body += f"""<table class="dt"><thead><tr>
<th>Segment</th><th class="r">b (ft)</th><th class="r">b<sub>adj</sub> (ft)</th>
<th class="r">h/b<sub>s</sub></th><th class="r">AR factor</th><th class="r">AR check</th>
</tr></thead><tbody>{seg_rows}</tbody></table>"""
    if notes.get("sheathing_callout"):
        body += _row("Sheathing", f"<b>{_e(notes.get('sheathing_callout'))}</b>")
    if notes.get("sill_callout"):
        body += _row("Sill plate", f"<b>{_e(notes.get('sill_callout'))}</b>")
    body += _row(
        "Governing load", f"<b>{_e(raw.get('governs') or notes.get('governs'))}</b>"
    )

    # Check summary table
    summary_rows = ""
    for name, chk in results.items():
        if not isinstance(chk, dict) or chk.get("Result") is None:
            continue
        u = chk.get("Result")
        scls = "res-pass" if (u or 0) <= 100 else "res-fail"
        summary_rows += (
            f"<tr><td>{_e(name)}</td>"
            f'<td class="r">{_n(chk.get("Actual"), 2)}</td>'
            f'<td class="r">{_n(chk.get("Allowed"), 2)}</td>'
            f'<td class="r {scls}">{_pct(u)}</td></tr>'
        )
    if summary_rows:
        body += _heading("Design check summary")
        body += f"""<table class="dt"><thead><tr>
<th>Check</th><th class="r">Demand</th><th class="r">Capacity</th><th class="r">Util.</th>
</tr></thead><tbody>{summary_rows}</tbody></table>"""

    # Unit shear derivation
    body += _heading("Unit shear demand")
    V_design = raw.get("shear_design_lbs")
    L_eff = raw.get("L_eff_ft")
    Co = raw.get("Co")
    if V_design is not None and L_eff:
        body += _row_formula(
            "Governing wall shear (ASD)",
            f"V = <b>{_n(V_design, 0)}</b> lbs",
        )
        if Co:
            body += _row_formula(
                "Perforated adjustment factor",
                f"C<sub>o</sub> = <b>{_n(Co, 3)}</b> &nbsp;(SDPWS &sect;4.3.5.3, "
                "from opening area and full-height sheathing ratio)",
            )
            body += _row_formula(
                "Unit shear (perforated)",
                f"v = V / (C<sub>o</sub> &middot; &Sigma;L<sub>i</sub>) = "
                f"{_n(V_design, 0)} / {_n(L_eff, 2)} = "
                f"<b>{_n(raw.get('unit_shear_v_plf'), 1)}</b> plf",
                indent=True,
            )
        else:
            body += _row_formula(
                "Unit shear",
                f"v = V / &Sigma;b<sub>i</sub> = {_n(V_design, 0)} / {_n(L_eff, 2)} = "
                f"<b>{_n(raw.get('unit_shear_v_plf'), 1)}</b> plf",
                indent=True,
            )
    ar_chk = results.get("Aspect Ratio (h/bs)") or {}
    if ar_chk.get("Actual") is not None:
        body += _row(
            "Aspect ratio",
            f"h / b<sub>s</sub> = <b>{_n(ar_chk.get('Actual'), 2)}</b> "
            f"&le; {_n(ar_chk.get('Allowed'), 1)}",
        )

    # Seismic shear
    body += _coderef("Seismic unit shear &mdash; SDPWS &sect;4.3")
    if seis_data:
        body += _row(
            "Applied unit shear (0.7E)",
            f"v<sub>s</sub> = <b>{_n(seis_data.get('vs_plf'), 1)}</b> plf",
        )
        if seis_data.get("v_nominal_plf"):
            body += _row_formula(
                "Allowable unit shear",
                f"v<sub>allow</sub> = v<sub>n</sub> / 2 = {_n(seis_data.get('v_nominal_plf'), 0)} / 2 = "
                f"<b>{_n(seis_data.get('v_allow_seismic_plf'), 0)}</b> plf",
            )
        else:
            body += _row(
                "Allowable unit shear",
                f"v<sub>allow</sub> = <b>{_n(seis_data.get('v_allow_seismic_plf'), 0)}</b> plf",
            )
        if seis_data.get("ar_reduction"):
            body += _row(
                "Aspect ratio reduction (2b<sub>s</sub>/h factor)",
                f"<b>{_n(seis_data.get('ar_reduction'), 3)}</b>",
                )
        va = seis_data.get("v_allow_seismic_plf") or 0
        vs = seis_data.get("vs_plf") or 0
        if va:
            body += _row_formula(
                "Utilization", f"v<sub>s</sub> / v<sub>allow</sub> = <b>{vs/va:.2f}</b>", indent=True
            )
        s = seis_data.get("shear_check", "OK")
        if s == "N/A":
            body += _note(
                "Unit-shear check not applicable &mdash; segment aspect ratio "
                "h/b<sub>s</sub> &gt; 3.5 disqualifies it as a shear wall "
                "(see aspect-ratio check)."
            )
        else:
            body += _pass_line(
                "PASS" if s == "OK" else "FAIL",
                "Seismic unit shear adequate" if s == "OK" else "Seismic unit shear exceeds allowable",
            )

    # Wind shear
    body += _coderef("Wind unit shear &mdash; SDPWS &sect;4.3")
    if wind_data:
        body += _row(
            "Applied unit shear (0.6W)",
            f"v<sub>w</sub> = <b>{_n(wind_data.get('vw_plf'), 1)}</b> plf",
        )
        if wind_data.get("v_nominal_plf"):
            body += _row_formula(
                "Allowable unit shear (wind)",
                f"v<sub>allow</sub> = v<sub>n,wind</sub> / 2 = {_n(wind_data.get('v_nominal_plf'), 0)} / 2 = "
                f"<b>{_n(wind_data.get('v_allow_wind_plf'), 0)}</b> plf",
            )
        else:
            body += _row(
                "Allowable unit shear",
                f"v<sub>allow</sub> = <b>{_n(wind_data.get('v_allow_wind_plf'), 0)}</b> plf",
            )
        va = wind_data.get("v_allow_wind_plf") or 0
        vw = wind_data.get("vw_plf") or 0
        if va:
            body += _row_formula(
                "Utilization", f"v<sub>w</sub> / v<sub>allow</sub> = <b>{vw/va:.2f}</b>", indent=True
            )
        s = wind_data.get("shear_check", "OK")
        if s == "N/A":
            body += _note(
                "Unit-shear check not applicable &mdash; segment aspect ratio "
                "h/b<sub>s</sub> &gt; 3.5 disqualifies it as a shear wall "
                "(see aspect-ratio check)."
            )
        else:
            body += _pass_line(
                "PASS" if s == "OK" else "FAIL",
                "Wind unit shear adequate" if s == "OK" else "Wind unit shear exceeds allowable",
            )

    # Chord design
    if chord_data:
        body += _heading("Chord (boundary) studs")
        if material.get("chord_studs"):
            body += _row(
                "Chord build-up",
                f"<b>{_e(material.get('chord_studs'))}</b> {_e(material.get('chord_species') or '')} "
                f"(A = {_n(material.get('Ae_in2'), 1)} in&sup2;, "
                f"A<sub>net</sub> = {_n(material.get('Aen_in2'), 1)} in&sup2;)",
            )
        body += _coderef("Overturning chord forces")
        body += _row_formula(
            "Chord tension",
            f"T = V &middot; h / (moment arm) = <b>{_n(chord_data.get('T_chord_lbs'), 0)}</b> lbs "
            "(arm = wall length less chord offset, net of resisting dead load)",
        )
        body += _row(
            "Chord compression",
            f"C = <b>{_n(chord_data.get('C_chord_lbs'), 0)}</b> lbs",
        )
        tension = chord_data.get("tension") or {}
        if tension:
            body += _row(
                "Tension stress (net section)",
                f"f<sub>t</sub> = T / A<sub>net</sub> = <b>{_n(tension.get('ft_actual_psi'), 1)}</b> psi "
                f"&le; F<sub>t</sub>&prime; = <b>{_n(tension.get('Ft_adj_psi'), 0)}</b> psi",
            )
            s = tension.get("check", "OK")
            body += _pass_line(
                "PASS" if s == "OK" else "FAIL", "Chord tension adequate"
            )
        compression = chord_data.get("compression") or {}
        if compression:
            if material.get("FcE_psi"):
                body += _row_formula(
                    "Euler buckling stress",
                    f"F<sub>cE</sub> = 0.822 &middot; E<sub>min</sub>&prime; / (l<sub>e</sub>/d)&sup2; = "
                    f"<b>{_n(material.get('FcE_psi'), 0)}</b> psi &nbsp;&middot;&nbsp; "
                    f"F<sub>c</sub>* = <b>{_n(material.get('Fc_star_psi'), 0)}</b> psi",
                    indent=True,
                )
            body += _row(
                "Column stability (NDS Eq. 3.7-1)",
                f"l<sub>e</sub>/d = <b>{_n(compression.get('le_over_d'), 1)}</b>, "
                f"C<sub>P</sub> = <b>{_n(compression.get('Cp'), 3)}</b>",
            )
            body += _row(
                "Compression stress",
                f"f<sub>c</sub> = C / A = <b>{_n(compression.get('fc_actual_psi'), 1)}</b> psi "
                f"&le; F<sub>c</sub>&prime; = F<sub>c</sub>* &middot; C<sub>P</sub> = "
                f"<b>{_n(compression.get('Fc_adj_psi'), 0)}</b> psi",
            )
            s = compression.get("check", "OK")
            body += _pass_line(
                "PASS" if s == "OK" else "FAIL", "Chord compression adequate"
            )
        bearing = chord_data.get("bearing") or {}
        if bearing.get("fc_perp_psi") is not None:
            body += _row(
                "Sill plate bearing",
                f"f<sub>c&perp;</sub> = <b>{_n(bearing.get('fc_perp_psi'), 1)}</b> psi "
                f"&le; F<sub>c&perp;</sub>&prime; = <b>{_n(bearing.get('Fc_perp_adj_psi'), 0)}</b> psi",
            )
            s = bearing.get("check", "OK")
            body += _pass_line("PASS" if s == "OK" else "FAIL", "Sill bearing adequate")

    # Holdowns
    holdowns = raw.get("holdowns") or []
    if holdowns:
        body += _coderef("Holdown anchorage")
        for hd in holdowns:
            chord_n = _n(hd.get("chord"), 0)
            status = hd.get("status", "PASS")
            if status == "N/A":
                body += _row(
                    f"Holdown at chord {chord_n}",
                    f"T = <b>{_n(hd.get('T_lbs'), 0)}</b> lbs &mdash; "
                    "no net uplift, holdown not required",
                )
                continue
            if status == "REQUIRED":
                body += _row(
                    f"Holdown at chord {chord_n}",
                    f"T = <b>{_n(hd.get('T_lbs'), 0)}</b> lbs &mdash; "
                    "holdown required but none specified",
                )
                body += _pass_line("FAIL", "Holdown required to resist net uplift")
                continue
            model_txt = f"{_e(hd.get('model'))}: " if hd.get("model") else ""
            body += _row(
                f"Holdown at chord {chord_n}",
                f"{model_txt}T = <b>{_n(hd.get('T_lbs'), 0)}</b> lbs &le; capacity = "
                f"<b>{_n(hd.get('capacity_lbs'), 0)}</b> lbs "
                f"(ratio {_n((hd.get('ratio') or 0) * 100, 0)}%)",
            )
            body += _pass_line(status, "Holdown capacity adequate")

    # Collector / drag transfer
    if collector and collector.get("V_demand_lbs"):
        body += _coderef("Top plate collector / shear transfer")
        body += _row(
            "Shear transfer demand",
            f"v = <b>{_n(collector.get('unit_shear_plf'), 1)}</b> plf",
        )
        if collector.get("clip_model"):
            body += _row(
                "Framing clips",
                f"<b>{_e(collector.get('clip_model'))}</b> @ allowable "
                f"<b>{_n(collector.get('F_allow_lbs'), 0)}</b> lbs each",
            )
        body += _pass_line(
            "PASS" if collector.get("collector_ok", True) else "FAIL",
            "Collector load path adequate",
        )

    # Anchor bolts
    if anchor_data:
        body += _coderef("Sill anchorage &mdash; NDS Ch. 12 bolt values")
        if anchor_data.get("Z_para_adj_lbs"):
            body += _row(
                "Adjusted bolt capacity (parallel / perp.)",
                f"Z&prime; = <b>{_n(anchor_data.get('Z_para_adj_lbs'), 0)}</b> / "
                f"<b>{_n(anchor_data.get('Z_perp_adj_lbs'), 0)}</b> lbs",
            )
        body += _row_formula(
            "Bolts required (shear, parallel)",
            f"n = V / Z&prime; = <b>{_n(anchor_data.get('num_para'), 1)}</b> &rarr; spacing "
            f"<b>{_n(anchor_data.get('spacing_para_ft'), 1)}</b> ft o/c",
        )
        body += _row(
            "Bolts required (perpendicular)",
            f"n = <b>{_n(anchor_data.get('num_perp'), 1)}</b> &rarr; spacing "
            f"<b>{_n(anchor_data.get('spacing_perp_ft'), 1)}</b> ft o/c",
        )
        if plate_nailing.get("Z_per_nail_lbs"):
            body += _row(
                "Bottom plate nailing (alt.)",
                f"Z&prime; = <b>{_n(plate_nailing.get('Z_per_nail_lbs'), 0)}</b> lbs/nail &rarr; "
                f"spacing &le; <b>{_n(plate_nailing.get('spacing_calc_in'), 1)}</b> in o/c",
            )

    # Deflection & drift — SDPWS 4-term equation
    if defl_data:
        body += _coderef("Deflection &mdash; SDPWS Eq. 4.3-1 (bending + shear + nail slip + anchorage)")
        body += _row_formula(
            "Deflection model",
            "&delta; = 8vh&sup3;/(EAb) + vh/(1000&middot;G<sub>a</sub>) + h&middot;d<sub>a</sub>/b",
        )
        if material.get("Ga_kips_per_in"):
            body += _row(
                "Apparent shear stiffness",
                f"G<sub>a</sub> = <b>{_n(material.get('Ga_kips_per_in'), 1)}</b> kips/in "
                "(SDPWS Table 4.3A)",
            )
        wind_defl = defl_data.get("wind") or {}
        if wind_defl.get("max_in") is not None:
            wseg = (wind_defl.get("segments") or [{}])[0]
            if wseg.get("bend_in") is not None:
                body += _row_formula(
                    "Wind: term breakdown",
                    f"&delta; = {_n(wseg.get('bend_in'), 4)} (bending) + "
                    f"{_n(wseg.get('shear_slip_in'), 4)} (shear + nail slip) + "
                    f"{_n(wseg.get('anchor_in'), 4)} (anchorage, d<sub>a</sub> = "
                    f"{_n(wseg.get('da_in'), 4)} in)",
                    indent=True,
                )
            body += _row(
                "Wind deflection (serviceability)",
                f"&delta; = <b>{_n(wind_defl.get('max_in'), 3)}</b> in "
                f"&le; h/600 = <b>{_n(wind_defl.get('limit_in'), 3)}</b> in",
            )
            body += _pass_line(wind_defl.get("status", "PASS"), "Wind deflection within limits")
        seis_defl = defl_data.get("seismic") or {}
        if seis_defl.get("max_in") is not None:
            segs = seis_defl.get("segments") or [{}]
            sseg = segs[0]
            d_e = sseg.get("delta_e_in")
            if sseg.get("bend_in") is not None:
                body += _row_formula(
                    "Seismic: term breakdown (elastic, strength-level V)",
                    f"&delta;<sub>e</sub> = {_n(sseg.get('bend_in'), 4)} (bending) + "
                    f"{_n(sseg.get('shear_slip_in'), 4)} (shear + nail slip) + "
                    f"{_n(sseg.get('anchor_in'), 4)} (anchorage) = "
                    f"<b>{_n(d_e, 4)}</b> in",
                    indent=True,
                )
            body += _row_formula(
                "Amplified seismic drift",
                f"&delta;<sub>amp</sub> = C<sub>d</sub> &middot; &delta;<sub>e</sub> / I<sub>e</sub> = "
                f"{_n(seis_defl.get('Cd'), 1)} &middot; {_n(d_e, 4)} / {_n(seis_defl.get('Ie'), 1)} = "
                f"<b>{_n(seis_defl.get('max_in'), 3)}</b> in",
            )
            body += _row(
                "Drift limit (ASCE 7 Table 12.12-1)",
                f"&Delta;<sub>allow</sub> = 0.020 &middot; h<sub>sx</sub> = "
                f"<b>{_n(seis_defl.get('limit_in'), 3)}</b> in",
            )
            # Per-segment table for multi-panel walls
            if len(segs) > 1:
                seg_rows = "".join(
                    f'<tr><td>{_e(sg.get("segment"))}</td>'
                    f'<td class="r">{_n(sg.get("b_ft"), 2)}</td>'
                    f'<td class="r">{_n(sg.get("v_plf"), 1)}</td>'
                    f'<td class="r">{_n(sg.get("T_lbs"), 0)}</td>'
                    f'<td class="r">{_n(sg.get("C_lbs"), 0)}</td>'
                    f'<td class="r">{_n(sg.get("delta_amp_in"), 3)}</td>'
                    f'<td class="r {_status_class(sg.get("status", "PASS"))}">{_e(sg.get("status"))}</td></tr>'
                    for sg in segs
                )
                body += f"""<table class="dt"><thead><tr>
<th>Segment</th><th class="r">b (ft)</th><th class="r">v (plf)</th><th class="r">T (lbs)</th>
<th class="r">C (lbs)</th><th class="r">&delta;<sub>amp</sub> (in)</th><th class="r">Result</th>
</tr></thead><tbody>{seg_rows}</tbody></table>"""
        s = defl_data.get("drift_check", "OK")
        body += _pass_line("PASS" if s == "OK" else "FAIL", "Story drift within limits")

    footer = _sheet_footer(opts, project, sheet_num, total_sheets)
    return _wrap_sheet(header, body, footer, "member-sheet")


# ------------------------------------------------------------------ DIAPHRAGM SHEET
def _diaphragm_sheet(
    item: Dict[str, Any],
    raw: Dict[str, Any],
    opts: Dict[str, Any],
    project: Dict[str, Any],
    sheet_num: int,
    total_sheets: int,
) -> str:
    header = _sheet_header(opts, project)
    checks = {c["name"]: c for c in (item.get("checks") or [])}
    inter = raw.get("intermediate") or {}
    geo = inter.get("geometry") or {}
    dem = inter.get("demands") or {}
    cap = inter.get("capacity") or {}

    body = '<div class="calc-title">DIAPHRAGM DESIGN</div>'
    body += '<div class="calc-sub">SDPWS 2021 &sect;4.2 &middot; flexible single-span diaphragm &middot; ASD</div>'

    body += _heading("Diaphragm details", first=True)
    body += _row("Identification", f"<b>{_e(item.get('label'))}</b>")
    if inter.get("sheathing_note"):
        body += _row("Sheathing", f"<b>{_e(inter.get('sheathing_note'))}</b>")
    body += _row(
        "Geometry",
        f"span L = <b>{_n(geo.get('span_ft'), 1)}</b> ft between lateral lines, "
        f"depth d = <b>{_n(geo.get('depth_ft'), 1)}</b> ft",
    )
    body += _row(
        "Construction",
        ("<b>blocked</b>" if geo.get("blocked") else "<b>unblocked</b>"),
    )
    if inter.get("chord_member"):
        body += _row("Chord member", f"<b>{_e(inter.get('chord_member'))}</b>")
    body += _row("Governing load", f"<b>{_e(dem.get('governs'))}</b>")

    body += _heading("Demands")
    body += _row(
        "Story shear (ASD)",
        f"V<sub>s</sub> = <b>{_n(dem.get('Vs_lbs'), 0)}</b> lbs (0.7E) &nbsp;&middot;&nbsp; "
        f"V<sub>w</sub> = <b>{_n(dem.get('Vw_lbs'), 0)}</b> lbs (0.6W)",
    )
    body += _row_formula(
        "Uniform diaphragm load",
        f"w = V / L = <b>{_n(dem.get('w_s_plf'), 1)}</b> / <b>{_n(dem.get('w_w_plf'), 1)}</b> plf (E / W)",
    )
    body += _row_formula(
        "Maximum unit shear",
        f"v = V / (2 &middot; d) = <b>{_n(dem.get('v_s_plf'), 1)}</b> / "
        f"<b>{_n(dem.get('v_w_plf'), 1)}</b> plf (E / W)",
    )
    body += _row_formula(
        "Chord force",
        f"T = C = w &middot; L&sup2; / (8 &middot; d) = <b>{_n(dem.get('chord_T_s_lbs'), 0)}</b> / "
        f"<b>{_n(dem.get('chord_T_w_lbs'), 0)}</b> lbs (E / W)",
    )
    body += _row(
        "Reaction per lateral line",
        f"R = V / 2 = <b>{_n(dem.get('R_s_lbs'), 0)}</b> / <b>{_n(dem.get('R_w_lbs'), 0)}</b> lbs (E / W)",
    )

    body += _coderef("Unit shear capacity &mdash; SDPWS &sect;4.1.4 / Table 4.2A")
    if cap.get("v_nominal_plf"):
        body += _row("Nominal unit shear (schedule)", f"v<sub>n</sub> = <b>{_n(cap.get('v_nominal_plf'), 0)}</b> plf")
        body += _row_formula(
            "Allowable (seismic / wind)",
            f"v<sub>n</sub>/2 = <b>{_n(cap.get('v_allow_seismic_plf'), 0)}</b> plf &nbsp;&middot;&nbsp; "
            f"1.4&middot;v<sub>n</sub>/2 = <b>{_n(cap.get('v_allow_wind_plf'), 0)}</b> plf",
        )
        for chk_name, txt in (
            ("Unit Shear Seismic (plf)", "Seismic unit shear adequate"),
            ("Unit Shear Wind (plf)", "Wind unit shear adequate"),
        ):
            chk = checks.get(chk_name, {})
            if chk.get("utilization") is not None:
                body += _row_formula(
                    f"Utilization ({chk_name.split()[2].lower()})",
                    f"<b>{float(chk['utilization'])/100:.2f}</b>", indent=True,
                )
                body += _pass_line(chk.get("status", "PASS"), txt)
    else:
        body += _note(
            "<b>Capacity not evaluated</b> &mdash; no nominal unit shear provided. "
            "Compare the demands above against the diaphragm schedule / SDPWS Table 4.2A."
        )

    ar_chk = checks.get("Aspect Ratio (L/d)", {})
    body += _coderef("Aspect ratio &mdash; SDPWS Table 4.2.4")
    body += _row(
        "Aspect ratio",
        f"L/d = <b>{_n(ar_chk.get('actual'), 2)}</b> &le; {_n(ar_chk.get('allowed'), 0)}",
    )
    body += _pass_line(ar_chk.get("status", "PASS"), "Aspect ratio within limits")

    for note in (inter.get("notes") or []):
        body += _note(f"&bull; {_e(note)}")

    footer = _sheet_footer(opts, project, sheet_num, total_sheets)
    return _wrap_sheet(header, body, footer, "member-sheet")


# ------------------------------------------------------------------ I-JOIST SHEET
def _ijoist_sheet(
    item: Dict[str, Any],
    raw: Dict[str, Any],
    opts: Dict[str, Any],
    project: Dict[str, Any],
    sheet_num: int,
    total_sheets: int,
) -> str:
    header = _sheet_header(opts, project)
    checks = {c["name"]: c for c in (item.get("checks") or [])}
    inter = raw.get("intermediate") or {}
    product = inter.get("product") or {}
    geo = inter.get("geometry") or {}
    loads = inter.get("loads") or {}
    combos = inter.get("combos") or []
    brg = inter.get("bearing") or {}
    defl = inter.get("deflection") or {}

    body = '<div class="calc-title">I-JOIST DESIGN</div>'
    body += (f'<div class="calc-sub">{_e(product.get("source") or "Weyerhaeuser TJ-4000")} '
             "&middot; ICC-ES ESR-1153 &middot; ASD</div>")

    body += _heading("Member details", first=True)
    body += _row("Product", f"<b>{_e(item.get('size'))}</b>")
    body += _row("Clear span", f"L<sub>clear</sub> = <b>{_n(geo.get('L_clear_ft'), 2)}</b> ft")
    body += _row_formula(
        "Design span",
        f"L = L<sub>clear</sub> + 2 &middot; 0.875&Prime;/12 = <b>{_n(geo.get('L_span_ft'), 2)}</b> ft",
    )
    body += _row("Spacing", f"s = <b>{_n(geo.get('spacing_in'), 1)}</b> in o.c.")

    body += _heading("Design properties (100% load duration)")
    body += _row("Allowable moment", f"M<sub>r</sub> = <b>{_n(product.get('Mr_ftlb_100'), 0)}</b> ft&middot;lb")
    body += _row("Allowable shear", f"V<sub>r</sub> = <b>{_n(product.get('Vr_lb_100'), 0)}</b> lbs")
    body += _row("Bending stiffness", f"EI = <b>{_n(product.get('EI_e6'), 0)}</b> &times; 10&#8310; lb&middot;in&sup2;")
    body += _row("Self weight", f"<b>{_n(product.get('weight_plf'), 1)}</b> plf")
    body += _note("Moment values may not be increased by a repetitive-member factor (TJ-4000).")

    if loads:
        body += _heading("Load buildup")
        area_parts = []
        for sym, key in (("D", "D_psf"), ("L", "L_psf"), ("L<sub>r</sub>", "Lr_psf"), ("S", "S_psf")):
            if loads.get(key):
                area_parts.append(f"{sym} = {_n(loads[key], 1)} psf")
        if area_parts:
            body += _row("Area loads", " &nbsp;&middot;&nbsp; ".join(area_parts))
        body += _row_formula(
            "Dead line load",
            f"w<sub>D</sub> = D &middot; s/12 + w<sub>self</sub> = "
            f"{_n(loads.get('D_psf'), 1)} &middot; {_n(geo.get('trib_width_ft'), 2)} + "
            f"{_n(loads.get('self_weight_plf'), 1)} = <b>{_n(loads.get('w_D_plf'), 1)}</b> plf",
        )
        for sym, key in (("L", "w_L_plf"), ("L<sub>r</sub>", "w_Lr_plf"), ("S", "w_S_plf")):
            if loads.get(key):
                body += _row(f"{sym} line load", f"w<sub>{sym}</sub> = <b>{_n(loads[key], 1)}</b> plf")

    if combos:
        body += _coderef("ASD load combinations &mdash; ASCE 7-22 &sect;2.4.1 (IBC 2024 &sect;1605.1)")
        # Combos whose transient terms (L/Lr/S/W) are all zero collapse onto the
        # pure-dead load (e.g. a roof beam has no floor L). Show the distinct
        # combinations only, so the table doesn't list several identical rows.
        d_only = next((c.get("w_plf") for c in combos if c.get("name") == "1.0D"), None)
        shown, suppressed = [], 0
        for c in combos:
            if c.get("name") != "1.0D" and d_only is not None and abs((c.get("w_plf") or 0) - d_only) <= 0.05:
                suppressed += 1
                continue
            shown.append(c)
        rows = "".join(
            f'<tr><td>{_e(c.get("name"))}</td><td class="r">{_n(c.get("w_plf"), 1)}</td>'
            f'<td class="r">{_n(c.get("CD"), 2)}</td></tr>'
            for c in shown
        )
        body += f"""<table class="dt"><thead><tr>
<th>Combination</th><th class="r">w (plf)</th><th class="r">C<sub>D</sub></th>
</tr></thead><tbody>{rows}</tbody></table>"""
        if suppressed:
            body += (
                '<div class="note-line">'
                f"{suppressed} further combination(s) omitted &mdash; their L / L<sub>r</sub> / S / W "
                "terms are zero for this member, reducing them to 1.0D above."
                "</div>"
            )

    moment_chk = checks.get("Moment (Ft-lbs)", {})
    shear_chk = checks.get("Shear (lbs)", {})
    reaction_chk = checks.get("Member Reaction (lbs)", {})
    tl_chk = checks.get("Total Load Defl (in)", {})
    ll_chk = checks.get("Live Load Defl. (in)", {})

    body += _coderef("Bending")
    body += _row("Applied moment", f"M = w &middot; L&sup2;/8 = <b>{_n(moment_chk.get('actual'), 0)}</b> ft&middot;lb "
                                   f"({_e(moment_chk.get('combo') or '')})")
    body += _row_formula(
        "Duration-adjusted capacity",
        f"M&prime; = M<sub>r</sub> &middot; C<sub>D</sub> = <b>{_n(moment_chk.get('allowed'), 0)}</b> ft&middot;lb "
        f"(C<sub>D</sub> = {_n(moment_chk.get('ldf'), 2)})",
    )
    if moment_chk.get("utilization") is not None:
        body += _row_formula("Utilization", f"M / M&prime; = <b>{float(moment_chk['utilization'])/100:.2f}</b>", indent=True)
        body += _pass_line(moment_chk.get("status", "PASS"), "Moment capacity adequate")

    body += _coderef("Shear (at inside face of support)")
    body += _row("Applied shear", f"V = <b>{_n(shear_chk.get('actual'), 0)}</b> lbs")
    body += _row("Duration-adjusted capacity",
                 f"V&prime; = V<sub>r</sub> &middot; C<sub>D</sub> = <b>{_n(shear_chk.get('allowed'), 0)}</b> lbs")
    if shear_chk.get("utilization") is not None:
        body += _row_formula("Utilization", f"V / V&prime; = <b>{float(shear_chk['utilization'])/100:.2f}</b>", indent=True)
        body += _pass_line(shear_chk.get("status", "PASS"), "Shear capacity adequate")

    body += _coderef("End reaction / bearing")
    body += _row(
        "Bearing length",
        f"<b>{_n(brg.get('bearing_length_in'), 2)}</b> in"
        + (" (web stiffeners)" if brg.get("web_stiffeners") else " (no web stiffeners)"),
    )
    body += _row_formula(
        "Reaction capacity",
        f"interpolated 1&frac34;&Prime; &rarr; 3&frac12;&Prime; bearing: "
        f"{_n(brg.get('R175_lbs'), 0)} &rarr; {_n(brg.get('R35_lbs'), 0)} lbs = "
        f"<b>{_n(brg.get('R_allow_lbs'), 0)}</b> lbs (not duration-adjusted)",
    )
    body += _row("Applied reaction", f"R = <b>{_n(reaction_chk.get('actual'), 0)}</b> lbs")
    if reaction_chk.get("utilization") is not None:
        body += _pass_line(reaction_chk.get("status", "PASS"), "Bearing capacity adequate")

    body += _coderef("Deflection &mdash; TJ-4000 two-term formula")
    body += _row_formula(
        "Deflection model",
        f"&Delta; = 22.5wL&#8308;/EI + {_n(defl.get('formula_coeff'), 2)}wL&sup2;/(d&middot;10&#8309;)",
    )
    if ll_chk.get("actual") is not None:
        body += _row_formula(
            "Live load: term breakdown",
            f"{_n(defl.get('bend_LL_in'), 3)} (bending) + {_n(defl.get('shear_LL_in'), 3)} (shear) = "
            f"<b>{_n(ll_chk.get('actual'), 3)}</b> in &le; L/{_n(defl.get('LL_denom'), 0)} = "
            f"<b>{_n(ll_chk.get('allowed'), 3)}</b> in",
        )
    if tl_chk.get("actual") is not None:
        body += _row(
            "Total-load deflection",
            f"&Delta;<sub>TL</sub> = <b>{_n(tl_chk.get('actual'), 3)}</b> in "
            f"&le; L/{_n(defl.get('TL_denom'), 0)} = <b>{_n(tl_chk.get('allowed'), 3)}</b> in",
        )
    defl_ok = (ll_chk.get("utilization") or 0) <= 100 and (tl_chk.get("utilization") or 0) <= 100
    body += _pass_line("PASS" if defl_ok else "FAIL",
                       "Deflections within code limits" if defl_ok else "Deflection exceeds code limits")

    for note in (inter.get("notes") or []):
        body += _note(f"&bull; {_e(note)}")

    footer = _sheet_footer(opts, project, sheet_num, total_sheets)
    return _wrap_sheet(header, body, footer, "member-sheet")


# ------------------------------------------------------------------ GENERIC FALLBACK SHEET
def _generic_sheet(
    item: Dict[str, Any],
    raw: Dict[str, Any],
    opts: Dict[str, Any],
    project: Dict[str, Any],
    sheet_num: int,
    total_sheets: int,
) -> str:
    header = _sheet_header(opts, project)
    checks = item.get("checks") or []

    body = f'<div class="calc-title">{_e((item.get("application") or "MEMBER").upper())} DESIGN</div>'
    body += f'<div class="calc-sub">{_e(item.get("system") or "")} member check</div>'

    body += _heading("Member details", first=True)
    body += _row("Label", f"<b>{_e(item.get('label'))}</b>")
    body += _row("Size", f"<b>{_e(item.get('size'))}</b>")

    body += _heading("Design checks")
    for chk in checks:
        body += _coderef(_e(chk.get("name") or "Check"))
        body += _row("Actual", f"<b>{_n(chk.get('actual'))}</b>")
        body += _row("Allowable", f"<b>{_n(chk.get('allowed'))}</b>")
        u = chk.get("utilization")
        if u is not None:
            body += _row_formula(
                "Utilization", f"<b>{float(u)/100:.3f}</b>", indent=True
            )
        combo = chk.get("combo")
        if combo:
            body += _row("Load combination", _e(combo))
        body += _pass_line(
            chk.get("status", "PASS"),
            f"{_e(chk.get('name'))} check "
            + ("satisfied" if chk.get("status") in ("PASS", "OK") else "fails"),
        )

    footer = _sheet_footer(opts, project, sheet_num, total_sheets)
    return _wrap_sheet(header, body, footer, "member-sheet")


# ------------------------------------------------------------------ DISPATCHER
# Maps source_type (from analysis normalization) to sheet builder
_SHEET_BUILDERS = {
    "roof_rafter": _timber_beam_sheet,
    "ceiling_joist": _timber_beam_sheet,
    "ridge_beam": _timber_beam_sheet,
    "hip_valley_rafter": _timber_beam_sheet,
    "roof_drop_beam": _timber_beam_sheet,
    "roof_flush_beam": _timber_beam_sheet,
    "floor_joist": _timber_beam_sheet,
    "floor_drop_beam": _timber_beam_sheet,
    "floor_flush_beam": _timber_beam_sheet,
    "header": _timber_beam_sheet,
    "stud_wall": _stud_wall_sheet,
    "masonry_wall": _masonry_sheet,
    "post": _post_sheet,
    "continuous_footing": _footing_sheet,
    "pad_footing": _pad_footing_sheet,
    "shear_wall": _shear_wall_sheet,
    "i_joist": _ijoist_sheet,
    "diaphragm": _diaphragm_sheet,
}


def _build_member_sheet(
    item: Dict[str, Any],
    opts: Dict[str, Any],
    project: Dict[str, Any],
    sheet_num: int,
    total_sheets: int,
) -> str:
    raw = item.get("raw") or {}
    source_type = item.get("source_type") or ""

    if item.get("error"):
        header = _sheet_header(opts, project)
        body = (
            f'<div class="calc-title">ERROR &mdash; {_e(item.get("label"))}</div>'
            f'<div class="note-line" style="color:#b91c1c"><b>Error:</b> {_e(item["error"])}</div>'
        )
        footer = _sheet_footer(opts, project, sheet_num, total_sheets)
        return _wrap_sheet(header, body, footer, "member-sheet")

    # Masonry special detection (may come through header mapper)
    if raw.get("method") and "TMS" in str(raw.get("method", "")):
        builder = _masonry_sheet
    elif raw.get("seismic_shear") or raw.get("wind_shear"):
        builder = _shear_wall_sheet
    else:
        builder = _SHEET_BUILDERS.get(source_type, _generic_sheet)

    return builder(item, raw, opts, project, sheet_num, total_sheets)


# ------------------------------------------------------------------ CSS
_CSS = """
*{box-sizing:border-box;}
:root{--brand:#6A4FE3;--brand-dark:#4A38A6;--brand-tint:#EFEBFB;--brand-tint2:#F7F5FE;
  --ink:#15151A;--muted:#5f6068;--faint:#8a8b93;--hair:#dcdce1;--frame:#cdced6;}
body{margin:0;background:#8d8d90;color:var(--ink);font-family:'Libre Franklin',system-ui,-apple-system,Segoe UI,sans-serif;font-size:10.5px;line-height:1.5;-webkit-font-smoothing:antialiased;}
.sheet{width:850px;max-width:96vw;margin:22px auto;background:#fff;padding:26px 32px 22px;box-shadow:0 1px 8px rgba(0,0,0,.32);}
b{font-weight:700;} sub{font-size:0.72em;vertical-align:-0.22em;} sup{font-size:0.72em;}
.hdr{display:flex;justify-content:space-between;align-items:flex-end;}
.hdr-left{display:flex;align-items:center;gap:12px;}
.wm{font-size:21px;font-weight:700;letter-spacing:-0.5px;line-height:1;}
.wm-sub{font-size:8.5px;letter-spacing:2.4px;text-transform:uppercase;color:var(--muted);font-weight:600;margin-top:3px;}
.hdr-meta{display:flex;gap:30px;}
.mi{display:flex;flex-direction:column;align-items:flex-end;}
.mk{font-size:8px;letter-spacing:1.4px;text-transform:uppercase;color:var(--faint);font-weight:600;}
.mv{font-size:12.5px;font-weight:700;font-variant-numeric:tabular-nums;}
.hdr-rule{height:3px;background:var(--brand);margin-top:9px;border-radius:1px;}
.signoff-strip{display:flex;gap:30px;padding:7px 0 9px;}
.so{display:flex;flex-direction:column;}
.sk{font-size:8px;letter-spacing:1.2px;text-transform:uppercase;color:var(--faint);font-weight:600;}
.sv{font-size:10.5px;font-weight:600;}
.body{border:1px solid var(--frame);border-top:2px solid var(--brand);padding:15px 20px 20px;}
.calc-title{font-size:14px;font-weight:700;letter-spacing:-0.2px;margin:2px 0 1px;}
.calc-sub{font-size:10.5px;color:var(--muted);margin-bottom:3px;}
.h{font-size:11px;font-weight:700;margin:15px 0 4px;padding-left:11px;position:relative;}
.h::before{content:"";position:absolute;left:0;top:2px;bottom:2px;width:3px;background:var(--brand);border-radius:2px;}
.h.first{margin-top:8px;}
.coderef{font-size:10.5px;font-weight:700;color:var(--brand-dark);margin:12px 0 3px;letter-spacing:0.1px;}
.row{display:grid;grid-template-columns:minmax(140px,33%) 1fr;gap:14px;padding:1.5px 0;font-size:10.5px;align-items:start;}
.row .d{color:var(--ink);}
.row .v{font-variant-numeric:tabular-nums;line-height:1.5;word-break:break-word;}
.row.indent .d{padding-left:20px;color:var(--muted);}
.note-line{font-size:10.5px;padding:1px 0;color:var(--muted);}
.pass{font-weight:700;font-style:italic;text-align:right;font-size:10px;margin:3px 0 4px;}
.pass .pchk{color:var(--brand);font-style:normal;margin-right:4px;}
.pass .fail-chk{color:#b91c1c;}
.pass.fail-pass{color:#b91c1c;}
.res-pass{color:var(--brand-dark);font-weight:700;}
.res-fail{color:#b91c1c;font-weight:700;}
.res-err{color:#b45309;font-weight:700;}
.dt{width:100%;border-collapse:collapse;margin:7px 0 3px;font-size:10px;font-variant-numeric:tabular-nums;}
.dt th{background:var(--brand-tint);color:var(--brand-dark);border:0.7px solid #b9b2e0;padding:5px 8px;text-align:left;font-weight:700;font-size:9px;letter-spacing:0.2px;}
.dt td{border:0.7px solid var(--hair);padding:5px 8px;text-align:left;}
.dt td.r,.dt th.r{text-align:right;}
.dt tr.gov td{font-weight:700;background:var(--brand-tint2);}
.foot{display:flex;justify-content:space-between;font-size:8.5px;color:var(--faint);margin-top:12px;}
.software-note{font-size:8.5px;color:var(--muted);margin-top:15px;border-top:0.6px solid var(--hair);padding-top:8px;line-height:1.55;}
@media print{body{background:#fff;}.sheet{box-shadow:none;margin:0;width:auto;padding:0;page-break-before:always;}.sheet:first-child{page-break-before:auto;}}
"""


# ------------------------------------------------------------------ PUBLIC API
def build_html_report(
    extracted_data: Dict[str, Any],
    analysis_result: Dict[str, Any],
    *,
    options: Optional[Dict[str, Any]] = None,
) -> str:
    opts = options or {}
    project = extracted_data.get("project_data") or {}
    if not isinstance(project, dict):
        project = {}
    items = analysis_result.get("items", [])

    hazard = extracted_data.get("hazard_lookup_result") or {}
    if not isinstance(hazard, dict):
        hazard = {}
    lateral = analysis_result.get("lateral") or {}
    has_hazard = bool(hazard.get("wind") or hazard.get("snow") or hazard.get("seismic"))
    has_lateral = bool(
        lateral.get("computed") or lateral.get("line_shears")
        or lateral.get("story_shears") or lateral.get("component_pressures")
    )

    # Closing disclaimer must describe only the modules actually present, not a
    # fixed boilerplate list of every standard the engine can handle.
    types = {it.get("source_type") for it in items}
    std_parts = []
    if types & _TIMBER_SOURCE_TYPES:
        std_parts.append("timber per NDS (ASD)")
    if "i_joist" in types:
        std_parts.append("I-joists per ICC-ES ESR-1153")
    if "masonry_wall" in types:
        std_parts.append("masonry per TMS 402 (Strength)")
    if types & {"continuous_footing", "pad_footing", "slab"}:
        std_parts.append("concrete per ACI 318-19")
    if "shear_wall" in types or has_lateral:
        std_parts.append("shear walls per SDPWS 2021")
    if std_parts or has_hazard or has_lateral:
        std_parts.append("loads per ASCE 7-22")
    std_clause = (" " + " &middot; ".join(std_parts) + ".") if std_parts else ""
    assumptions_ref = (
        " Design assumptions are stated on the Lateral Load Analysis sheet."
        if has_lateral else ""
    )

    total_sheets = 1 + (1 if has_hazard else 0) + (1 if has_lateral else 0) + len(items)

    sheets = []
    sheets.append(
        _build_cover_sheet(extracted_data, analysis_result, opts, project, total_sheets)
    )
    sheet_num = 2
    if has_hazard:
        sheets.append(_hazard_sheet(hazard, opts, project, sheet_num, total_sheets))
        sheet_num += 1
    if has_lateral:
        sheets.append(_lateral_sheet(lateral, opts, project, sheet_num, total_sheets))
        sheet_num += 1
    for item in items:
        sheets.append(_build_member_sheet(item, opts, project, sheet_num, total_sheets))
        sheet_num += 1

    title = _e(
        project.get("project_name") or opts.get("project_name") or "Calculation Report"
    )
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    all_sheets = "\n".join(sheets)

    # substitute sequential page numbers (pagination may have split sheets)
    page_total = all_sheets.count(_PG_TOKEN)
    parts = all_sheets.split(_PG_TOKEN)
    all_sheets = "".join(
        p + (str(i + 1) if i < len(parts) - 1 else "") for i, p in enumerate(parts)
    ).replace(_PGTOT_TOKEN, str(page_total))

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} &mdash; Structural Calculation Package</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Libre+Franklin:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{_CSS}</style>
</head><body>
{all_sheets}
<div class="sheet">
  <div class="software-note">
    This report was generated by the SmartStructs structural calculation engine on {gen}.
    Results are computer-generated and must be reviewed and sealed by the engineer of record.
    Utilization = demand &divide; capacity.{std_clause}{assumptions_ref}
  </div>
</div>
</body></html>"""


def render_pdf(html: str, target_path: Optional[str] = None) -> Optional[bytes]:
    from weasyprint import HTML  # type: ignore[import-untyped]
    if target_path is not None:
        HTML(string=html).write_pdf(target_path)
        return None
    return HTML(string=html).write_pdf()