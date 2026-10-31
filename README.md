# noaa-goes-east-gif

GeoColor-style night loops of the eastern United States from GOES-East (GOES-19),
built from NOAA's public bucket with no cloud compute and no credentials.

- **Satellite data:** the ABI CONUS multiband files (`ABI-L2-MCMIPC`, every 5 minutes)
  in `s3://noaa-goes19`. [VirtualiZarr](https://github.com/zarr-developers/VirtualiZarr)
  opens each netCDF file as byte-range references, so a frame fetches only the bands
  and rows it needs (about 4 MB per band for full CONUS, less for a smaller box).
- **City lights:** NASA's VIIRS Black Marble 2016, requested from
  [GIBS](https://nasa-gibs.github.io/gibs-api-docs/) at run time for the exact box.
- **Night layer:** 10.3 µm infrared clouds over the city lights, plus low cloud and fog
  from the 10.3 minus 3.9 µm difference, tinted pale blue.
- **Day layer** (with `--day-night`): true color from bands 1 to 3 with the CIMSS
  synthetic green, blended into night across the terminator.

Frames are reprojected to Web Mercator so they line up with common web basemaps.

## Night window

A night loop keeps only frames where the sun is below the horizon over the whole box,
from the last sunset at the west edge to the first sunrise at the east edge. The window
follows the sun, so it handles the season and daylight saving time on its own. A wider
box gives a shorter night: for Texas to Nova Scotia that is about 10 hours in late
October and about 5 hours at the June solstice.

## Usage

Needs [uv](https://docs.astral.sh/uv/) and `ffmpeg`. Dependencies are declared inline
in the script.

```bash
# The last full night, default box
uv run geocolor_night.py --out out/

# A chosen box and night
uv run geocolor_night.py --bbox=-98.57,31.41,-67.52,46.43 --night 2026-10-29 --out out/

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
| `--day-night`, `--hours` | Rolling window ending now, with the day layer |
| `--max-dropped` | Share of frames that may be dropped before the run fails |
| `--start`, `--end` | UTC override for the window |

Outputs in `--out`: `east_night.mp4`, `east_night.webp`, `east_night.gif`, `frames/` and
`report.json`.
The MP4 is by far the smallest (3 to 5 MB for a night at 1400 px); the GIF is 40 MB or
more.

A 1400 px night takes about a minute on a laptop and peaks around 1 GB of memory.

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

## Notes

- Pixels outside the GOES-East CONUS scan area are drawn black.
- The day layer has no Rayleigh correction yet, so daytime looks a little hazy.
- Data: NOAA GOES-R series on the [AWS Open Data Registry](https://registry.opendata.aws/noaa-goes/);
  NASA Black Marble via GIBS.
