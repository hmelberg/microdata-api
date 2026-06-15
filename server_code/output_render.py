"""Etterbehandling av MicroInterpreter.output_log -> ren tekst / HTML-dokument.

`output_log` er en liste strenger der tabeller og figurer er innpakket som
embeds:  MICRO_EMBED_START.format(type) \\n payload \\n MICRO_EMBED_END.
Observerte typer: ``tablehtml`` (rå HTML-tabell) og ``figure`` (Plotly-JSON).

Denne modulen splitter loggen i segmenter og produserer to leveranser:
  * to_text()  – ren tekst: figurer rendres som ASCII (datadrevet fra
                 Plotly-JSON, aldri via piksel-rendering — kun stdlib).
  * to_html()  – ett HTML-dokument: tabeller inline, og figurer som EKTE
                 interaktive Plotly-grafer (Plotly-JSON-en mates rett inn
                 i Plotly.js fra CDN — ingen server-side rendering).
"""

from __future__ import annotations

import html as _html
import json
import math
import re
from html.parser import HTMLParser

# Hold markørene i synk med m2py når den er importerbar; fall tilbake til de
# kjente konstantene slik at modulen kan enhetstestes frittstående.
try:  # pragma: no cover - trivielt
    from m2py import MICRO_EMBED_START, MICRO_EMBED_END  # type: ignore
except Exception:  # pragma: no cover
    MICRO_EMBED_START = "__micro_transform_start_{}__"
    MICRO_EMBED_END = "__micro_transform_end__"

_pre, _suf = MICRO_EMBED_START.split("{}")
_EMBED_RE = re.compile(
    re.escape(_pre) + r"(\w+)" + re.escape(_suf) + r"\n(.*?)\n" + re.escape(MICRO_EMBED_END),
    re.DOTALL,
)


# ─── Splitt loggen i segmenter ───────────────────────────────────────────────

def split_segments(joined: str):
    """Returner en ordnet liste av (kind, payload).

    kind == "text" for løpende tekst, ellers embed-typen ("tablehtml",
    "figure", …). Tomme tekstbiter hoppes over.
    """
    segs = []
    pos = 0
    for m in _EMBED_RE.finditer(joined):
        if m.start() > pos:
            txt = joined[pos:m.start()]
            if txt.strip():
                segs.append(("text", txt))
        segs.append((m.group(1), m.group(2)))
        pos = m.end()
    if pos < len(joined):
        txt = joined[pos:]
        if txt.strip():
            segs.append(("text", txt))
    return segs


# ─── HTML-tabell -> tekst (stdlib) ───────────────────────────────────────────

class _TableToText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _table_to_text(html_str: str) -> str:
    p = _TableToText()
    p.feed(html_str)
    rows = [r for r in p.rows if r]
    if not rows:
        return ""
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    widths = [max(len(r[c]) for r in rows) for c in range(ncol)]
    out = []
    for r in rows:
        cells = [r[0].ljust(widths[0])] + [r[c].rjust(widths[c]) for c in range(1, ncol)]
        out.append("  ".join(cells).rstrip())
    return "\n".join(out)


# ─── Plotly-JSON -> ASCII ────────────────────────────────────────────────────

_MARKS = "*o+x#@%"


def _fmt(v: float) -> str:
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.2f}"


def _nums(seq):
    out = []
    for v in seq or []:
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            out.append(None)
    return out


def _hbars(labels, vals, width=40, suffix=None):
    mx = max([v for v in vals if v is not None] or [1]) or 1
    lw = max((len(l) for l in labels), default=0)
    lines = []
    for i, (l, v) in enumerate(zip(labels, vals)):
        v = v or 0.0
        n = int(round(v / mx * width)) if mx else 0
        extra = suffix(i) if suffix else _fmt(v)
        lines.append(f"{l.rjust(lw)} | {'#' * n} {extra}")
    return "\n".join(lines)


def _render_xy(data, width, height):
    series = []
    for i, tr in enumerate(data):
        y = _nums(tr.get("y"))
        x = tr.get("x")
        x = _nums(x) if x is not None else list(range(len(y)))
        pts = [(a, b) for a, b in zip(x, y) if a is not None and b is not None]
        if not pts:
            continue
        series.append({"pts": sorted(pts), "mode": tr.get("mode") or "lines",
                       "char": _MARKS[i % len(_MARKS)], "name": tr.get("name") or f"trace{i}"})
    if not series:
        return "(ingen numeriske punkter)"

    xs = [p[0] for s in series for p in s["pts"]]
    ys = [p[1] for s in series for p in s["pts"]]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    if xmax == xmin:
        xmax = xmin + 1
    if ymax == ymin:
        ymax = ymin + 1

    grid = [[" "] * width for _ in range(height)]
    colof = lambda x: int(round((x - xmin) / (xmax - xmin) * (width - 1)))
    rowof = lambda y: int(round((ymax - y) / (ymax - ymin) * (height - 1)))

    for s in series:
        ch = s["char"]
        if "lines" in s["mode"]:
            for (x0, y0), (x1, y1) in zip(s["pts"], s["pts"][1:]):
                c0, c1 = colof(x0), colof(x1)
                if c0 == c1:
                    grid[rowof(y0)][c0] = ch
                    continue
                for c in range(min(c0, c1), max(c0, c1) + 1):
                    t = (c - c0) / (c1 - c0)
                    grid[rowof(y0 + (y1 - y0) * t)][c] = ch
        for (x, y) in s["pts"]:
            grid[rowof(y)][colof(x)] = ch

    ylw = max(len(_fmt(ymax)), len(_fmt(ymin)))
    out = []
    for r in range(height):
        lab = _fmt(ymax) if r == 0 else (_fmt(ymin) if r == height - 1 else "")
        out.append(lab.rjust(ylw) + " |" + "".join(grid[r]))
    out.append(" " * ylw + " +" + "-" * width)
    xmnl, xmxl = _fmt(xmin), _fmt(xmax)
    out.append(" " * (ylw + 2) + xmnl + " " * max(1, width - len(xmnl) - len(xmxl)) + xmxl)
    if len(series) > 1:
        out.append("  " + "   ".join(f"{s['char']} {s['name']}" for s in series))
    return "\n".join(out)


def _render_bars(data, width=40):
    tr = data[0]
    if tr.get("orientation") == "h":
        labels, values = tr.get("y"), _nums(tr.get("x"))
    else:
        labels, values = tr.get("x"), _nums(tr.get("y"))
    labels = [str(l) for l in (labels or range(len(values)))]
    return _hbars(labels, values, width)


def _render_histogram(data, bins=10, width=40):
    xs = [v for v in _nums(data[0].get("x") or data[0].get("y")) if v is not None]
    if not xs:
        return "(tomt histogram)"
    lo, hi = min(xs), max(xs)
    if hi == lo:
        hi = lo + 1
    step = (hi - lo) / bins
    counts = [0] * bins
    for v in xs:
        counts[min(bins - 1, int((v - lo) / step))] += 1
    labels = [f"{lo + i * step:.0f}-{lo + (i + 1) * step:.0f}" for i in range(bins)]
    return _hbars(labels, [float(c) for c in counts], width)


def _render_pie(data, width=40):
    tr = data[0]
    labels = [str(l) for l in (tr.get("labels") or [])]
    values = _nums(tr.get("values"))
    tot = sum(v for v in values if v) or 1
    return _hbars(labels, values, width,
                  suffix=lambda i: f"{(values[i] or 0) / tot * 100:.1f}%  ({_fmt(values[i] or 0)})")


def _render_box(data):
    lines = []
    for i, tr in enumerate(data):
        ys = sorted(v for v in _nums(tr.get("y") or tr.get("x")) if v is not None)
        if not ys:
            continue

        def q(p, ys=ys):
            if len(ys) == 1:
                return ys[0]
            idx = p * (len(ys) - 1)
            lo, hi = int(math.floor(idx)), int(math.ceil(idx))
            return ys[lo] + (ys[hi] - ys[lo]) * (idx - lo)

        name = tr.get("name") or f"box{i}"
        lines.append(f"{name}:  min={_fmt(ys[0])}  Q1={_fmt(q(.25))}  "
                     f"median={_fmt(q(.5))}  Q3={_fmt(q(.75))}  max={_fmt(ys[-1])}")
    return "\n".join(lines) or "(tom boksplott)"


def figure_to_ascii(fig_json, width: int = 64, height: int = 16) -> str:
    """Plotly pio.to_json (streng eller dict) -> ASCII-streng."""
    try:
        fig = fig_json if isinstance(fig_json, dict) else json.loads(fig_json)
    except (ValueError, TypeError):
        return "[figur: kunne ikke parse Plotly-JSON]"
    data = fig.get("data") or []
    if not data:
        return "(tom figur)"
    layout = fig.get("layout") or {}
    title = layout.get("title")
    if isinstance(title, dict):
        title = title.get("text", "")
    title = title or ""

    primary = (data[0].get("type") or "scatter").lower()
    if primary == "bar":
        body = _render_bars(data, width)
    elif primary == "histogram":
        body = _render_histogram(data, width=width)
    elif primary == "pie":
        body = _render_pie(data, width)
    elif primary == "box":
        body = _render_box(data)
    elif primary in ("scatter", "scattergl", "line"):
        body = _render_xy(data, width, height)
    else:
        body = f"[{primary}-figur: {len(data)} trace(s) - ingen ASCII-renderer]"
    return (f"{title}\n{'-' * max(len(title), 12)}\n{body}") if title else body


# ─── Tekst / HTML-sammenstilling ─────────────────────────────────────────────

def to_text(segments) -> str:
    parts = []
    for kind, payload in segments:
        if kind == "text":
            parts.append(payload.strip("\n"))
        elif kind == "tablehtml":
            parts.append(_table_to_text(payload))
        elif kind == "figure":
            parts.append(figure_to_ascii(payload))
        else:
            parts.append(f"[{kind}]")
    return "\n".join(p for p in parts if p)


_CSS = """
  body { font-family: 'Fira Code', Consolas, monospace; font-size: 13px;
         line-height: 1.5; margin: 1.2rem; color: #1a1a1a; background: #fff; }
  pre { margin: .4em 0; white-space: pre-wrap; word-break: break-word; }
  .output-table-wrap { max-width: 100%; max-height: 60vh; overflow: auto; margin: .4em 0; }
  table.output-table { border-collapse: collapse; font-size: 13px; }
  table.output-table th, table.output-table td {
    border: 1px solid #ccc; padding: 4px 8px; text-align: right; white-space: nowrap; }
  table.output-table th:first-child, table.output-table td:first-child { text-align: left; }
  table.output-table thead th { position: sticky; top: 0; background: #f2f2f2; }
  .m2py-plot { max-width: 100%; min-height: 420px; margin: .6em 0; }
  .m2py-error { color: #b00020; font-weight: 600; }
""".strip()

# Pin en konkret Plotly v2 (IKKE plotly-latest — den er frosset på v1).
_PLOTLY_CDN = "https://cdn.plot.ly/plotly-2.35.2.min.js"


def to_html(segments, title: str = "microdata-run") -> str:
    body = []
    fig_ids: list[str] = []
    for kind, payload in segments:
        if kind == "text":
            body.append(f"<pre>{_html.escape(payload.strip(chr(10)))}</pre>")
        elif kind == "tablehtml":
            body.append(f"<div class='output-table-wrap'>{payload}</div>")
        elif kind == "figure":
            # Ekte interaktiv graf: legg Plotly-JSON i et application/json-blokk
            # og la Plotly.js rendre den klientside. `</` escapes så payloaden
            # ikke kan lukke <script> (gyldig som JSON-escape inni strenger).
            div_id = f"m2py-fig-{len(fig_ids)}"
            safe = payload.replace("</", "<\\/")
            body.append(f"<div class='m2py-plot' id='{div_id}'></div>")
            body.append(f"<script type='application/json' id='{div_id}-data'>{safe}</script>")
            fig_ids.append(div_id)
        else:
            body.append(f"<pre>[{_html.escape(kind)}]</pre>")

    scripts = ""
    if fig_ids:
        renders = "\n".join(
            f"(function(){{var f=JSON.parse(document.getElementById('{d}-data').textContent);"
            f"Plotly.newPlot('{d}',f.data,f.layout||{{}},f.config||{{responsive:true}});}})();"
            for d in fig_ids
        )
        scripts = f"<script src=\"{_PLOTLY_CDN}\"></script>\n<script>{renders}</script>"

    return (
        "<!doctype html>\n<html lang=\"no\"><head><meta charset=\"utf-8\">"
        f"<title>{_html.escape(title)}</title><style>{_CSS}</style></head>"
        f"<body><main>{''.join(body)}</main>{scripts}</body></html>"
    )


def render(output, error: str | None = None) -> dict:
    """Hovedinngang: output_log (liste) eller ferdig joinet streng -> {text, html}."""
    if isinstance(output, (list, tuple)):
        output = "\n".join(str(x) for x in output)
    segs = split_segments(output or "")
    text = to_text(segs)
    html_doc = to_html(segs)
    if error:
        text = (text + ("\n\n" if text else "") + f"FEIL: {error}").strip()
        err_html = f"<pre class='m2py-error'>FEIL: {_html.escape(str(error))}</pre>"
        html_doc = html_doc.replace("</main>", err_html + "</main>")
    return {"text": text, "html": html_doc}
