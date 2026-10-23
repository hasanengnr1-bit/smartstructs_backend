"""Render this draft's small LaTeX subset as a readable HTML preview.

This is a convenience preview, not a general LaTeX converter. The .tex file is
the manuscript source.
"""
from html import escape
from pathlib import Path
import re

HERE = Path(__file__).parent
src = (HERE / "timberparse_draft.tex").read_text(encoding="utf-8")
src = src.split(r"\begin{document}", 1)[1].split(r"\end{document}", 1)[0]
src = src.replace(r"\maketitle", "")
src = re.sub(r"^.*Draft status\..*$", "@@WARNING@@", src, flags=re.M)

ref_keys = re.findall(r"\\bibitem\{([^}]+)\}", src)
ref_numbers = {key: number for number, key in enumerate(ref_keys, 1)}


def inline(value: str) -> str:
    value = value.replace(r'\"a', 'ä').replace(r'\"o', 'ö').replace(r"\'e", 'é')
    value = escape(value.strip())
    for command, tag, prefix in [
        ("todo", "strong", "PLACEHOLDER: "),
        ("planned", "strong", "PLANNED: "),
        ("textbf", "strong", ""),
        ("emph", "em", ""),
        ("texttt", "code", ""),
    ]:
        value = re.sub(
            rf"\\{command}\{{([^{{}}]*)\}}",
            lambda m: f'<{tag} class="placeholder">[{prefix}{m.group(1)}]</{tag}>'
            if command in {"todo", "planned"}
            else f"<{tag}>{m.group(1)}</{tag}>",
            value,
        )
    value = re.sub(
        r"\\cite\{([^}]+)\}",
        lambda m: '[' + ', '.join(
            f'<a href="#{key.strip()}">{ref_numbers[key.strip()]}</a>'
            for key in m.group(1).split(',')
        ) + ']',
        value,
    )
    value = re.sub(r"\\url\{([^}]+)\}", lambda m: f'<a href="{m.group(1)}">{m.group(1)}</a>', value)
    value = re.sub(r"\$([^$]+)\$", lambda m: r"\(" + m.group(1) + r"\)", value)
    value = value.replace(r"\_", "_").replace(r"\%", "%")
    value = value.replace(r"\emph", "").replace(r"\noindent", "")
    value = value.replace(r"\ ", " ").replace('``', '“').replace('&#x27;&#x27;', '”')
    value = value.replace("---", "—").replace("--", "–").replace("~", " ")
    return value


blocks: list[str] = []


def save(html: str) -> str:
    blocks.append(html)
    return f"\n\n@@BLOCK{len(blocks)-1}@@\n\n"


def render_table(match: re.Match) -> str:
    raw = match.group(0)
    caption = re.search(r"\\caption\{([^}]*)\}", raw)
    tabular = re.search(r"\\begin\{tabular\}[^\n]*\n(.*?)\\end\{tabular\}", raw, re.S)
    if not tabular:
        return ""
    rows = []
    for row in re.split(r"\\\\\s*\n", tabular.group(1)):
        row = re.sub(r"\\(?:toprule|midrule|bottomrule)", "", row).strip()
        if not row or "&" not in row:
            continue
        cells = [inline(cell) for cell in row.split("&")]
        tag = "th" if not rows else "td"
        rows.append("<tr>" + "".join(f"<{tag}>{cell}</{tag}>" for cell in cells) + "</tr>")
    return save(f'<figure class="table"><figcaption>{inline(caption.group(1)) if caption else ""}</figcaption><table>{"".join(rows)}</table></figure>')


src = re.sub(r"\\begin\{table\}.*?\\end\{table\}", render_table, src, flags=re.S)


def render_enumerate(match: re.Match) -> str:
    entries = re.split(r"\\item\s*", match.group(1))[1:]
    return save('<ol class="research-questions">' + ''.join(
        f'<li><strong>RQ{number}:</strong> {inline(entry)}</li>'
        for number, entry in enumerate(entries, 1)
    ) + '</ol>')


src = re.sub(r"\\begin\{enumerate\}[^\n]*\n(.*?)\\end\{enumerate\}", render_enumerate, src, flags=re.S)
src = re.sub(
    r"\\begin\{equation\}(.*?)\\end\{equation\}",
    lambda m: save('<div class="equation">\\[' + m.group(1).strip() + '\\]</div>'),
    src,
    flags=re.S,
)


def render_bibliography(match: re.Match) -> str:
    entries = re.split(r"\\bibitem\{([^}]+)\}", match.group(1))[1:]
    html = []
    for key, entry in zip(entries[0::2], entries[1::2]):
        html.append(f'<li id="{key}">{inline(entry)}</li>')
    return save('<h2>References</h2><ol class="references">' + "".join(html) + "</ol>")


src = re.sub(r"\\begin\{thebibliography\}.*?\n(.*?)\\end\{thebibliography\}", render_bibliography, src, flags=re.S)
src = src.replace(r"\begin{abstract}", "\n\n@@ABSTRACT@@\n\n").replace(r"\end{abstract}", "")
src = re.sub(r"\\label\{[^}]*\}", "", src)
src = re.sub(r"(?<!\n)\n(\\(?:sub)?section\{)", r"\n\n\1", src)

parts = []
for item in re.split(r"\n\s*\n", src):
    item = item.strip()
    if not item:
        continue
    if item == "@@WARNING@@":
        parts.append('<aside><strong>Draft status.</strong> No empirical evaluation has been completed. Blue placeholders and planned sample sizes are illustrative and must be replaced with observed values before public release. The synthetic software demonstration is excluded from the proposed research dataset.</aside>')
    elif item == "@@ABSTRACT@@":
        parts.append("<h2>Abstract</h2>")
    elif re.fullmatch(r"@@BLOCK\d+@@", item):
        parts.append(blocks[int(item[7:-2])])
    elif item.startswith(r"\section{"):
        m = re.match(r"\\section\{([^}]*)\}(.*)", item, re.S)
        parts.append(f"<h2>{inline(m.group(1))}</h2>")
        if m.group(2).strip():
            parts.append(f"<p>{inline(m.group(2))}</p>")
    elif item.startswith(r"\subsection{"):
        m = re.match(r"\\subsection\{([^}]*)\}(.*)", item, re.S)
        parts.append(f"<h3>{inline(m.group(1))}</h3>")
        if m.group(2).strip():
            parts.append(f"<p>{inline(m.group(2))}</p>")
    else:
        parts.append(f"<p>{inline(item)}</p>")

css = """
@page { size: A4; margin: 21mm; }
* { box-sizing: border-box; }
body { font: 16px/1.62 Georgia, 'Times New Roman', serif; color: #1e293b; background: #edf1f4; margin: 0; }
main { max-width: 840px; min-height: 100vh; margin: 0 auto; padding: 52px 70px 70px; background: white; box-shadow: 0 12px 40px #1425361a; }
h1 { font: 700 33px/1.16 Georgia, serif; letter-spacing: -.02em; margin: 0 0 12px; color: #13283b; }
h2 { font: 700 21px/1.25 Georgia, serif; margin: 38px 0 12px; color: #13283b; }
h3 { font: 700 17px/1.3 Georgia, serif; margin: 25px 0 8px; color: #13283b; }
p { margin: 0 0 15px; text-align: justify; }
.byline { color: #536779; margin-bottom: 26px; font: 14px/1.5 system-ui, sans-serif; }
.placeholder { color: #1b62a3; font-weight: 700; }
aside { border: 1px solid #ce756b; background: #fff5f3; padding: 14px 16px; font: 14px/1.5 system-ui, sans-serif; margin: 24px 0 32px; }
code { background: #f1f5f8; padding: 1px 3px; font-size: .9em; }
a { color: #1b62a3; }
.equation { overflow-x: auto; text-align: center; margin: 22px 0; }
figure.table { margin: 24px 0 28px; overflow-x: auto; break-inside: avoid; }
figcaption { font: 13px/1.5 system-ui, sans-serif; color: #536779; margin-bottom: 8px; }
table { border-collapse: collapse; width: 100%; font-size: 14px; line-height: 1.45; }
th, td { padding: 7px 8px; text-align: left; vertical-align: top; border-bottom: 1px solid #dce4e9; }
th { border-top: 2px solid #263f55; border-bottom: 1px solid #263f55; }
.references { padding-left: 20px; font-size: 14px; }
.references li { margin-bottom: 9px; overflow-wrap: anywhere; }
.research-questions { padding: 0; list-style: none; }
.research-questions li { margin: 0 0 15px; }
.references li:target { background: #eef6ff; }
@media(max-width:700px) { main { padding: 24px 20px 40px; } h1 { font-size: 27px; } }
@media print { body { background: white; } main { padding: 0; box-shadow: none; max-width: none; } h2, h3 { break-after: avoid; } }
"""
html = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>TimberParse paper draft</title><style>{css}</style><script>window.MathJax={{tex:{{inlineMath:[['\\\\(','\\\\)']]}}}};</script><script defer src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js"></script></head><body><main><h1>TimberParse: Field-Level Accuracy and Uncertainty of Vision-Language Extraction from Timber Structural Plans</h1><div class="byline"><span class="placeholder">[PLACEHOLDER: Author names and affiliations]</span><br>Draft for study planning — 2 October 2026</div>{''.join(parts)}</main></body></html>'''
(HERE / "timberparse_draft_preview.html").write_text(html, encoding="utf-8")
print(HERE / "timberparse_draft_preview.html")
