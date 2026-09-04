#!/usr/bin/env python3
"""Render the parsed Apple Health JSON into a self-contained HTML dashboard.

No network, no CDN, no build step - one HTML file with inline SVG charts that
opens straight in a browser.

Usage:
    python3 build_dashboard.py health.json -o dashboard.html
"""

from __future__ import annotations

import argparse
import html
import json
import math
from datetime import date, datetime, timedelta
from statistics import mean, median, pstdev

# --------------------------------------------------------------------------
# Palette - the validated reference instance (see dataviz/references/palette.md).
# Light and dark are each selected sets, not an automatic flip.
# --------------------------------------------------------------------------
SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500"]

# Which metrics lead the summary, in order, and how they are drawn.
FEATURED = [
    "steps", "sleep_asleep", "resting_hr", "hrv",
    "active_energy", "exercise_minutes", "body_mass", "vo2max",
]

# Commonly cited adult reference ranges. Context for reading a chart - not a
# diagnosis, and not tuned to any individual.
REFERENCE = {
    "resting_hr": (60, 100, "typical adult resting range"),
    "spo2": (95, 100, "typical range at sea level"),
    "respiratory_rate": (12, 20, "typical adult resting range"),
    "sleep_asleep": (7, 9, "commonly recommended for adults"),
    "sleep_efficiency": (85, 100, "commonly cited as good efficiency"),
    "bp_systolic": (90, 120, "normal systolic"),
    "bp_diastolic": (60, 80, "normal diastolic"),
}

FORMATS = {
    "steps": ("{:,.0f}", ""),
    "distance": ("{:,.1f}", " km"),
    "active_energy": ("{:,.0f}", " kcal"),
    "basal_energy": ("{:,.0f}", " kcal"),
    "flights": ("{:,.0f}", ""),
    "exercise_minutes": ("{:,.0f}", " min"),
    "stand_minutes": ("{:,.0f}", " min"),
    "resting_hr": ("{:,.0f}", " bpm"),
    "heart_rate": ("{:,.0f}", " bpm"),
    "walking_hr": ("{:,.0f}", " bpm"),
    "hrv": ("{:,.0f}", " ms"),
    "respiratory_rate": ("{:,.1f}", " br/min"),
    "spo2": ("{:,.1f}", "%"),
    "body_mass": ("{:,.1f}", " kg"),
    "lean_mass": ("{:,.1f}", " kg"),
    "bmi": ("{:,.1f}", ""),
    "body_fat": ("{:,.1f}", "%"),
    "vo2max": ("{:,.1f}", ""),
    "sleep_efficiency": ("{:,.0f}", "%"),
    "bp_systolic": ("{:,.0f}", ""),
    "bp_diastolic": ("{:,.0f}", ""),
    "blood_glucose": ("{:,.0f}", " mg/dL"),
    "water": ("{:,.1f}", " L"),
}

STAGE_LABELS = [("deep", "Deep"), ("core", "Core"), ("rem", "REM"), ("awake", "Awake")]

E = html.escape


def esc(value):
    return E(str(value), quote=True)


# --------------------------------------------------------------------------
# Series helpers
# --------------------------------------------------------------------------

def scalar_series(daily, key):
    """Return {date: float} for a metric, flattening avg-style dict entries."""
    raw = daily.get(key) or {}
    out = {}
    for day, value in raw.items():
        if isinstance(value, dict):
            value = value.get("avg")
        if isinstance(value, (int, float)):
            out[day] = float(value)
    return dict(sorted(out.items()))


def date_axis(series_list):
    """Continuous day-by-day axis spanning every series handed in."""
    days = sorted({d for s in series_list for d in s})
    if not days:
        return []
    first = date.fromisoformat(days[0])
    last = date.fromisoformat(days[-1])
    out, cursor = [], first
    while cursor <= last:
        out.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return out


def rolling(axis, series, window=7, min_periods=3):
    """Centre-anchored trailing mean that skips missing days rather than zeroing."""
    out, buf = [], []
    for day in axis:
        buf.append(series.get(day))
        if len(buf) > window:
            buf.pop(0)
        present = [v for v in buf if v is not None]
        out.append(mean(present) if len(present) >= min_periods else None)
    return out


def fmt(key, value, unit=""):
    if value is None:
        return "-"
    if key in ("sleep_asleep", "sleep_in_bed") or unit == "h":
        total = int(round(value * 60))
        return f"{total // 60}h {total % 60:02d}m"
    pattern, suffix = FORMATS.get(key, ("{:,.1f}", f" {unit}" if unit else ""))
    return pattern.format(value) + suffix


def window_mean(series, days, end=None):
    """Mean over the last `days` calendar days of available data."""
    if not series:
        return None
    keys = sorted(series)
    last = date.fromisoformat(end or keys[-1])
    start = last - timedelta(days=days - 1)
    vals = [v for d, v in series.items() if start <= date.fromisoformat(d) <= last]
    return mean(vals) if vals else None


def nice_ticks(lo, hi, count=4):
    if hi <= lo:
        hi = lo + 1
    span = hi - lo
    raw = span / max(1, count)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    step = min((m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw), default=mag * 10)
    start = math.floor(lo / step) * step
    ticks = []
    value = start
    while value <= hi + step * 0.5:
        if value >= lo - step * 0.5:
            ticks.append(value)
        value += step
    return ticks


# --------------------------------------------------------------------------
# SVG chart builders. Each returns a <figure> block.
# All geometry is in viewBox units; CSS scales the SVG to its container.
# --------------------------------------------------------------------------

W, H = 820, 250
PAD_L, PAD_R, PAD_T, PAD_B = 52, 14, 18, 30
PLOT_W = W - PAD_L - PAD_R
PLOT_H = H - PAD_T - PAD_B


def axis_labels(axis, count=6):
    """Evenly spaced date ticks, formatted short."""
    if not axis:
        return []
    step = max(1, len(axis) // count)
    out = []
    for i in range(0, len(axis), step):
        day = date.fromisoformat(axis[i])
        out.append((i, day.strftime("%d %b")))
    return out


def grid_and_axes(ticks, lo, hi, axis, unit_hint=""):
    parts = []
    for tick in ticks:
        y = PAD_T + PLOT_H - (tick - lo) / (hi - lo) * PLOT_H
        parts.append(f'<line class="grid" x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}"/>')
        label = f"{tick:,.0f}" if abs(tick) >= 10 or tick == int(tick) else f"{tick:,.1f}"
        parts.append(f'<text class="tick" x="{PAD_L - 8}" y="{y + 3.5:.1f}" text-anchor="end">{label}</text>')
    for i, label in axis_labels(axis):
        x = PAD_L + (i / max(1, len(axis) - 1)) * PLOT_W
        parts.append(f'<text class="tick" x="{x:.1f}" y="{H - PAD_B + 18}" text-anchor="middle">{label}</text>')
    return "".join(parts)


def line_chart(cid, title, subtitle, axis, series, key, unit, ref=None):
    """Emphasis form: raw daily recedes to grey, the 7-day average carries the story."""
    values = [series.get(d) for d in axis]
    present = [v for v in values if v is not None]
    if len(present) < 2:
        return ""
    avg = rolling(axis, series)

    lo, hi = min(present), max(present)
    if ref:
        lo, hi = min(lo, ref[0]), max(hi, ref[1])
    pad = (hi - lo) * 0.12 or 1
    lo, hi = lo - pad, hi + pad
    ticks = nice_ticks(lo, hi)
    if ticks:
        lo, hi = min(lo, ticks[0]), max(hi, ticks[-1])

    def px(i):
        return PAD_L + (i / max(1, len(axis) - 1)) * PLOT_W

    def py(v):
        return PAD_T + PLOT_H - (v - lo) / (hi - lo) * PLOT_H

    body = [grid_and_axes(ticks, lo, hi, axis, unit)]

    # Reference band sits behind the data, unlabelled by colour alone.
    if ref:
        y1, y2 = py(ref[1]), py(ref[0])
        body.append(f'<rect class="refband" x="{PAD_L}" y="{y1:.1f}" '
                    f'width="{PLOT_W}" height="{max(1, y2 - y1):.1f}"/>')

    # Raw daily line, broken across gaps of 2+ days so absence isn't interpolated.
    seg, segments = [], []
    for i, v in enumerate(values):
        if v is None:
            if len(seg) > 1:
                segments.append(seg)
            seg = []
        else:
            seg.append((px(i), py(v)))
    if len(seg) > 1:
        segments.append(seg)
    for s in segments:
        d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in s)
        body.append(f'<path class="raw" d="{d}"/>')

    seg, segments = [], []
    for i, v in enumerate(avg):
        if v is None:
            if len(seg) > 1:
                segments.append(seg)
            seg = []
        else:
            seg.append((px(i), py(v)))
    if len(seg) > 1:
        segments.append(seg)
    for s in segments:
        d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in s)
        body.append(f'<path class="avg" d="{d}"/>')

    body.append(f'<line class="crosshair" id="{cid}-cross" x1="0" y1="{PAD_T}" '
                f'x2="0" y2="{PAD_T + PLOT_H}" style="display:none"/>')
    body.append(f'<circle class="dot" id="{cid}-dot" r="4" style="display:none"/>')
    body.append(f'<rect class="hit" x="{PAD_L}" y="{PAD_T}" width="{PLOT_W}" height="{PLOT_H}"/>')

    points = [
        {"x": round(px(i), 1), "y": round(py(v), 1), "d": axis[i],
         "v": fmt(key, v, unit),
         "a": fmt(key, avg[i], unit) if avg[i] is not None else None}
        for i, v in enumerate(values) if v is not None
    ]

    ref_note = (f'<span class="chip">Shaded band: {esc(ref[2])} '
                f'({fmt(key, ref[0], unit)}-{fmt(key, ref[1], unit)})</span>') if ref else ""

    return f"""
<figure class="chart" id="{cid}">
  <figcaption>
    <h3>{esc(title)}</h3>
    <p class="sub">{esc(subtitle)}</p>
  </figcaption>
  <div class="keyrow">
    <span class="chip"><i class="sw sw-raw"></i>Daily value</span>
    <span class="chip"><i class="sw sw-avg"></i>7-day average</span>
    {ref_note}
  </div>
  <svg viewBox="0 0 {W} {H}" class="plot" data-points='{json.dumps(points)}'
       role="img" aria-label="{esc(title)} over time">
    {''.join(body)}
  </svg>
</figure>"""


def stacked_bar(cid, title, subtitle, axis, stages):
    """Sleep stages. Four categorical slots, direct-labelled with their averages."""
    days = [d for d in axis if d in stages]
    if len(days) < 2:
        return ""
    totals = [sum(stages[d].get(s, 0.0) for s, _ in STAGE_LABELS) for d in days]
    hi = max(totals) * 1.12 or 1
    ticks = nice_ticks(0, hi, 4)
    hi = max(hi, ticks[-1] if ticks else hi)

    bw = PLOT_W / max(1, len(days))
    bar_w = max(1.0, bw - (2.0 if bw > 5 else 0.4))   # 2px surface gap between bars
    body = [grid_and_axes(ticks, 0, hi, days, "h")]

    means = {}
    for si, (stage, label) in enumerate(STAGE_LABELS):
        vals = [stages[d].get(stage, 0.0) for d in days]
        means[stage] = mean(vals) if vals else 0.0

    for i, day in enumerate(days):
        x = PAD_L + i * bw
        cursor = 0.0
        tip = [f"{day}"]
        for stage, label in STAGE_LABELS:
            tip.append(f"{label}: {fmt('', stages[day].get(stage, 0.0), 'h')}")
        tip.append(f"Total: {fmt('', totals[i], 'h')}")
        for si, (stage, label) in enumerate(STAGE_LABELS):
            v = stages[day].get(stage, 0.0)
            if v <= 0:
                continue
            y0 = PAD_T + PLOT_H - (cursor + v) / hi * PLOT_H
            y1 = PAD_T + PLOT_H - cursor / hi * PLOT_H
            height = max(0.6, y1 - y0 - 1.2)          # 2px-equivalent segment gap
            body.append(
                f'<rect class="seg s{si + 1}" x="{x:.1f}" y="{y0:.1f}" '
                f'width="{bar_w:.1f}" height="{height:.1f}" rx="1" '
                f'data-tip="{esc(" | ".join(tip))}"/>'
            )
            cursor += v

    legend = "".join(
        f'<span class="chip"><i class="sw s{i + 1}"></i>{esc(label)} '
        f'<b>{fmt("", means[stage], "h")}</b> avg</span>'
        for i, (stage, label) in enumerate(STAGE_LABELS)
    )

    return f"""
<figure class="chart wide" id="{cid}">
  <figcaption>
    <h3>{esc(title)}</h3>
    <p class="sub">{esc(subtitle)}</p>
  </figcaption>
  <div class="keyrow">{legend}</div>
  <svg viewBox="0 0 {W} {H}" class="plot bars" role="img" aria-label="{esc(title)}">
    {''.join(body)}
  </svg>
</figure>"""


def histogram(cid, title, subtitle, values, key, unit, bins=14):
    vals = [v for v in values if v is not None]
    if len(vals) < 5:
        return ""
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        hi = lo + 1
    width = (hi - lo) / bins
    counts = [0] * bins
    for v in vals:
        idx = min(bins - 1, int((v - lo) / width))
        counts[idx] += 1
    top = max(counts) * 1.15 or 1
    ticks = nice_ticks(0, top, 3)

    bw = PLOT_W / bins
    body = [grid_and_axes(ticks, 0, max(top, ticks[-1] if ticks else top), [], "")]
    med = median(vals)
    for i, c in enumerate(counts):
        if c == 0:
            continue
        h = c / max(top, ticks[-1] if ticks else top) * PLOT_H
        x = PAD_L + i * bw
        y = PAD_T + PLOT_H - h
        lo_b, hi_b = lo + i * width, lo + (i + 1) * width
        body.append(
            f'<rect class="seg s1" x="{x + 1:.1f}" y="{y:.1f}" '
            f'width="{max(1.0, bw - 2):.1f}" height="{h:.1f}" rx="2" '
            f'data-tip="{esc(f"{fmt(key, lo_b, unit)} - {fmt(key, hi_b, unit)}: {c} day(s)")}"/>'
        )
    # x labels at both ends and the median
    for frac, label in ((0.0, fmt(key, lo, unit)), (1.0, fmt(key, hi, unit))):
        x = PAD_L + frac * PLOT_W
        anchor = "start" if frac == 0.0 else "end"
        body.append(f'<text class="tick" x="{x:.1f}" y="{H - PAD_B + 18}" text-anchor="{anchor}">{esc(label)}</text>')
    mx = PAD_L + (med - lo) / (hi - lo) * PLOT_W
    body.append(f'<line class="median" x1="{mx:.1f}" y1="{PAD_T}" x2="{mx:.1f}" y2="{PAD_T + PLOT_H}"/>')
    body.append(f'<text class="tick strong" x="{mx:.1f}" y="{PAD_T - 5}" text-anchor="middle">median {esc(fmt(key, med, unit))}</text>')

    return f"""
<figure class="chart" id="{cid}">
  <figcaption><h3>{esc(title)}</h3><p class="sub">{esc(subtitle)}</p></figcaption>
  <svg viewBox="0 0 {W} {H}" class="plot bars" role="img" aria-label="{esc(title)}">
    {''.join(body)}
  </svg>
</figure>"""


def category_bar(cid, title, subtitle, pairs, key, unit, note=""):
    """Horizontal bars, sequential single hue - magnitude, not identity."""
    if not pairs:
        return ""
    top = max(v for _, v in pairs) or 1
    row_h = 30
    height = PAD_T + len(pairs) * row_h + 24
    label_w = 150
    body = []
    for i, (label, value) in enumerate(pairs):
        y = PAD_T + i * row_h
        w = (value / top) * (W - label_w - 90)
        body.append(f'<text class="tick end" x="{label_w - 10}" y="{y + 19}" text-anchor="end">{esc(label)}</text>')
        body.append(
            f'<rect class="seg s1" x="{label_w}" y="{y + 6}" width="{max(2.0, w):.1f}" '
            f'height="18" rx="4" data-tip="{esc(f"{label}: {fmt(key, value, unit)}")}"/>'
        )
        body.append(f'<text class="tick strong" x="{label_w + max(2.0, w) + 8:.1f}" y="{y + 19}">'
                    f'{esc(fmt(key, value, unit))}</text>')
    return f"""
<figure class="chart" id="{cid}">
  <figcaption><h3>{esc(title)}</h3><p class="sub">{esc(subtitle)}</p></figcaption>
  <svg viewBox="0 0 {W} {height}" class="plot bars" role="img" aria-label="{esc(title)}">
    {''.join(body)}
  </svg>
  {f'<p class="note">{esc(note)}</p>' if note else ''}
</figure>"""


def scatter(cid, title, subtitle, pairs, xlabel, ylabel, xkey, ykey, xunit, yunit):
    if len(pairs) < 8:
        return ""
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    xlo, xhi = min(xs), max(xs)
    ylo, yhi = min(ys), max(ys)
    xpad, ypad = (xhi - xlo) * 0.08 or 1, (yhi - ylo) * 0.08 or 1
    xlo, xhi, ylo, yhi = xlo - xpad, xhi + xpad, ylo - ypad, yhi + ypad
    yticks = nice_ticks(ylo, yhi)

    body = []
    for tick in yticks:
        y = PAD_T + PLOT_H - (tick - ylo) / (yhi - ylo) * PLOT_H
        body.append(f'<line class="grid" x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}"/>')
        body.append(f'<text class="tick" x="{PAD_L - 8}" y="{y + 3.5:.1f}" text-anchor="end">{tick:,.0f}</text>')

    for x, y, label in pairs:
        cx = PAD_L + (x - xlo) / (xhi - xlo) * PLOT_W
        cy = PAD_T + PLOT_H - (y - ylo) / (yhi - ylo) * PLOT_H
        body.append(
            f'<circle class="pt" cx="{cx:.1f}" cy="{cy:.1f}" r="4.5" '
            f'data-tip="{esc(f"{label} | {xlabel}: {fmt(xkey, x, xunit)} | {ylabel}: {fmt(ykey, y, yunit)}")}"/>'
        )

    # Least-squares fit and Pearson r, both reported with the sample size.
    n = len(xs)
    mx, my = mean(xs), mean(ys)
    sxx = sum((v - mx) ** 2 for v in xs)
    sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    syy = sum((v - my) ** 2 for v in ys)
    r = sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else 0.0
    if sxx > 0:
        slope = sxy / sxx
        intercept = my - slope * mx
        x1, x2 = xlo, xhi
        y1, y2 = intercept + slope * x1, intercept + slope * x2
        y1c, y2c = max(min(y1, yhi), ylo), max(min(y2, yhi), ylo)
        px1 = PAD_L + (x1 - xlo) / (xhi - xlo) * PLOT_W
        px2 = PAD_L + (x2 - xlo) / (xhi - xlo) * PLOT_W
        py1 = PAD_T + PLOT_H - (y1c - ylo) / (yhi - ylo) * PLOT_H
        py2 = PAD_T + PLOT_H - (y2c - ylo) / (yhi - ylo) * PLOT_H
        body.append(f'<line class="fit" x1="{px1:.1f}" y1="{py1:.1f}" x2="{px2:.1f}" y2="{py2:.1f}"/>')

    for frac, val in ((0.0, xlo), (1.0, xhi)):
        x = PAD_L + frac * PLOT_W
        anchor = "start" if frac == 0.0 else "end"
        body.append(f'<text class="tick" x="{x:.1f}" y="{H - PAD_B + 18}" text-anchor="{anchor}">'
                    f'{esc(fmt(xkey, val, xunit))}</text>')

    strength = ("no meaningful" if abs(r) < 0.2 else
                "a weak" if abs(r) < 0.4 else
                "a moderate" if abs(r) < 0.6 else "a strong")
    verdict = (f"Pearson r = {r:+.2f} over {n} paired days - {strength} linear relationship. "
               "Association is not cause; both move with things this export can't see.")

    return f"""
<figure class="chart wide" id="{cid}">
  <figcaption><h3>{esc(title)}</h3><p class="sub">{esc(subtitle)}</p></figcaption>
  <svg viewBox="0 0 {W} {H}" class="plot bars" role="img" aria-label="{esc(title)}">
    {''.join(body)}
  </svg>
  <p class="note">{esc(verdict)} Horizontal: {esc(xlabel)}. Vertical: {esc(ylabel)}.</p>
</figure>"""


def stat_tile(key, label, series, unit, direction):
    if not series:
        return ""
    keys = sorted(series)
    latest_day = keys[-1]
    latest = series[latest_day]
    recent = window_mean(series, 30)
    prior_end = (date.fromisoformat(latest_day) - timedelta(days=30)).isoformat()
    prior = window_mean(series, 30, end=prior_end)

    delta_html = '<span class="delta flat">no prior window</span>'
    if recent is not None and prior is not None and prior != 0:
        pct = (recent - prior) / abs(prior) * 100.0
        if abs(pct) < 1.0:
            delta_html = '<span class="delta flat">&#8226; holding steady vs prior 30 days</span>'
        else:
            rising = pct > 0
            good = None if direction is None else (rising == (direction == "up"))
            cls = "flat" if good is None else ("good" if good else "bad")
            arrow = "&#9650;" if rising else "&#9660;"
            word = "up" if rising else "down"
            delta_html = (f'<span class="delta {cls}">{arrow} {word} {abs(pct):.1f}% '
                          f'vs prior 30 days</span>')

    # Sparkline over the trailing 60 days of the axis.
    spark = ""
    tail_axis = date_axis([series])[-60:]
    pts = [(i, series.get(d)) for i, d in enumerate(tail_axis)]
    have = [(i, v) for i, v in pts if v is not None]
    if len(have) > 2:
        lo = min(v for _, v in have)
        hi = max(v for _, v in have)
        rng = (hi - lo) or 1
        coords = [(6 + i / max(1, len(tail_axis) - 1) * 148,
                   34 - (v - lo) / rng * 26) for i, v in have]
        d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in coords)
        spark = (f'<svg class="spark" viewBox="0 0 160 40" aria-hidden="true">'
                 f'<path class="avg" d="{d}"/></svg>')

    ref_note = ""
    if key in REFERENCE and recent is not None:
        lo_r, hi_r, desc = REFERENCE[key]
        inside = lo_r <= recent <= hi_r
        mark = "&#10003; within" if inside else "&#9679; outside"
        ref_note = (f'<p class="ref">{mark} {esc(desc)} '
                    f'({fmt(key, lo_r, unit)}-{fmt(key, hi_r, unit)})</p>')

    return f"""
<div class="tile">
  <p class="tl">{esc(label)}</p>
  <p class="tv">{esc(fmt(key, recent if recent is not None else latest, unit))}</p>
  <p class="tm">30-day average &middot; latest {esc(fmt(key, latest, unit))} on {esc(latest_day)}</p>
  {delta_html}
  {spark}
  {ref_note}
</div>"""


def table_view(title, axis, columns):
    """Accessible fallback: every charted number, as text."""
    heads = "".join(f"<th>{esc(c[0])}</th>" for c in columns)
    rows = []
    for day in reversed(axis):
        if not any(c[1].get(day) is not None for c in columns):
            continue
        cells = "".join(f"<td>{esc(c[2](c[1].get(day)))}</td>" for c in columns)
        rows.append(f"<tr><td>{esc(day)}</td>{cells}</tr>")
        if len(rows) >= 500:
            break
    return f"""
<details class="tableview">
  <summary>Table view &mdash; {esc(title)} ({len(rows)} rows, newest first)</summary>
  <div class="tablewrap">
    <table><thead><tr><th>Date</th>{heads}</tr></thead><tbody>{''.join(rows)}</tbody></table>
  </div>
</details>"""


# --------------------------------------------------------------------------
# Page assembly
# --------------------------------------------------------------------------

CSS = """
:root { color-scheme: light dark; }
.viz-root {
  --surface-0:#f6f5f2; --surface-1:#fcfcfb; --border:#e2e0da;
  --text-primary:#0b0b0b; --text-secondary:#52514e; --text-muted:#78766f;
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100;
  --raw:#b9b7b0; --grid:#eceae4; --band:rgba(42,120,214,.09);
  --good:#0ca30c; --critical:#d03b3b;
  background:var(--surface-0); color:var(--text-primary);
  font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  min-height:100vh; padding:32px 24px 64px;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .viz-root {
    --surface-0:#111110; --surface-1:#1a1a19; --border:#33322e;
    --text-primary:#ffffff; --text-secondary:#c3c2b7; --text-muted:#8e8c83;
    --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500;
    --raw:#4f4e49; --grid:#262521; --band:rgba(57,135,229,.14);
  }
}
:root[data-theme="dark"] .viz-root {
  --surface-0:#111110; --surface-1:#1a1a19; --border:#33322e;
  --text-primary:#ffffff; --text-secondary:#c3c2b7; --text-muted:#8e8c83;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500;
  --raw:#4f4e49; --grid:#262521; --band:rgba(57,135,229,.14);
}
.wrap { max-width:1180px; margin:0 auto; }
header.top { margin-bottom:28px; }
h1 { font-size:26px; margin:0 0 6px; letter-spacing:-.02em; }
.lede { color:var(--text-secondary); margin:0; max-width:70ch; }
.badge { display:inline-block; background:#fab219; color:#241c00; font-weight:700;
  padding:3px 9px; border-radius:5px; font-size:12px; margin-bottom:10px; }
h2 { font-size:13px; text-transform:uppercase; letter-spacing:.09em;
  color:var(--text-muted); margin:40px 0 14px; font-weight:600; }
.hero { background:var(--surface-1); border:1px solid var(--border); border-radius:12px;
  padding:22px 24px; margin-bottom:18px; }
.hero .n { font-size:52px; font-weight:650; letter-spacing:-.03em; line-height:1.05; }
.hero .c { color:var(--text-secondary); margin:6px 0 0; max-width:75ch; }
.tiles { display:grid; gap:12px; grid-template-columns:repeat(auto-fill,minmax(232px,1fr)); }
.tile { background:var(--surface-1); border:1px solid var(--border); border-radius:11px; padding:14px 16px; }
.tl { margin:0; font-size:12px; color:var(--text-muted); text-transform:uppercase; letter-spacing:.05em; }
.tv { margin:5px 0 2px; font-size:27px; font-weight:640; letter-spacing:-.02em; }
.tm { margin:0 0 7px; font-size:11.5px; color:var(--text-muted); }
.delta { font-size:12px; font-weight:600; display:inline-block; }
.delta.good { color:var(--good); } .delta.bad { color:var(--critical); }
.delta.flat { color:var(--text-muted); font-weight:500; }
.ref { margin:7px 0 0; font-size:11.5px; color:var(--text-secondary); }
.spark { display:block; width:100%; height:34px; margin-top:8px; overflow:visible; }
.grid2 { display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(420px,1fr)); }
.chart { background:var(--surface-1); border:1px solid var(--border); border-radius:12px;
  padding:16px 18px 12px; margin:0; }
.chart.wide { grid-column:1/-1; }
figcaption h3 { margin:0; font-size:15px; font-weight:620; }
figcaption .sub { margin:3px 0 0; font-size:12.5px; color:var(--text-secondary); }
.keyrow { display:flex; flex-wrap:wrap; gap:8px 14px; margin:11px 0 4px; }
.chip { font-size:11.5px; color:var(--text-secondary); display:inline-flex; align-items:center; gap:5px; }
.chip b { color:var(--text-primary); font-weight:640; }
.sw { width:11px; height:11px; border-radius:3px; display:inline-block; background:var(--s1); }
.sw-raw { background:var(--raw); } .sw-avg { background:var(--s1); }
.sw.s1{background:var(--s1)} .sw.s2{background:var(--s2)}
.sw.s3{background:var(--s3)} .sw.s4{background:var(--s4)}
svg.plot { width:100%; height:auto; display:block; overflow:visible; }
.grid { stroke:var(--grid); stroke-width:1; }
.tick { fill:var(--text-muted); font-size:11px; }
.tick.strong { fill:var(--text-secondary); font-weight:600; }
.raw { fill:none; stroke:var(--raw); stroke-width:1.25; stroke-linejoin:round; }
.avg { fill:none; stroke:var(--s1); stroke-width:2; stroke-linecap:round; stroke-linejoin:round; }
.refband { fill:var(--band); }
.median { stroke:var(--s2); stroke-width:2; stroke-dasharray:4 3; }
.fit { stroke:var(--s2); stroke-width:2; stroke-dasharray:5 4; }
.pt { fill:var(--s1); fill-opacity:.72; stroke:var(--surface-1); stroke-width:1.5; }
.seg { stroke:var(--surface-1); stroke-width:0; }
.seg.s1{fill:var(--s1)} .seg.s2{fill:var(--s2)}
.seg.s3{fill:var(--s3)} .seg.s4{fill:var(--s4)}
.seg:hover { filter:brightness(1.12); }
.crosshair { stroke:var(--text-muted); stroke-width:1; stroke-dasharray:3 3; }
.dot { fill:var(--s1); stroke:var(--surface-1); stroke-width:2; }
.hit { fill:transparent; }
.note { font-size:12px; color:var(--text-secondary); margin:10px 0 4px; }
.tip { position:fixed; z-index:50; pointer-events:none; background:var(--surface-1);
  border:1px solid var(--border); border-radius:7px; padding:7px 10px; font-size:12px;
  color:var(--text-primary); box-shadow:0 6px 20px rgba(0,0,0,.16); display:none; max-width:280px; }
.tip b { display:block; color:var(--text-muted); font-weight:500; font-size:11px; margin-bottom:2px; }
.tableview { margin:20px 0 0; }
.tableview summary { cursor:pointer; font-size:12.5px; color:var(--text-secondary); padding:7px 0; }
.tablewrap { overflow:auto; max-height:340px; border:1px solid var(--border); border-radius:8px; }
table { border-collapse:collapse; width:100%; font-size:12px; }
th,td { padding:5px 10px; text-align:right; border-bottom:1px solid var(--border); white-space:nowrap; }
th:first-child,td:first-child { text-align:left; position:sticky; left:0; background:var(--surface-1); }
thead th { position:sticky; top:0; background:var(--surface-1); color:var(--text-muted);
  font-weight:600; text-transform:uppercase; font-size:10.5px; letter-spacing:.05em; }
.panel { background:var(--surface-1); border:1px solid var(--border); border-radius:12px; padding:18px 20px; }
.panel ul { margin:0; padding-left:19px; } .panel li { margin:5px 0; color:var(--text-secondary); }
.panel li b { color:var(--text-primary); }
footer { margin-top:44px; padding-top:18px; border-top:1px solid var(--border);
  color:var(--text-muted); font-size:12px; max-width:80ch; }
"""

JS = """
(function(){
  var tip=document.createElement('div'); tip.className='tip'; document.body.appendChild(tip);
  function show(x,y,html){ tip.innerHTML=html; tip.style.display='block';
    var r=tip.getBoundingClientRect(); var left=x+14, top=y-r.height-12;
    if(left+r.width>innerWidth-8) left=x-r.width-14;
    if(top<8) top=y+18;
    tip.style.left=left+'px'; tip.style.top=top+'px'; }
  function hide(){ tip.style.display='none'; }

  // Crosshair + nearest-point tooltip on every line chart.
  document.querySelectorAll('svg.plot[data-points]').forEach(function(svg){
    var pts; try { pts=JSON.parse(svg.getAttribute('data-points')); } catch(e){ return; }
    if(!pts || !pts.length) return;
    var id=svg.closest('figure').id;
    var cross=document.getElementById(id+'-cross'), dot=document.getElementById(id+'-dot');
    svg.addEventListener('mousemove', function(ev){
      var ctm=svg.getScreenCTM(); if(!ctm) return;
      var p=svg.createSVGPoint(); p.x=ev.clientX; p.y=ev.clientY;
      var loc=p.matrixTransform(ctm.inverse());
      var best=pts[0], bd=Math.abs(pts[0].x-loc.x);
      for(var i=1;i<pts.length;i++){ var d=Math.abs(pts[i].x-loc.x); if(d<bd){bd=d;best=pts[i];} }
      if(cross){ cross.setAttribute('x1',best.x); cross.setAttribute('x2',best.x); cross.style.display=''; }
      if(dot){ dot.setAttribute('cx',best.x); dot.setAttribute('cy',best.y); dot.style.display=''; }
      var sp=svg.createSVGPoint(); sp.x=best.x; sp.y=best.y;
      var scr=sp.matrixTransform(ctm);
      show(scr.x, scr.y, '<b>'+best.d+'</b>'+best.v+(best.a?' &middot; 7-day avg '+best.a:''));
    });
    svg.addEventListener('mouseleave', function(){
      hide(); if(cross) cross.style.display='none'; if(dot) dot.style.display='none';
    });
  });

  // Per-mark tooltip on bars, cells and points.
  document.querySelectorAll('[data-tip]').forEach(function(el){
    el.addEventListener('mousemove', function(ev){
      var parts=el.getAttribute('data-tip').split(' | ');
      show(ev.clientX, ev.clientY, '<b>'+parts[0]+'</b>'+parts.slice(1).join('<br>'));
    });
    el.addEventListener('mouseleave', hide);
  });
})();
"""


def build(data, fixture=False):
    daily = data["daily"]
    defs = data["metrics"]
    meta = data["meta"]

    series = {k: scalar_series(daily, k) for k in defs if k != "sleep_stages"}
    series = {k: v for k, v in series.items() if v}
    axis = date_axis(list(series.values()))

    # ---- headline ------------------------------------------------------
    rng = meta.get("date_range", ["?", "?"])
    days_data = meta.get("days_with_data", 0)
    days_span = meta.get("days_spanned", 0)
    coverage = (days_data / days_span * 100.0) if days_span else 0.0

    tiles = "".join(
        stat_tile(k, defs[k]["label"], series[k], defs[k].get("unit", ""), defs[k].get("direction"))
        for k in FEATURED if k in series
    )
    rest = [k for k in series if k not in FEATURED and k not in ("sleep_in_bed",)]
    tiles += "".join(
        stat_tile(k, defs[k]["label"], series[k], defs[k].get("unit", ""), defs[k].get("direction"))
        for k in sorted(rest, key=lambda k: defs[k]["label"])
    )

    # ---- narrative summary ---------------------------------------------
    lines = []
    for k in ("steps", "sleep_asleep", "resting_hr", "hrv", "active_energy", "body_mass"):
        if k not in series:
            continue
        s = series[k]
        recent = window_mean(s, 30)
        last_day = sorted(s)[-1]
        prior = window_mean(s, 30, end=(date.fromisoformat(last_day) - timedelta(days=30)).isoformat())
        unit = defs[k].get("unit", "")
        if recent is None:
            continue
        if prior and prior != 0:
            pct = (recent - prior) / abs(prior) * 100.0
            trend = ("essentially flat" if abs(pct) < 1
                     else f"{'up' if pct > 0 else 'down'} {abs(pct):.1f}%")
            lines.append(f"<li><b>{esc(defs[k]['label'])}</b> averaged "
                         f"{esc(fmt(k, recent, unit))} over the last 30 days &mdash; {esc(trend)} "
                         f"against the 30 days before.</li>")
        else:
            lines.append(f"<li><b>{esc(defs[k]['label'])}</b> averaged "
                         f"{esc(fmt(k, recent, unit))} over the last 30 days.</li>")

    hero_metric = "steps" if "steps" in series else (sorted(series)[0] if series else None)
    hero_html = ""
    if hero_metric:
        v = window_mean(series[hero_metric], 30)
        hero_html = f"""
<div class="hero">
  <div class="n">{esc(fmt(hero_metric, v, defs[hero_metric].get('unit', '')))}</div>
  <p class="c">{esc(defs[hero_metric]['label'])}, averaged over the last 30 days of your export.
  Across the whole file: <b>{days_data:,}</b> days carrying data out of <b>{days_span:,}</b>
  calendar days ({coverage:.0f}% coverage), {meta.get('total_records', 0):,} raw samples,
  {len(defs)} distinct metrics.</p>
  <ul style="margin:12px 0 0;padding-left:19px;color:var(--text-secondary)">{''.join(lines)}</ul>
</div>"""

    # ---- trend charts ---------------------------------------------------
    charts = []
    trend_order = [k for k in FEATURED if k in series] + \
                  [k for k in sorted(series) if k not in FEATURED and k != "sleep_in_bed"]
    for k in trend_order:
        d = defs[k]
        sub_bits = [f"{len(series[k])} days with data", f"{d.get('samples', 0):,} samples"]
        charts.append(line_chart(
            f"c-{k}", d["label"], " &middot; ".join(sub_bits).replace("&middot;", "·"),
            axis, series[k], k, d.get("unit", ""), REFERENCE.get(k)))
    charts = [c for c in charts if c]

    # ---- sleep ----------------------------------------------------------
    sleep_blocks = []
    if "sleep_stages" in daily:
        stages = daily["sleep_stages"]
        sleep_blocks.append(stacked_bar(
            "c-stages", "Sleep composition by night",
            "Stacked stages per night, attributed to the morning you woke up. "
            "Overlapping records from multiple devices are unioned, not summed.",
            axis, stages))
    if "sleep_asleep" in series:
        sleep_blocks.append(histogram(
            "c-sleephist", "How long you actually sleep",
            "Distribution of nightly asleep time across the export.",
            list(series["sleep_asleep"].values()), "sleep_asleep", "h"))
    if "sleep_efficiency" in series:
        sleep_blocks.append(histogram(
            "c-effhist", "Sleep efficiency",
            "Asleep time as a share of time in bed, per night.",
            list(series["sleep_efficiency"].values()), "sleep_efficiency", "%"))
    sleep_blocks = [b for b in sleep_blocks if b]

    # ---- weekday pattern + workouts -------------------------------------
    pattern_blocks = []
    if "steps" in series:
        buckets = {i: [] for i in range(7)}
        for day, v in series["steps"].items():
            buckets[date.fromisoformat(day).weekday()].append(v)
        names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        pairs = [(names[i], mean(buckets[i])) for i in range(7) if buckets[i]]
        pattern_blocks.append(category_bar(
            "c-dow", "Steps by day of week", "Mean steps for each weekday across the whole export.",
            pairs, "steps", "count"))
    workouts = data.get("workouts") or []
    if workouts:
        by_type = {}
        for w in workouts:
            by_type.setdefault(w["type"], []).append(w["minutes"])
        pairs = sorted(((k, sum(v)) for k, v in by_type.items()), key=lambda x: -x[1])
        note = ""
        if len(pairs) > 7:
            tail = sum(v for _, v in pairs[7:])
            pairs = pairs[:7] + [("Other", tail)]
            note = "Everything past the top 7 activities is folded into 'Other'."
        pattern_blocks.append(category_bar(
            "c-workouts", "Workout minutes by activity",
            f"{len(workouts):,} logged workouts across the export.",
            pairs, "exercise_minutes", "min", note))
    pattern_blocks = [b for b in pattern_blocks if b]

    # ---- relationship ---------------------------------------------------
    rel_blocks = []
    if "sleep_asleep" in series and "resting_hr" in series:
        pairs = []
        for day, hours in series["sleep_asleep"].items():
            nxt = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
            if nxt in series["resting_hr"]:
                pairs.append((hours, series["resting_hr"][nxt], f"slept {day}, RHR {nxt}"))
        rel_blocks.append(scatter(
            "c-sleep-rhr", "Sleep vs next-day resting heart rate",
            "Each dot is one night paired with the following morning's resting heart rate.",
            pairs, "Sleep", "Next-day resting HR", "sleep_asleep", "resting_hr", "h", "bpm"))
    rel_blocks = [b for b in rel_blocks if b]

    # ---- tables ---------------------------------------------------------
    table_cols = [(defs[k]["label"], series[k], (lambda kk: (lambda v: fmt(kk, v, defs[kk].get("unit", ""))))(k))
                  for k in trend_order[:9]]
    tables = table_view("all charted metrics", axis, table_cols) if table_cols else ""

    # ---- data quality ---------------------------------------------------
    notes = data.get("notes") or []
    note_items = "".join(f"<li>{esc(n)}</li>" for n in notes) or \
                 "<li>No gaps or device conflicts detected.</li>"
    src_items = "".join(
        f"<li><b>{esc(s['name'])}</b> &mdash; {s['records']:,} records</li>"
        for s in meta.get("sources", [])[:12]) or "<li>No source information in the export.</li>"

    badge = ('<div class="badge">SYNTHETIC FIXTURE &mdash; not real health data</div>'
             if fixture else "")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Health metrics</title>
<style>{CSS}</style>
</head>
<body style="margin:0">
<div class="viz-root"><div class="wrap">

<header class="top">
  {badge}
  <h1>Health metrics</h1>
  <p class="lede">Built from an Apple Health export covering
  <b>{esc(rng[0])}</b> to <b>{esc(rng[1])}</b>.
  Every figure is computed from the records in that file &mdash; days without data are
  skipped rather than counted as zero, and nothing is imputed.</p>
</header>

<h2>Overall summary</h2>
{hero_html}
<div class="tiles">{tiles}</div>

<h2>Trends over time</h2>
<div class="grid2">{''.join(charts)}</div>

{'<h2>Sleep</h2><div class="grid2">' + ''.join(sleep_blocks) + '</div>' if sleep_blocks else ''}

{'<h2>Activity patterns</h2><div class="grid2">' + ''.join(pattern_blocks) + '</div>' if pattern_blocks else ''}

{'<h2>Relationships</h2><div class="grid2">' + ''.join(rel_blocks) + '</div>' if rel_blocks else ''}

<h2>The numbers</h2>
{tables}

<h2>Data quality</h2>
<div class="panel">
  <p style="margin:0 0 10px;color:var(--text-secondary)">What to keep in mind when reading the charts above:</p>
  <ul>{note_items}</ul>
  <p style="margin:16px 0 8px;color:var(--text-secondary)">Recording devices and apps in this export:</p>
  <ul>{src_items}</ul>
</div>

<footer>
  Generated locally from your Apple Health export. Nothing was uploaded anywhere.
  Reference ranges shown are commonly cited population figures included for context only &mdash;
  they are not tailored to you, and this dashboard is not medical advice or a diagnosis.
  Consumer wearables carry real measurement error, particularly for sleep staging,
  blood oxygen and HRV. Talk to a clinician about anything that concerns you.
  Parsed {esc(meta.get('parsed_at', ''))}.
</footer>

</div></div>
<script>{JS}</script>
</body>
</html>"""


def main():
    ap = argparse.ArgumentParser(description="Render parsed health JSON into an HTML dashboard.")
    ap.add_argument("json", help="Path to health.json from parse_export.py")
    ap.add_argument("-o", "--out", default="dashboard.html")
    ap.add_argument("--fixture", action="store_true",
                    help="Mark the output as built from synthetic test data")
    args = ap.parse_args()

    with open(args.json) as fh:
        data = json.load(fh)

    html_out = build(data, fixture=args.fixture)
    with open(args.out, "w") as fh:
        fh.write(html_out)
    print(f"Wrote {args.out} ({len(html_out) / 1000:.0f} KB)")


if __name__ == "__main__":
    main()
