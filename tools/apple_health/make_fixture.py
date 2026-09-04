#!/usr/bin/env python3
"""Generate a SYNTHETIC Apple Health export.xml for testing the pipeline.

This produces obviously fake data. It exists so the parser and dashboard can be
exercised without a real export; it is never a stand-in for anyone's health
data, and the dashboard marks any run built from it as a fixture.
"""

import argparse
import math
import random
from datetime import datetime, timedelta

TZ = "-0700"


def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S ") + TZ


def record(out, rtype, unit, start, end, value, source="Fixture Watch"):
    out.append(
        f'<Record type="{rtype}" sourceName="{source}" unit="{unit}" '
        f'creationDate="{fmt(start)}" startDate="{fmt(start)}" endDate="{fmt(end)}" '
        f'value="{value}"/>'
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="fixture_export.xml")
    ap.add_argument("-d", "--days", type=int, default=180)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    end_day = datetime(2026, 9, 1)
    start_day = end_day - timedelta(days=args.days - 1)

    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<HealthData locale=\"en_US\">",
        f'<ExportDate value="{fmt(end_day)}"/>',
        '<Me HKCharacteristicTypeIdentifierBiologicalSex="HKBiologicalSexNotSet" '
        'HKCharacteristicTypeIdentifierDateOfBirth="1996-04-11"/>',
    ]

    weight = 78.0
    for i in range(args.days):
        day = start_day + timedelta(days=i)
        dow = day.weekday()
        season = math.sin(i / 30.0)

        # A deliberate 9-day gap, to exercise the gap detection.
        if 70 <= i < 79:
            continue

        weekend = dow >= 5
        steps = int(rng.gauss(9200 if not weekend else 6100, 2300) + season * 700)
        steps = max(400, steps)
        # Split across two sources on some days, to exercise de-duplication.
        record(out, "HKQuantityTypeIdentifierStepCount", "count",
               day.replace(hour=9), day.replace(hour=21), steps)
        if i % 5 == 0:
            record(out, "HKQuantityTypeIdentifierStepCount", "count",
                   day.replace(hour=9), day.replace(hour=21), int(steps * 0.93),
                   source="Fixture Phone")

        record(out, "HKQuantityTypeIdentifierDistanceWalkingRunning", "km",
               day.replace(hour=9), day.replace(hour=21), round(steps * 0.00073, 3))
        record(out, "HKQuantityTypeIdentifierActiveEnergyBurned", "kcal",
               day.replace(hour=9), day.replace(hour=21), int(steps * 0.041 + rng.gauss(90, 40)))
        record(out, "HKQuantityTypeIdentifierFlightsClimbed", "count",
               day.replace(hour=9), day.replace(hour=21), max(0, int(rng.gauss(11, 5))))
        record(out, "HKQuantityTypeIdentifierAppleExerciseTime", "min",
               day.replace(hour=9), day.replace(hour=21), max(0, int(rng.gauss(34, 18))))

        # Resting HR drifts down as the fixture "gets fitter"; HRV drifts up.
        rhr = rng.gauss(60 - i * 0.018, 2.4)
        record(out, "HKQuantityTypeIdentifierRestingHeartRate", "count/min",
               day.replace(hour=7), day.replace(hour=7), round(rhr, 1))
        record(out, "HKQuantityTypeIdentifierHeartRateVariabilitySDNN", "ms",
               day.replace(hour=7), day.replace(hour=7), round(rng.gauss(44 + i * 0.03, 8), 1))
        record(out, "HKQuantityTypeIdentifierRespiratoryRate", "count/min",
               day.replace(hour=3), day.replace(hour=3), round(rng.gauss(14.4, 1.0), 1))
        record(out, "HKQuantityTypeIdentifierOxygenSaturation", "%",
               day.replace(hour=3), day.replace(hour=3), round(rng.gauss(0.968, 0.008), 3))

        for hour in (8, 12, 16, 20):
            record(out, "HKQuantityTypeIdentifierHeartRate", "count/min",
                   day.replace(hour=hour), day.replace(hour=hour),
                   round(rng.gauss(76, 12), 1))

        weight += rng.gauss(-0.006, 0.16)
        if i % 3 == 0:
            record(out, "HKQuantityTypeIdentifierBodyMass", "kg",
                   day.replace(hour=7, minute=20), day.replace(hour=7, minute=20),
                   round(weight, 2))
        if i % 30 == 0:
            record(out, "HKQuantityTypeIdentifierVO2Max", "mL/min*kg",
                   day.replace(hour=18), day.replace(hour=18),
                   round(rng.gauss(41 + i * 0.012, 0.8), 1))

        # Sleep: in-bed block plus stages inside it.
        bed = (day - timedelta(days=1)).replace(hour=23, minute=rng.randint(0, 50))
        hours = max(4.2, rng.gauss(7.3 if not weekend else 8.0, 0.9))
        wake = bed + timedelta(hours=hours)
        record(out, "HKCategoryTypeIdentifierSleepAnalysis", "",
               bed, wake, "HKCategoryValueSleepAnalysisInBed")
        cursor = bed + timedelta(minutes=12)
        remaining = (wake - cursor).total_seconds() / 3600.0
        plan = [("deep", 0.19), ("core", 0.53), ("rem", 0.22), ("awake", 0.06)]
        for stage, share in plan:
            block = remaining * share
            seg_end = cursor + timedelta(hours=block)
            record(out, "HKCategoryTypeIdentifierSleepAnalysis", "",
                   cursor, seg_end, f"HKCategoryValueSleepAnalysis{stage.capitalize() if stage != 'rem' else 'REM'}"
                   .replace("Deep", "AsleepDeep").replace("Core", "AsleepCore")
                   .replace("REM", "AsleepREM").replace("Awake", "Awake"))
            cursor = seg_end

        if rng.random() < 0.42:
            kind = rng.choice(["Running", "Walking", "TraditionalStrengthTraining",
                               "Cycling", "HighIntensityIntervalTraining"])
            mins = round(rng.gauss(41, 15), 1)
            out.append(
                f'<Workout workoutActivityType="HKWorkoutActivityType{kind}" '
                f'duration="{max(8.0, mins)}" durationUnit="min" '
                f'sourceName="Fixture Watch" startDate="{fmt(day.replace(hour=18))}" '
                f'endDate="{fmt(day.replace(hour=19))}">'
                f'<WorkoutStatistics type="HKQuantityTypeIdentifierActiveEnergyBurned" '
                f'sum="{int(max(8.0, mins) * rng.uniform(7, 12))}" unit="kcal"/>'
                f"</Workout>"
            )

    out.append("</HealthData>")
    with open(args.out, "w") as fh:
        fh.write("\n".join(out))
    print(f"Wrote {args.out} ({len(out):,} lines, {args.days} days, synthetic)")


if __name__ == "__main__":
    main()
