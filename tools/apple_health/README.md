# Apple Health metrics dashboard

Turns an Apple Health export into a self-contained HTML dashboard: overall
summary, stat tiles, trend charts, sleep composition, activity patterns and
data-quality notes.

Everything runs locally with the Python standard library. No dependencies, no
network calls, nothing uploaded.

## Why you have to export manually

There is no Apple Health API to pull from. HealthKit data is stored encrypted on
your iPhone and Watch, and Apple publishes no server-side endpoint for it — so no
script, cloud agent, or web service can fetch it on your behalf. The export below
is the only supported way the data leaves the device.

## 1. Export from your iPhone

1. Open the **Health** app.
2. Tap your **profile picture** (top right).
3. Scroll to the bottom and tap **Export All Health Data**.
4. Confirm **Export**. Preparing the archive takes a few minutes on a large history.
5. Share the resulting `export.zip` to yourself (AirDrop, Files, email, Drive).

The zip contains `apple_health_export/export.xml`. You can hand this tool either
the zip or the extracted xml.

Exports are commonly 100 MB–1 GB. The parser streams the file and releases each
record as it goes, so memory stays flat regardless of size.

## 2. Build the dashboard

```bash
python3 parse_export.py ~/Downloads/export.zip -o health.json
python3 build_dashboard.py health.json -o dashboard.html
```

Then open `dashboard.html` in any browser.

Or in one step:

```bash
bash run.sh ~/Downloads/export.zip
```

(`run.sh` is checked in without the executable bit, so call it via `bash`, or
`chmod +x run.sh` once and use `./run.sh` after that.)

## What it reads

| Group | Metrics |
|---|---|
| Activity | steps, walking + running distance, active & resting energy, flights climbed, exercise and stand minutes |
| Heart | heart rate, resting HR, walking HR, HRV (SDNN) |
| Respiratory | respiratory rate, blood oxygen |
| Body | weight, BMI, body fat, lean mass, height, VO₂ max |
| Sleep | in-bed and asleep time, deep / core / REM / awake stages, efficiency |
| Other | blood pressure, blood glucose, dietary energy and water, workouts |

Metrics absent from your export are simply omitted — nothing is invented to fill
a panel.

## How the numbers are computed

These choices change the totals, so they are stated rather than hidden:

- **Local wall-clock dates.** A sample belongs to the calendar day it was recorded
  in local time, matching what the Health app shows. Timezone offsets are not
  converted to UTC, which would shuffle late-evening samples into the next day.
- **Additive metrics de-duplicate across devices.** A phone in a pocket and a
  watch on a wrist both record the same steps. For a day recorded by more than
  one source, the single most complete source is used rather than the sum. Days
  affected are reported in the data-quality panel.
- **Sleep intervals are unioned, not summed.** Overlapping blocks from multiple
  trackers count once.
- **A night is attributed to the morning you woke up.** A block starting at or
  after 18:00 counts toward the next day.
- **Averaged metrics** (heart rate, HRV) keep the daily mean, min, max and sample
  count. **Point metrics** (weight, VO₂ max) keep the day's last reading.
- **Missing days are skipped, never zero-filled.** Rolling averages need at least
  3 of 7 days present, and lines break across gaps of 2+ days instead of
  interpolating across them.
- **Units are normalised to metric** (km, kg, °C, L) whatever the export uses.

## Files

| File | Purpose |
|---|---|
| `parse_export.py` | Streams `export.xml` → tidy daily metrics JSON |
| `build_dashboard.py` | Renders that JSON → one self-contained HTML file |
| `make_fixture.py` | Generates synthetic test data (clearly labelled; not real data) |
| `run.sh` | Both steps in one command |

## Testing without a real export

```bash
python3 make_fixture.py -o fixture_export.xml -d 180
python3 parse_export.py fixture_export.xml -o fixture.json
python3 build_dashboard.py fixture.json -o fixture.html --fixture
```

Output built this way carries a visible **SYNTHETIC FIXTURE** banner so it can
never be mistaken for real measurements.

## Privacy

`parse_export.py` and `build_dashboard.py` read local files and write local
files. There is no network code in either. The generated HTML has no external
scripts, stylesheets, fonts, or images — nothing loads when you open it.

Health data is sensitive: `health.json` and `dashboard.html` contain your full
history, so treat them like any other private record. Both are gitignored here.

## Not medical advice

The reference ranges shown are commonly cited population figures included for
context only. They are not tailored to you and this dashboard does not diagnose
anything. Consumer wearables carry real measurement error — sleep staging, blood
oxygen and HRV especially. Talk to a clinician about anything that concerns you.
