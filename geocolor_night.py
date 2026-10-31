# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "virtualizarr[hdf]>=2.8", "obstore", "obspec-utils", "xarray", "zarr>=3",
#   "h5netcdf", "numpy", "pyproj", "pillow", "scipy",
# ]
# ///
"""GeoColor-style night loop of the eastern US from GOES-East (GOES-19).

Reads the ABI CONUS multiband files (ABI-L2-MCMIPC) from NOAA's public bucket
through VirtualiZarr, so only the byte ranges of the needed bands and rows are
fetched. City lights come from NASA GIBS (VIIRS Black Marble) at run time.

The loop covers one night, from when the sun has set over the whole box to just
before it rises anywhere in it, so no frame has daylight. The window follows the
sun, which takes care of the season and of daylight saving time.

    uv run geocolor_night.py --out out/                     # the last full night
    uv run geocolor_night.py --night 2026-10-29 --out out/  # the night starting that evening

With --day-night it instead renders the last 24 hours (--hours), blending true
color by day into the night layer across the terminator.

    uv run geocolor_night.py --day-night --every 15 --out out/
"""

from __future__ import annotations

import argparse
import io
import resource
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import obstore
import xarray as xr
from obspec_utils.registry import ObjectStoreRegistry
from obstore.store import HTTPStore, S3Store
from PIL import Image, ImageDraw, ImageFont
from pyproj import Proj
from scipy.ndimage import map_coordinates
from virtualizarr.parsers import HDFParser

BUCKET = "noaa-goes19"
ORIGIN = f"https://{BUCKET}.s3.amazonaws.com"
PRODUCT = "ABI-L2-MCMIPC"

# Default box, lon/lat: Texas to Nova Scotia, the Florida Keys to Lake Superior.
DEFAULT_BBOX = "-103.15,23.92,-64.05,49.37"
R_EARTH = 6_378_137.0

EASTERN = ZoneInfo("America/New_York")
s3 = S3Store(BUCKET, region="us-east-1", skip_signature=True)
registry = ObjectStoreRegistry({ORIGIN: HTTPStore.from_url(ORIGIN)})


def merc_y(lat: float) -> float:
    return R_EARTH * float(np.log(np.tan(np.pi / 4 + np.radians(lat) / 2)))


class Grid:
    """The output grid: a Web Mercator box from lon/lat bounds, `width` pixels wide."""

    def __init__(self, bbox: str, width: int):
        lon0, lat0, lon1, lat1 = map(float, bbox.split(","))
        self.xmin, self.xmax = R_EARTH * np.radians(lon0), R_EARTH * np.radians(lon1)
        self.ymin, self.ymax = merc_y(lat0), merc_y(lat1)
        self.width = width
        self.height = round(width * (self.ymax - self.ymin) / (self.xmax - self.xmin))

    def lonlat(self) -> tuple[np.ndarray, np.ndarray]:
        x = self.xmin + (np.arange(self.width) + 0.5) * (self.xmax - self.xmin) / self.width
        y = self.ymax - (np.arange(self.height) + 0.5) * (self.ymax - self.ymin) / self.height
        xx, yy = np.meshgrid(x, y)
        return np.degrees(xx / R_EARTH), np.degrees(np.arctan(np.sinh(yy / R_EARTH)))

    def city_lights(self) -> np.ndarray:
        """VIIRS Black Marble 2016 for the box, as float RGB in 0..1."""
        box = ",".join(f"{v:.0f}" for v in (self.xmin, self.ymin, self.xmax, self.ymax))
        url = (
            "https://gibs.earthdata.nasa.gov/wms/epsg3857/best/wms.cgi?SERVICE=WMS&REQUEST=GetMap"
            "&VERSION=1.3.0&LAYERS=VIIRS_Black_Marble&STYLES=&CRS=EPSG:3857"
            f"&BBOX={box}&WIDTH={self.width}&HEIGHT={self.height}"
            "&FORMAT=image/png&TIME=2016-01-01"
        )
        with urllib.request.urlopen(url, timeout=120) as r:
            im = Image.open(io.BytesIO(r.read())).convert("RGB")
        return np.asarray(im, dtype="float32") / 255


def list_scans(start: datetime, end: datetime, every_min: int) -> list[str]:
    keys: list[str] = []
    hour = start.replace(minute=0, second=0, microsecond=0)
    while hour <= end:
        prefix = f"{PRODUCT}/{hour:%Y/%j/%H}/"
        for batch in obstore.list(s3, prefix=prefix):
            keys += [o["path"] for o in batch]
        hour += timedelta(hours=1)
    out, last = [], None
    for k in sorted(keys):
        t = scan_start(k)
        if not (start <= t <= end):
            continue
        if last is None or t - last >= timedelta(minutes=every_min) - timedelta(seconds=30):
            out.append(k)
            last = t
    return out


def scan_start(key: str) -> datetime:
    stamp = key.split("_s")[1][:13]
    return datetime.strptime(stamp, "%Y%j%H%M%S").replace(tzinfo=timezone.utc)


def solar_zenith(lat: np.ndarray, lon: np.ndarray, when: datetime) -> np.ndarray:
    """NOAA low-precision solar position, good to a fraction of a degree."""
    doy = when.timetuple().tm_yday
    g = 2 * np.pi / 365 * (doy - 1 + (when.hour - 12) / 24)
    decl = (0.006918 - 0.399912 * np.cos(g) + 0.070257 * np.sin(g) - 0.006758 * np.cos(2 * g)
            + 0.000907 * np.sin(2 * g) - 0.002697 * np.cos(3 * g) + 0.00148 * np.sin(3 * g))
    eqt = 229.18 * (0.000075 + 0.001868 * np.cos(g) - 0.032077 * np.sin(g)
                    - 0.014615 * np.cos(2 * g) - 0.040849 * np.sin(2 * g))
    minutes = when.hour * 60 + when.minute + when.second / 60
    ha = np.radians((minutes + eqt + 4 * lon) / 4 - 180)
    la = np.radians(lat)
    cz = np.sin(la) * np.sin(decl) + np.cos(la) * np.cos(decl) * np.cos(ha)
    return np.degrees(np.arccos(np.clip(cz, -1, 1)))


class Sampler:
    """Maps the output grid to fractional rows and columns of the ABI fixed grid."""

    def __init__(self, ds: xr.Dataset, lon: np.ndarray, lat: np.ndarray):
        p = ds["goes_imager_projection"].attrs
        h = p["perspective_point_height"]
        proj = Proj(proj="geos", h=h, lon_0=p["longitude_of_projection_origin"], sweep="x",
                    a=p["semi_major_axis"], b=p["semi_minor_axis"])
        gx, gy = proj(lon, lat)
        gx, gy = np.asarray(gx) / h, np.asarray(gy) / h
        xs, ys = ds["x"].values, ds["y"].values
        col = (gx - xs[0]) / (xs[1] - xs[0])
        row = (gy - ys[0]) / (ys[1] - ys[0])
        pad = 2
        self.r0 = max(int(np.floor(row.min())) - pad, 0)
        self.r1 = min(int(np.ceil(row.max())) + pad, len(ys))
        self.c0 = max(int(np.floor(col.min())) - pad, 0)
        self.c1 = min(int(np.ceil(col.max())) + pad, len(xs))
        self.coords = np.stack([row - self.r0, col - self.c0])
        # Output pixels outside the scan sector (or off the disk) get no data.
        self.valid = (np.isfinite(row) & np.isfinite(col) & (row >= 0) & (row <= len(ys) - 1)
                      & (col >= 0) & (col <= len(xs) - 1))
        self.coords = np.nan_to_num(self.coords)
        self.key = (float(xs[0]), float(ys[0]), len(xs), len(ys))

    def window(self, ds: xr.Dataset) -> xr.Dataset:
        return ds.isel(y=slice(self.r0, self.r1), x=slice(self.c0, self.c1))

    def warp(self, a: np.ndarray) -> np.ndarray:
        return map_coordinates(a, self.coords, order=1, mode="nearest").astype("float32")


def open_scan(key: str) -> xr.Dataset:
    ms = HDFParser()(url=f"{ORIGIN}/{key}", registry=registry)
    return xr.open_zarr(ms, consolidated=False, zarr_format=3, decode_times=False)


def compose(bands: dict[str, np.ndarray], lights: np.ndarray,
            sza: np.ndarray | None = None) -> np.ndarray:
    # Night: IR clouds over city lights. Cold tops are opaque and bright.
    t13 = bands["C13"]
    # The ramp starts below clear-ground temperature so open land and sea stay dark.
    ir = np.clip((274 - t13) / (274 - 215), 0, 1)
    cloud_alpha = np.clip(ir * 1.6, 0, 1) ** 0.8
    cloud_gray = (0.45 + 0.55 * ir)[..., None]
    night = lights * (1 - cloud_alpha[..., None]) + cloud_gray * cloud_alpha[..., None]

    # Low cloud and fog: warm, so IR misses it, but the 10.3 - 3.9 um difference
    # is positive over water clouds at night. Tinted pale blue, as in GeoColor.
    btd = bands["C13"] - bands["C07"]
    low = np.clip((btd - 1.0) / 4.0, 0, 1) * (1 - cloud_alpha) * 0.55
    night = night * (1 - low[..., None]) + low[..., None] * np.array([0.62, 0.72, 0.85])
    if sza is None or "C02" not in bands:
        return night

    # Day: true color with the CIMSS synthetic green (0.45 red, 0.45 blue, 0.1 near-IR),
    # a flat haze offset, and a gamma. Blended into night from solar zenith 80 to 88.
    r, b, veg = bands["C02"], bands["C01"], bands["C03"]
    g = 0.45 * r + 0.45 * b + 0.1 * veg
    day = np.stack([r, g, b], -1)
    day = np.clip((day - [0.015, 0.03, 0.06]) / 0.9, 0, 1) ** (1 / 2.2)
    w = np.clip((88 - sza) / 8, 0, 1)[..., None]
    return w * day + (1 - w) * night


def night_window(lon: np.ndarray, lat: np.ndarray, evening: date,
                 sun_below: float) -> tuple[datetime, datetime]:
    """The dark span of the night that starts on `evening` (Eastern date).

    Dark means the sun's zenith angle is at least `sun_below` degrees at every
    point of the box. 90 is sunset; 96 is the end of civil twilight.
    """
    lo = np.concatenate([lon[::10, ::10].ravel(), lon[[0, 0, -1, -1], [0, -1, 0, -1]]])
    la = np.concatenate([lat[::10, ::10].ravel(), lat[[0, 0, -1, -1], [0, -1, 0, -1]]])
    t0 = datetime(evening.year, evening.month, evening.day, 18, tzinfo=timezone.utc)
    times = [t0 + timedelta(minutes=m) for m in range(0, 24 * 60, 5)]
    dark = np.array([solar_zenith(la, lo, t).min() >= sun_below for t in times])
    if not dark.any():
        raise SystemExit(f"no time on the night of {evening} is fully dark over the box")
    i0 = int(np.argmax(dark))
    i1 = i0 + int(np.argmin(dark[i0:])) - 1 if not dark[i0:].all() else len(dark) - 1
    return times[i0], times[i1]


def last_full_night(lon: np.ndarray, lat: np.ndarray, sun_below: float) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    today = now.astimezone(EASTERN).date()
    for back in (1, 2):
        start, end = night_window(lon, lat, today - timedelta(days=back), sun_below)
        if end <= now - timedelta(minutes=10):  # allow for NOAA's delivery delay
            return start, end
    raise SystemExit("no finished night found")


def render(key: str, lights: np.ndarray, lon: np.ndarray, lat: np.ndarray,
           sampler: Sampler | None, day_night: bool = False) -> tuple[Image.Image, datetime, Sampler]:
    ds = open_scan(key)
    if sampler is None or sampler.key != (float(ds.x[0]), float(ds.y[0]), ds.sizes["x"], ds.sizes["y"]):
        sampler = Sampler(ds, lon, lat)
    when = scan_start(key)
    win = sampler.window(ds)
    sza = solar_zenith(lat, lon, when) if day_night else None
    names = ["C07", "C13"]
    if sza is not None and (sza < 88).any():
        names += ["C01", "C02", "C03"]
    bands = {n: sampler.warp(np.nan_to_num(win[f"CMI_{n}"].values, nan=0.0)) for n in names}
    rgb = compose(bands, lights, sza)
    rgb[~sampler.valid] = 0
    im = Image.fromarray((np.clip(rgb, 0, 1) * 255).astype("uint8"))
    draw = ImageDraw.Draw(im)
    font = ImageFont.load_default(size=22)
    label = when.astimezone(EASTERN).strftime("%b %-d  %-I:%M %p %Z")
    draw.text((18, im.height - 40), label, fill=(235, 235, 235), font=font)
    return im, when, sampler


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--night", type=date.fromisoformat,
                    help="Eastern date of the evening the night starts; default: the last full night")
    ap.add_argument("--sun-below", type=float, default=90.0,
                    help="solar zenith angle, degrees, that counts as dark (90 sunset, 96 civil dusk)")
    ap.add_argument("--day-night", action="store_true",
                    help="the last --hours ending now, with day and night blended")
    ap.add_argument("--hours", type=float, default=24, help="length of the --day-night loop")
    ap.add_argument("--start", help="UTC override, e.g. 2026-10-29T22:30")
    ap.add_argument("--end", help="UTC override")
    ap.add_argument("--every", type=int, default=10, help="minutes between frames")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--bbox", default=DEFAULT_BBOX, help="lon_min,lat_min,lon_max,lat_max")
    ap.add_argument("--width", type=int, default=1400, help="output width in pixels")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    frames_dir = args.out / "frames"
    frames_dir.mkdir(exist_ok=True)

    t0 = time.time()
    grid = Grid(args.bbox, args.width)
    lights = grid.city_lights()
    lon, lat = grid.lonlat()
    print(f"grid {grid.width} x {grid.height}")
    if args.start and args.end:
        start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    elif args.day_night:
        end = datetime.now(timezone.utc) - timedelta(minutes=15)  # NOAA delivery delay
        start = end - timedelta(hours=args.hours)
    elif args.night:
        start, end = night_window(lon, lat, args.night, args.sun_below)
    else:
        start, end = last_full_night(lon, lat, args.sun_below)
    fmt = "%b %-d %-I:%M %p %Z"
    print(f"window: {start.astimezone(EASTERN):{fmt}} to {end.astimezone(EASTERN):{fmt}}"
          f" ({(end - start).total_seconds() / 3600:.1f} h)")
    keys = list_scans(start, end, args.every)
    print(f"{len(keys)} scans, lights in {time.time() - t0:.1f} s")

    # One sampler built up front, then shared by the workers.
    _, _, sampler = render(keys[0], lights, lon, lat, None, args.day_night)

    def job(i_key: tuple[int, str]) -> str:
        i, key = i_key
        im, when, _ = render(key, lights, lon, lat, sampler, args.day_night)
        im.save(frames_dir / f"{i:04d}.png")
        return f"{when:%H:%M}"

    with ThreadPoolExecutor(args.workers) as pool:
        for n, stamp in enumerate(pool.map(job, enumerate(keys)), 1):
            if n % 10 == 0 or n == len(keys):
                print(f"{n}/{len(keys)} {stamp} {time.time() - t0:.0f} s", flush=True)

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20
    print(f"frames done in {time.time() - t0:.0f} s, peak RSS {peak:.0f} MB")

    frames = [Image.open(p) for p in sorted(frames_dir.glob("*.png"))]
    small = [f.resize((f.width * 3 // 4, f.height * 3 // 4), Image.LANCZOS) for f in frames]
    pal = [f.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
           for f in small]
    pal[0].save(args.out / "east_night.gif", save_all=True, append_images=pal[1:],
                duration=90, loop=0, optimize=True)
    frames[0].save(args.out / "east_night.webp", save_all=True, append_images=frames[1:],
                   duration=90, loop=0, quality=70, method=4)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-framerate", "11", "-i", str(frames_dir / "%04d.png"),
         "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "24", "-movflags", "+faststart",
         str(args.out / "east_night.mp4")],
        check=True,
    )


if __name__ == "__main__":
    main()
