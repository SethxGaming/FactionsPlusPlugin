#!/usr/bin/env python3
"""Parse an Apple Health export into tidy daily metrics.

Reads the ``export.xml`` Apple's Health app produces (or the ``.zip`` it ships
inside) and writes a compact JSON summary: one value per metric per day, plus
sleep stages, workouts and data-quality notes.

The file is streamed with ``iterparse`` and elements are released as they are
consumed, so memory stays flat on multi-hundred-megabyte exports.

Usage:
    python3 parse_export.py export.zip -o health.json
    python3 parse_export.py export.xml -o health.json --units imperial
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import mean, median
from xml.etree import ElementTree as ET

# --------------------------------------------------------------------------
# Metric configuration
# --------------------------------------------------------------------------
# How each metric collapses to one number per day:
#   sum  - additive over the day (steps, calories); needs source de-duplication
#   avg  - pooled mean of every sample that day (heart rate, HRV)
#   last - the day's final reading (body mass, VO2 max, blood pressure)

SUM_METRICS = {
    "HKQuantityTypeIdentifierStepCount":                ("steps", "count", "Steps"),
    "HKQuantityTypeIdentifierDistanceWalkingRunning":   ("distance", "km", "Walk + run distance"),
    "HKQuantityTypeIdentifierActiveEnergyBurned":       ("active_energy", "kcal", "Active energy"),
    "HKQuantityTypeIdentifierBasalEnergyBurned":        ("basal_energy", "kcal", "Resting energy"),
    "HKQuantityTypeIdentifierFlightsClimbed":           ("flights", "count", "Flights climbed"),
    "HKQuantityTypeIdentifierAppleExerciseTime":        ("exercise_minutes", "min", "Exercise minutes"),
    "HKQuantityTypeIdentifierAppleStandTime":           ("stand_minutes", "min", "Stand minutes"),
    "HKQuantityTypeIdentifierDietaryWater":             ("water", "L", "Water"),
    "HKQuantityTypeIdentifierDietaryEnergyConsumed":    ("energy_in", "kcal", "Dietary energy"),
}

AVG_METRICS = {
    "HKQuantityTypeIdentifierHeartRate":                  ("heart_rate", "bpm", "Heart rate"),
    "HKQuantityTypeIdentifierRestingHeartRate":           ("resting_hr", "bpm", "Resting heart rate"),
    "HKQuantityTypeIdentifierWalkingHeartRateAverage":    ("walking_hr", "bpm", "Walking heart rate"),
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN":   ("hrv", "ms", "HRV (SDNN)"),
    "HKQuantityTypeIdentifierRespiratoryRate":            ("respiratory_rate", "br/min", "Respiratory rate"),
    "HKQuantityTypeIdentifierOxygenSaturation":           ("spo2", "%", "Blood oxygen"),
    "HKQuantityTypeIdentifierBodyTemperature":            ("body_temp", "degC", "Body temperature"),
    "HKQuantityTypeIdentifierAppleSleepingWristTemperature": ("wrist_temp", "degC", "Wrist temperature"),
}

LAST_METRICS = {
    "HKQuantityTypeIdentifierBodyMass":                ("body_mass", "kg", "Weight"),
    "HKQuantityTypeIdentifierBodyMassIndex":           ("bmi", "", "BMI"),
    "HKQuantityTypeIdentifierBodyFatPercentage":       ("body_fat", "%", "Body fat"),
    "HKQuantityTypeIdentifierLeanBodyMass":            ("lean_mass", "kg", "Lean body mass"),
    "HKQuantityTypeIdentifierHeight":                  ("height", "m", "Height"),
    "HKQuantityTypeIdentifierVO2Max":                  ("vo2max", "mL/kg*min", "VO2 max"),
    "HKQuantityTypeIdentifierBloodPressureSystolic":   ("bp_systolic", "mmHg", "Blood pressure (systolic)"),
    "HKQuantityTypeIdentifierBloodPressureDiastolic":  ("bp_diastolic", "mmHg", "Blood pressure (diastolic)"),
    "HKQuantityTypeIdentifierBloodGlucose":            ("blood_glucose", "mg/dL", "Blood glucose"),
}

# Higher-is-better drives the arrow direction on the dashboard's stat tiles.
DIRECTION = {
    "steps": "up", "distance": "up", "active_energy": "up", "flights": "up",
    "exercise_minutes": "up", "stand_minutes": "up", "hrv": "up", "vo2max": "up",
    "sleep_asleep": "up", "sleep_efficiency": "up", "spo2": "up", "water": "up",
    "resting_hr": "down", "walking_hr": "down", "body_fat": "down",
    "bp_systolic": "down", "bp_diastolic": "down",
}

SLEEP_STAGES = {
    "HKCategoryValueSleepAnalysisInBed": "in_bed",
    "HKCategoryValueSleepAnalysisAsleepUnspecified": "core",
    "HKCategoryValueSleepAnalysisAsleep": "core",
    "HKCategoryValueSleepAnalysisAsleepCore": "core",
    "HKCategoryValueSleepAnalysisAsleepDeep": "deep",
    "HKCategoryValueSleepAnalysisAsleepREM": "rem",
    "HKCategoryValueSleepAnalysisAwake": "awake",
}
ASLEEP_STAGES = ("core", "deep", "rem")

# Unit conversions to the canonical metric unit stored in the JSON.
TO_METRIC = {
    "mi": ("km", 1.609344),
    "ft": ("m", 0.3048),
    "in": ("m", 0.0254),
    "lb": ("kg", 0.45359237),
    "st": ("kg", 6.35029318),
    "degF": ("degC", None),      # affine, handled separately
    "fl_oz_us": ("L", 0.0295735296),
    "cup_us": ("L", 0.2365882365),
    "Cal": ("kcal", 1.0),
    "kJ": ("kcal", 0.239005736),
}

DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")


def parse_ts(value):
    """Apple writes '2026-01-31 22:14:03 -0700'. Keep the local wall clock.

    The offset is deliberately dropped: the Health app shows a sample on the
    calendar day it was recorded in *local* time, and converting to UTC would
    shuffle late-evening samples into the next day.
    """
    if not value:
        return None
    m = DATE_RE.match(value)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(g) for g in m.groups())
    try:
        return datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None


def convert(value, unit):
    """Normalise a raw (value, unit) pair to the canonical metric unit."""
    if unit == "degF":
        return (value - 32.0) * 5.0 / 9.0, "degC"
    if unit in TO_METRIC:
        canonical, factor = TO_METRIC[unit]
        return value * factor, canonical
    if unit == "%" and value <= 1.0:
        # Blood oxygen is exported as a 0-1 fraction by some watchOS versions.
        return value * 100.0, "%"
    return value, unit


def night_of(start):
    """Attribute a sleep interval to the night it belongs to.

    A block starting at or after 18:00 belongs to the *next* morning; anything
    earlier (a 02:00 start, a nap) stays on its own date. The dashboard then
    reads 'the night of the 3rd' as the sleep you woke up from on the 3rd.
    """
    return (start + timedelta(days=1)).date() if start.hour >= 18 else start.date()


def merge_intervals(intervals):
    """Union overlapping (start, end) pairs and return total seconds.

    An Apple Watch and a third-party sleep tracker will both write the same
    night; unioning rather than summing keeps that from doubling the total.
    """
    if not intervals:
        return 0.0
    ordered = sorted(intervals)
    total = 0.0
    cur_start, cur_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= cur_end:
            cur_end = max(cur_end, end)
        else:
            total += (cur_end - cur_start).total_seconds()
            cur_start, cur_end = start, end
    total += (cur_end - cur_start).total_seconds()
    return total


def open_export(path):
    """Yield a file object for export.xml, reaching inside a .zip if needed."""
    if zipfile.is_zipfile(path):
        zf = zipfile.ZipFile(path)
        names = [n for n in zf.namelist() if n.endswith("export.xml")]
        if not names:
            raise SystemExit(
                f"{path} is a zip but contains no export.xml (found: {zf.namelist()[:5]})"
            )
        # 'export.xml' rather than 'export_cda.xml' when both are present.
        name = min(names, key=lambda n: (not n.endswith("/export.xml"), len(n)))
        return zf.open(name)
    return open(path, "rb")


class Accumulator:
    """Collects raw samples, keyed for de-duplication, before daily collapse."""

    def __init__(self):
        # sum metrics: day -> source -> total (sources reconciled at the end)
        self.sums = defaultdict(lambda: defaultdict(float))
        # avg metrics: day -> list of values
        self.avgs = defaultdict(list)
        # last metrics: day -> (timestamp, value)
        self.lasts = {}
        self.seen = set()
        self.unit = None

    def dedupe(self, key):
        if key in self.seen:
            return False
        self.seen.add(key)
        return True


def parse(path, progress=True):
    accs = defaultdict(Accumulator)
    sleep = defaultdict(lambda: defaultdict(list))   # night -> stage -> intervals
    sleep_sources = defaultdict(set)
    workouts = []
    sources = defaultdict(int)
    meta = {}
    counts = defaultdict(int)
    total_records = 0
    skipped_dates = 0

    stream = open_export(path)
    try:
        context = ET.iterparse(stream, events=("start", "end"))
        _, root = next(context)
        meta["locale"] = root.get("locale")

        for event, elem in context:
            if event != "end":
                continue
            tag = elem.tag

            if tag == "Record":
                total_records += 1
                if progress and total_records % 250000 == 0:
                    print(f"  ... {total_records:,} records", file=sys.stderr)

                rtype = elem.get("type")
                start = parse_ts(elem.get("startDate"))
                if start is None:
                    skipped_dates += 1
                    elem.clear()
                    root.clear()
                    continue
                source = elem.get("sourceName") or "unknown"

                if rtype == "HKCategoryTypeIdentifierSleepAnalysis":
                    end = parse_ts(elem.get("endDate")) or start
                    stage = SLEEP_STAGES.get(elem.get("value"))
                    if stage and end > start:
                        sleep[night_of(start)][stage].append((start, end))
                        sleep_sources[night_of(start)].add(source)
                        sources[source] += 1
                        counts["sleep"] += 1
                elif rtype in SUM_METRICS or rtype in AVG_METRICS or rtype in LAST_METRICS:
                    raw = elem.get("value")
                    try:
                        value = float(raw)
                    except (TypeError, ValueError):
                        elem.clear()
                        root.clear()
                        continue
                    value, unit = convert(value, elem.get("unit") or "")
                    sources[source] += 1

                    if rtype in SUM_METRICS:
                        key, _, _ = SUM_METRICS[rtype]
                        acc = accs[key]
                        acc.unit = acc.unit or unit
                        # Identical samples re-imported by another app are dropped.
                        if acc.dedupe((source, elem.get("startDate"), elem.get("endDate"), raw)):
                            acc.sums[start.date()][source] += value
                            counts[key] += 1
                    elif rtype in AVG_METRICS:
                        key, _, _ = AVG_METRICS[rtype]
                        acc = accs[key]
                        acc.unit = acc.unit or unit
                        if acc.dedupe((source, elem.get("startDate"), raw)):
                            acc.avgs[start.date()].append(value)
                            counts[key] += 1
                    else:
                        key, _, _ = LAST_METRICS[rtype]
                        acc = accs[key]
                        acc.unit = acc.unit or unit
                        prev = acc.lasts.get(start.date())
                        if prev is None or start >= prev[0]:
                            acc.lasts[start.date()] = (start, value)
                        counts[key] += 1

            elif tag == "Workout":
                start = parse_ts(elem.get("startDate"))
                if start is not None:
                    duration = float(elem.get("duration") or 0)
                    if (elem.get("durationUnit") or "min") == "sec":
                        duration /= 60.0
                    energy = None
                    for stat in elem.findall("WorkoutStatistics"):
                        if stat.get("type") == "HKQuantityTypeIdentifierActiveEnergyBurned":
                            try:
                                energy = float(stat.get("sum"))
                            except (TypeError, ValueError):
                                pass
                    workouts.append({
                        "date": start.date().isoformat(),
                        "type": (elem.get("workoutActivityType") or "")
                                .replace("HKWorkoutActivityType", "") or "Other",
                        "minutes": round(duration, 1),
                        "energy": round(energy, 1) if energy is not None else None,
                    })

            elif tag == "Me":
                for attr, label in (
                    ("HKCharacteristicTypeIdentifierBiologicalSex", "biological_sex"),
                    ("HKCharacteristicTypeIdentifierDateOfBirth", "date_of_birth"),
                    ("HKCharacteristicTypeIdentifierBloodType", "blood_type"),
                ):
                    val = elem.get(attr)
                    if val:
                        meta[label] = val.replace("HKBiologicalSex", "").replace("HKBloodType", "")

            elif tag == "ExportDate":
                meta["export_date"] = elem.get("value")

            elem.clear()
            root.clear()
    finally:
        stream.close()

    return build_summary(accs, sleep, sleep_sources, workouts, sources, meta,
                         counts, total_records, skipped_dates)


def build_summary(accs, sleep, sleep_sources, workouts, sources, meta,
                  counts, total_records, skipped_dates):
    daily = {}
    defs = {}
    notes = []

    label_lookup = {}
    for table in (SUM_METRICS, AVG_METRICS, LAST_METRICS):
        for key, unit, label in table.values():
            label_lookup[key] = (unit, label)

    multi_source = []
    for key, acc in accs.items():
        unit, label = label_lookup.get(key, ("", key))
        series = {}

        if acc.sums:
            conflicted = 0
            for day, by_source in acc.sums.items():
                if len(by_source) > 1:
                    conflicted += 1
                    # An iPhone in a pocket and a Watch on the wrist both count
                    # the same steps. Summing them inflates the day, so the
                    # single most complete source wins instead.
                    series[day.isoformat()] = round(max(by_source.values()), 3)
                else:
                    series[day.isoformat()] = round(next(iter(by_source.values())), 3)
            if conflicted:
                multi_source.append((label, conflicted))
        elif acc.avgs:
            for day, values in acc.avgs.items():
                series[day.isoformat()] = {
                    "avg": round(mean(values), 2),
                    "min": round(min(values), 2),
                    "max": round(max(values), 2),
                    "n": len(values),
                }
        elif acc.lasts:
            for day, (_, value) in acc.lasts.items():
                series[day.isoformat()] = round(value, 3)

        if series:
            daily[key] = dict(sorted(series.items()))
            defs[key] = {
                "label": label,
                "unit": acc.unit or unit,
                "direction": DIRECTION.get(key),
                "samples": counts.get(key, 0),
                "kind": ("avg" if acc.avgs else "sum" if acc.sums else "last"),
            }

    # --- sleep ------------------------------------------------------------
    if sleep:
        stages_out, asleep_out, inbed_out, eff_out = {}, {}, {}, {}
        for night, by_stage in sorted(sleep.items()):
            hours = {s: merge_intervals(v) / 3600.0 for s, v in by_stage.items()}
            asleep = sum(hours.get(s, 0.0) for s in ASLEEP_STAGES)
            in_bed = hours.get("in_bed", 0.0)
            # Some trackers write only InBed; treat that as the asleep estimate.
            if asleep == 0.0 and in_bed > 0.0:
                asleep = in_bed
            key = night.isoformat()
            stages_out[key] = {s: round(hours.get(s, 0.0), 3)
                               for s in ("deep", "core", "rem", "awake")}
            asleep_out[key] = round(asleep, 3)
            if in_bed > 0:
                inbed_out[key] = round(in_bed, 3)
                eff_out[key] = round(100.0 * min(asleep / in_bed, 1.0), 1)

        daily["sleep_asleep"] = asleep_out
        defs["sleep_asleep"] = {"label": "Sleep", "unit": "h", "direction": "up",
                                "samples": counts.get("sleep", 0), "kind": "sum"}
        daily["sleep_stages"] = stages_out
        defs["sleep_stages"] = {"label": "Sleep stages", "unit": "h",
                                "direction": None, "samples": counts.get("sleep", 0),
                                "kind": "stacked"}
        if inbed_out:
            daily["sleep_in_bed"] = inbed_out
            defs["sleep_in_bed"] = {"label": "Time in bed", "unit": "h", "direction": None,
                                    "samples": counts.get("sleep", 0), "kind": "sum"}
            daily["sleep_efficiency"] = eff_out
            defs["sleep_efficiency"] = {"label": "Sleep efficiency", "unit": "%",
                                        "direction": "up",
                                        "samples": counts.get("sleep", 0), "kind": "avg_scalar"}

    # --- data-quality notes ----------------------------------------------
    all_days = sorted({d for series in daily.values() for d in series})
    if all_days:
        first = datetime.fromisoformat(all_days[0]).date()
        last = datetime.fromisoformat(all_days[-1]).date()
        span = (last - first).days + 1
        meta["date_range"] = [all_days[0], all_days[-1]]
        meta["days_spanned"] = span
        meta["days_with_data"] = len(all_days)

        present = {datetime.fromisoformat(d).date() for d in all_days}
        gaps, run_start, prev = [], None, None
        cursor = first
        while cursor <= last:
            if cursor not in present:
                if run_start is None:
                    run_start = cursor
                prev = cursor
            elif run_start is not None:
                gaps.append((run_start, prev))
                run_start = None
            cursor += timedelta(days=1)
        if run_start is not None:
            gaps.append((run_start, prev))
        big_gaps = [g for g in gaps if (g[1] - g[0]).days + 1 >= 2]
        if big_gaps:
            notes.append(
                f"{len(big_gaps)} gap(s) of 2+ days with no data at all, totalling "
                f"{sum((b - a).days + 1 for a, b in big_gaps)} days. "
                "Averages skip these days rather than treating them as zero."
            )
            meta["gaps"] = [{"from": a.isoformat(), "to": b.isoformat(),
                             "days": (b - a).days + 1} for a, b in big_gaps[:20]]

    for label, days in sorted(multi_source, key=lambda x: -x[1]):
        notes.append(
            f"{label}: {days} day(s) recorded by more than one device. "
            "The most complete device was used for those days, not the sum, "
            "so a phone and a watch don't double-count."
        )

    if skipped_dates:
        notes.append(f"{skipped_dates:,} record(s) had an unreadable timestamp and were dropped.")

    meta["sources"] = [{"name": n, "records": c}
                       for n, c in sorted(sources.items(), key=lambda x: -x[1])[:25]]
    meta["total_records"] = total_records
    meta["parsed_at"] = datetime.now().isoformat(timespec="seconds")

    return {
        "meta": meta,
        "metrics": defs,
        "daily": daily,
        "workouts": workouts,
        "notes": notes,
    }


def main():
    ap = argparse.ArgumentParser(description="Parse an Apple Health export into daily metrics JSON.")
    ap.add_argument("export", help="Path to export.zip or export.xml")
    ap.add_argument("-o", "--out", default="health.json", help="Output JSON path")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.export):
        raise SystemExit(f"No such file: {args.export}")

    if not args.quiet:
        size = os.path.getsize(args.export) / 1e6
        print(f"Reading {args.export} ({size:,.1f} MB)...", file=sys.stderr)

    data = parse(args.export, progress=not args.quiet)

    with open(args.out, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))

    if not args.quiet:
        meta = data["meta"]
        print(f"Parsed {meta.get('total_records', 0):,} records", file=sys.stderr)
        print(f"Metrics found: {len(data['metrics'])}", file=sys.stderr)
        rng = meta.get("date_range")
        if rng:
            print(f"Range: {rng[0]} -> {rng[1]} "
                  f"({meta.get('days_with_data')} days with data)", file=sys.stderr)
        print(f"Wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
