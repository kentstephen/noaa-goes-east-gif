# noaa-goes-east-gif

GeoColor-style loops of the eastern United States from GOES-East (GOES-19), at night
or from afternoon into night, read straight from NOAA's public bucket with
[VirtualiZarr](https://github.com/zarr-developers/VirtualiZarr), with no cloud compute
and no credentials.

Inspired by the old [fused.io](https://fused.io) website.

- **Satellite data:** the ABI CONUS multiband files (`ABI-L2-MCMIPC`, every 5 minutes)
  in `s3://noaa-goes19`. [VirtualiZarr](https://github.com/zarr-developers/VirtualiZarr)
  opens each netCDF file as byte-range references, so a frame fetches only the bands
  and rows it needs (about 4 MB per band for full CONUS, less for a smaller box).
- **City lights:** NASA's VIIRS Black Marble 2016, requested from
  [GIBS](https://nasa-gibs.github.io/gibs-api-docs/) at run time for the exact box.
- **Night layer:** 10.3 µm infrared clouds over the city lights, with a local-contrast
  boost for cloud texture, plus low cloud and fog from the 10.3 minus 3.9 µm
  difference, tinted pale blue.
- **Day layer** (with `--afternoon` or `--day-night`): true color from bands 1 to 3 with
  - a sun-angle correction capped at 80 degrees,
  - a Rayleigh (haze) correction from [pyspectral](https://github.com/pytroll/pyspectral),
    tapered off near the terminator,
  - detail from the 0.5 km red band (`ABI-L2-CMIPC` band 2), area-averaged to the
    output grid and applied as a ratio to all three channels,
  - the CIMSS synthetic green and CIRA's log contrast stretch.

  It blends into the night layer from solar zenith 80 to 88 degrees.

Frames are reprojected to Web Mercator so they line up with common web basemaps.

## Night window

A night loop keeps only frames where the sun is below the horizon over the whole box,
from the last sunset at the west edge to the first sunrise at the east edge. The window
follows the sun, so it handles the season and daylight saving time on its own. A wider
box gives a shorter night: for Texas to Nova Scotia that is about 10 hours in late
October and about 5 hours at the June solstice.

## Usage

Needs [uv](https://docs.astral.sh/uv/) and `ffmpeg`. Dependencies are in
`pyproject.toml`, pinned to exact versions in `uv.lock` (Python 3.12), and the daily
job installs only what the lockfile says. On the 1st of each month,
`.github/workflows/deps.yml` runs `uv lock --upgrade` on a branch, renders both loops
with it, and merges into `main` only if both pass; a failed upgrade stays on its
`deps/uv-YYYY-MM` branch. To upgrade by hand: `uv lock --upgrade`, render a test
loop, then commit the new `uv.lock`.

```bash
# The last full night, default box
uv run geocolor_night.py --out out/

# A chosen box and night
uv run geocolor_night.py --bbox=-98.57,31.41,-67.52,46.43 --night 2026-10-29 --out out/

# Afternoon into night: 2 hours before sunset reaches the box, through to first light
uv run geocolor_night.py --afternoon 2 --night 2026-10-29 --out out/

# 8 seconds: 2 of afternoon, then dusk and night (what the daily job renders)
uv run geocolor_night.py --seconds 8 --day-seconds 2 --bbox=-112.81,24.31,-66.47,47.11 --out out/

# The last 24 hours, day and night, every 15 minutes
uv run geocolor_night.py --bbox=-126,24,-66,49.9 --width 1920 --day-night --every 15 --out out/
```

| Option | Meaning |
|---|---|
| `--bbox W,S,E,N` | Box in degrees of longitude and latitude |
| `--width` | Output width in pixels (height follows from the box) |
| `--night YYYY-MM-DD` | Eastern date of the evening the night starts |
| `--sun-below` | Solar zenith angle that counts as dark: 90 is sunset, 96 is civil dusk |
| `--every` | Minutes between frames (scans arrive every 5) |
| `--afternoon H` | Start H hours before sunset first reaches the box, end at first light |
| `--seconds`, `--day-seconds` | Fixed-length loop: this many seconds of daylight before sunset first reaches the box, then dusk and night for the rest |
| `--label`, `--label-aspect` | Corner for the time stamp (or `none`), and a viewport shape (W:H) whose centered crop it must stay inside |
| `--day-night`, `--hours` | Rolling window ending now, with the day layer |
| `--no-hires` | Skip the 0.5 km red band detail |
| `--fps` | Playback frames per second (default 11) |
| `--formats` | Comma list of `mp4`, `webp`, `gif` (default `mp4`) |
| `--max-dropped` | Share of frames that may be dropped before the run fails |
| `--start`, `--end` | UTC override for the window |

Outputs in `--out`: `east_night.mp4` (plus `.webp` or `.gif` when asked), `frames/` and
`report.json`. The MP4 is by far the smallest: 3 to 5 MB for a night at 1400 px, about
7.5 MB for a 15 hour afternoon-to-dawn loop. The GIF is 40 MB or more.

A 1400 px night takes about a minute on a laptop and peaks around 1 GB of memory. An
afternoon loop takes about 3 minutes, downloads about 3 GB (daytime frames read the
0.5 km red band) and peaked at 3.6 GB with 4 workers; use `--workers 2` to lower that.
The Rayleigh tables (about 60 MB) download on first use and are cached by pyspectral.

## Bad frames

Scans sometimes arrive with a band missing (for example, the 3.9 µm band filled
entirely with fill values), which would flash a whole frame. Each run checks for this:

- **Per scan:** each band must cover at least 95% of the box, and the infrared median
  must be a plausible temperature. A failed scan is swapped for the nearest good scan
  within one step.
- **Per frame:** a frame whose mean brightness jumps more than 25% from its neighbors
  is dropped.
- **Per run:** if more than `--max-dropped` (default 10%) of the frames are dropped, the
  run exits with an error and writes no videos, so a scheduled job keeps the last good
  loop.

`report.json` in the output folder lists every swapped scan, empty slot and dropped
frame, with the reason.

## Daily job

`.github/workflows/hero.yml` runs every day at 12:30 UTC, after the previous
evening's loop has finished. It renders two 8 second loops of that evening, with no
time stamp burned in:

| Name | Box | Frame |
|---|---|---|
| `hero-desktop` | `-113.41,23.44,-61.94,49.12` (Arizona to Nova Scotia) | 1400 x 882 |
| `hero-mobile` | `-89.82,19.4,-68.5,49.99` (Great Lakes to Florida and Cuba) | 1400 x 2503 |

Each uploads to a Cloudflare R2 bucket:

- `hero-<name>.mp4` and a `hero-<name>.jpg` poster frame: the current loop, which the
  site plays. Overwritten each day, with a one hour cache lifetime.
- `archive/<name>/<YYYY-MM-DD>.mp4` and `.jpg`: a dated copy, named for the evening it
  shows. The newest 7 evenings are kept. Older copies are deleted by count, not by
  age, so an outage of any length never empties the archive.

To put an earlier evening back (for example after a loop that passed the checks but
looks wrong), run the workflow by hand with `restore` set to its date, or:

```bash
gh workflow run hero.yml -f restore=2026-10-29
```

Repo settings: secrets `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY`,
and the variable `R2_BUCKET`.

### Backup trigger

GitHub's schedule can be late, skip a day, or not start at all. `watchdog/` is a
Cloudflare Worker that checks the `evening` metadata on the current loops in R2 at
13:15, 15:15 and 18:15 UTC. If either loop is older than last evening and no render
is queued or running, it starts `hero.yml` through the GitHub API. If GitHub has
disabled the workflow for inactivity, it re-enables it first. On days the schedule
works, it does nothing.

It needs a fine-grained GitHub token (this repo only, Actions read and write) stored
as the Worker secret `GH_TOKEN`. Deploy with `cd watchdog && npx wrangler deploy`;
see its runs with `npx wrangler tail`.

## When a run fails

The published loop is replaced only after a run succeeds. If a run fails for any
reason (too many bad frames, missing scans, a network error), it writes no video and
the scheduled job leaves the published file alone, so the last good loop keeps
playing until a later run succeeds.

## Notes

- Pixels outside the GOES-East CONUS scan area are drawn black.
- Data: NOAA GOES-R series on the [AWS Open Data Registry](https://registry.opendata.aws/noaa-goes/);
  NASA Black Marble via GIBS.
