# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "virtualizarr[hdf]>=2.8", "obstore", "obspec-utils", "xarray", "zarr>=3",
#   "h5netcdf", "numpy", "pyproj", "pillow", "scipy", "pyspectral", "pyorbital",
# ]
# ///
"""GeoColor-style loops of the eastern US from GOES-East (GOES-19).

Reads the ABI CONUS multiband files (ABI-L2-MCMIPC) from NOAA's public bucket
through VirtualiZarr, so only the byte ranges of the needed bands and rows are
fetched. City lights come from NASA GIBS (VIIRS Black Marble) at run time.

Windows (all follow the sun, which takes care of the season and of daylight
saving time):

    uv run geocolor_night.py --out out/                       # last full night, all dark
    uv run geocolor_night.py --night 2026-10-29 --out out/    # that evening's night
    uv run geocolor_night.py --afternoon 2 --out out/         # 2 h before sunset reaches
                                                              # the box, through to dawn
    uv run geocolor_night.py --day-night --every 15 --out out/  # last 24 hours

Daytime frames get a Rayleigh (haze) correction, a partial sun-angle correction,
detail from the 0.5 km red band (ABI-L2-CMIPC C02), and CIRA's log contrast
stretch. Night clouds get a local-contrast boost.
"""

from __future__ import annotations

import argparse
import io
import json
import resource
import shutil
import subprocess
import threading
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
from scipy.ndimage import gaussian_filter, map_coordinates
from virtualizarr.parsers import HDFParser

BUCKET = "noaa-goes19"
ORIGIN = f"https://{BUCKET}.s3.amazonaws.com"
PRODUCT = "ABI-L2-MCMIPC"     # all 16 bands at 2 km
HIRES_PRODUCT = "ABI-L2-CMIPC"  # one band per file; C02 (red) is 0.5 km
SAT_ALT_KM = 35786.0

# Default box, lon/lat: Santa Fe to eastern Maine, the Florida Keys to Lake Superior.
DEFAULT_BBOX = "-106.1143,24.2797,-67.6187,46.5638"
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


# ---------------------------------------------------------------- listing

def list_keys(product: str, start: datetime, end: datetime) -> list[str]:
    keys: list[str] = []
    hour = start.replace(minute=0, second=0, microsecond=0)
    while hour <= end:
        for batch in obstore.list(s3, prefix=f"{product}/{hour:%Y/%j/%H}/"):
            keys += [o["path"] for o in batch]
        hour += timedelta(hours=1)
    return sorted(keys)


def list_scans(start: datetime, end: datetime, every_min: int) -> list[list[str]]:
    """One slot per frame time, every `every_min` minutes from `start`.

    Each slot lists the scans within one step of its time, nearest first, so a bad
    scan can be swapped for a neighbor (two bad scans in a row happen). A slot with
    no scans (an outage) is left out.
    """
    step = timedelta(minutes=every_min)
    keys = [k for k in list_keys(PRODUCT, start - step, end + step)
            if start - step <= scan_start(k) <= end + step]
    slots, t = [], start
    while t <= end:
        near = [k for k in keys if abs(scan_start(k) - t) <= step]
        if near:
            slots.append(sorted(near, key=lambda k: abs(scan_start(k) - t)))
        else:
            print(f"no scan near {t:%Y-%m-%d %H:%M} UTC, slot skipped")
        t += step
    return slots


def hires_red_keys(start: datetime, end: datetime) -> dict[str, str]:
    """0.5 km red band files by scan start stamp, to pair with the multiband files."""
    out = {}
    for k in list_keys(HIRES_PRODUCT, start - timedelta(minutes=15), end + timedelta(minutes=15)):
        if "M6C02_" in k or "M3C02_" in k:
            out[k.split("_s")[1][:13]] = k
    return out


def scan_start(key: str) -> datetime:
    stamp = key.split("_s")[1][:13]
    return datetime.strptime(stamp, "%Y%j%H%M%S").replace(tzinfo=timezone.utc)


def open_scan(key: str) -> xr.Dataset:
    ms = HDFParser()(url=f"{ORIGIN}/{key}", registry=registry)
    return xr.open_zarr(ms, consolidated=False, zarr_format=3, decode_times=False)


# ---------------------------------------------------------------- geometry

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
    """Maps the output grid to fractional rows and columns of an ABI fixed grid."""

    def __init__(self, ds: xr.Dataset, lon: np.ndarray, lat: np.ndarray):
        p = ds["goes_imager_projection"].attrs
        h = p["perspective_point_height"]
        self.sat_lon = float(p["longitude_of_projection_origin"])
        proj = Proj(proj="geos", h=h, lon_0=self.sat_lon, sweep="x",
                    a=p["semi_major_axis"], b=p["semi_minor_axis"])
        gx, gy = proj(lon, lat)
        gx, gy = np.asarray(gx) / h, np.asarray(gy) / h
        xs, ys = ds["x"].values, ds["y"].values
        col = (gx - xs[0]) / (xs[1] - xs[0])
        row = (gy - ys[0]) / (ys[1] - ys[0])
        # Source pixels per output pixel, for anti-aliasing finer grids.
        self.step = float(np.nanmedian(np.abs(np.diff(col, axis=1))))
        pad = 2
        self.r0 = max(int(np.floor(np.nanmin(row))) - pad, 0)
        self.r1 = min(int(np.ceil(np.nanmax(row))) + pad, len(ys))
        self.c0 = max(int(np.floor(np.nanmin(col))) - pad, 0)
        self.c1 = min(int(np.ceil(np.nanmax(col))) + pad, len(xs))
        # Output pixels outside the scan sector (or off the disk) get no data.
        self.valid = (np.isfinite(row) & np.isfinite(col) & (row >= 0) & (row <= len(ys) - 1)
                      & (col >= 0) & (col <= len(xs) - 1))
        self.coords = np.nan_to_num(np.stack([row - self.r0, col - self.c0]))
        self.key = (float(xs[0]), float(ys[0]), len(xs), len(ys))

    def window(self, ds: xr.Dataset) -> xr.Dataset:
        return ds.isel(y=slice(self.r0, self.r1), x=slice(self.c0, self.c1))

    def warp(self, a: np.ndarray) -> np.ndarray:
        return map_coordinates(a, self.coords, order=1, mode="nearest").astype("float32")


class Geometry:
    """Satellite viewing angles for the output grid. They do not change between scans."""

    def __init__(self, lon: np.ndarray, lat: np.ndarray, sat_lon: float):
        from pyorbital.orbital import get_observer_look

        self.lon, self.lat = lon, lat
        az, el = get_observer_look(np.full_like(lon, sat_lon), np.zeros_like(lon),
                                   np.full_like(lon, SAT_ALT_KM), datetime(2026, 1, 1),
                                   lon, lat, np.zeros_like(lon))
        self.sat_zenith = (90.0 - el).astype("float32")
        self.sat_azimuth = az.astype("float32")

    def azimuth_diff(self, when: datetime) -> np.ndarray:
        from pyorbital.astronomy import get_alt_az

        _, sun_az = get_alt_az(when.replace(tzinfo=None), self.lon, self.lat)
        d = np.abs(np.degrees(sun_az) - self.sat_azimuth) % 360
        return np.minimum(d, 360 - d).astype("float32")


_rayleigh = None
_rayleigh_lock = threading.Lock()


def rayleigh():
    """pyspectral's Rayleigh tables, downloaded once (about 60 MB) and cached on disk."""
    global _rayleigh
    with _rayleigh_lock:
        if _rayleigh is None:
            from pyspectral.rayleigh import Rayleigh
            from pyspectral.utils import download_luts

            try:
                _rayleigh = Rayleigh("GOES-19", "abi", atmosphere="us-standard",
                                     aerosol_type="rayleigh_only")
            except Exception:
                download_luts(aerosol_types=["rayleigh_only"])
                _rayleigh = Rayleigh("GOES-19", "abi", atmosphere="us-standard",
                                     aerosol_type="rayleigh_only")
        return _rayleigh


# ---------------------------------------------------------------- quality checks

class BadScan(ValueError):
    """A scan that fails a quality check and should not become a frame."""


# Share of the box a band may be missing before the scan is rejected. A failed band
# arrives as all fill values, so the real cases are far above this.
MAX_MISSING = 0.05
# Plausible brightness temperatures, kelvin, for the median over the box.
BT_RANGE = (180.0, 320.0)


def check_band(name: str, a: np.ndarray, valid: np.ndarray) -> None:
    vals = a[valid]
    missing = float(np.mean(~np.isfinite(vals))) if vals.size else 1.0
    if missing > MAX_MISSING:
        raise BadScan(f"{name} missing over {missing:.0%} of the box")
    if name in ("C07", "C13"):
        med = float(np.nanmedian(vals))
        if not BT_RANGE[0] <= med <= BT_RANGE[1]:
            raise BadScan(f"{name} median {med:.1f} K is outside {BT_RANGE}")


def brightness_outliers(means: list[float], tol: float = 0.25) -> list[int]:
    """Frames whose mean brightness jumps from the median of the 3 frames either side."""
    bad = []
    for i, m in enumerate(means):
        nb = means[max(0, i - 3):i] + means[i + 1:i + 4]
        if nb:
            med = float(np.median(nb))
            if med > 0 and abs(m - med) / med > tol:
                bad.append(i)
    return bad


# ---------------------------------------------------------------- compositing

def unsharp(a: np.ndarray, sigma: float, amount: float) -> np.ndarray:
    """Local contrast: push each pixel away from its blurred neighborhood."""
    return a + amount * (a - gaussian_filter(a, sigma))


def cira_stretch(pct: np.ndarray) -> np.ndarray:
    """CIRA's log stretch for true color (as in satpy), reflectance in percent to 0..1."""
    log_root = np.log10(0.0223)
    denom = (1.0 - log_root) * 0.75
    return (np.log10(np.clip(pct * 0.01, 1e-6, None)) - log_root) / denom


def night_layer(bands: dict[str, np.ndarray], lights: np.ndarray) -> np.ndarray:
    # IR clouds over city lights. Cold tops are opaque and bright. The local-contrast
    # boost brings out cloud texture without moving the overall temperature ramp.
    t13 = unsharp(bands["C13"], sigma=2.5, amount=0.9)
    # The ramp starts below clear-ground temperature so open land and sea stay dark.
    ir = np.clip((274 - t13) / (274 - 215), 0, 1)
    cloud_alpha = np.clip(ir * 1.6, 0, 1) ** 0.8
    cloud_gray = (0.40 + 0.60 * ir ** 0.85)[..., None]
    night = lights * (1 - cloud_alpha[..., None]) + cloud_gray * cloud_alpha[..., None]

    # Low cloud and fog: warm, so IR misses it, but the 10.3 - 3.9 um difference
    # is positive over water clouds at night. Tinted pale blue, as in GeoColor.
    btd = bands["C13"] - bands["C07"]
    low = np.clip((btd - 1.0) / 4.0, 0, 1) * (1 - cloud_alpha) * 0.55
    return night * (1 - low[..., None]) + low[..., None] * np.array([0.62, 0.72, 0.85])


def day_layer(bands: dict[str, np.ndarray], sza: np.ndarray, geom: Geometry,
              when: datetime, red_ratio: np.ndarray | None) -> np.ndarray:
    r, b, nir = bands["C02"] * 100, bands["C01"] * 100, bands["C03"] * 100  # percent

    # Sun-angle correction, capped at 80 degrees so the low sun near the terminator
    # keeps some of its natural fall-off.
    f = (1 / np.cos(np.radians(np.minimum(sza, 80.0)))).astype("float32")
    r, b, nir = r * f, b * f, nir * f

    # Rayleigh: remove the blue haze scattered by the air itself. Relaxed over
    # bright cloud, where the scattering is hidden, and tapered off from solar
    # zenith 70 to 88, where the full correction overshoots and turns land yellow.
    ray = rayleigh()
    azd = geom.azimuth_diff(when)
    sz = np.minimum(sza, 88.0).astype("float32")
    taper = np.clip((88.0 - sza) / 18.0, 0, 1).astype("float32")
    b = np.clip(b - taper * ray.get_reflectance(sz, geom.sat_zenith, azd, 0.47, r), 0, None)
    r = np.clip(r - taper * ray.get_reflectance(sz, geom.sat_zenith, azd, 0.64, r), 0, None)

    # Detail from the 0.5 km red band, applied to all three channels.
    if red_ratio is not None:
        r, b, nir = r * red_ratio, b * red_ratio, nir * red_ratio

    g = 0.45 * r + 0.45 * b + 0.1 * nir  # CIMSS synthetic green
    rgb = np.stack([r, g, b], -1)
    return np.clip(cira_stretch(rgb), 0, 1)


def compose(bands: dict[str, np.ndarray], lights: np.ndarray, sza: np.ndarray | None,
            geom: Geometry | None, when: datetime, red_ratio: np.ndarray | None) -> np.ndarray:
    night = night_layer(bands, lights)
    if sza is None or "C02" not in bands:
        return night
    day = day_layer(bands, sza, geom, when, red_ratio)
    w = np.clip((88 - sza) / 8, 0, 1)[..., None]  # blend from solar zenith 80 to 88
    return w * day + (1 - w) * night


# ---------------------------------------------------------------- windows

def _sun_series(lon: np.ndarray, lat: np.ndarray, evening: date):
    lo = np.concatenate([lon[::10, ::10].ravel(), lon[[0, 0, -1, -1], [0, -1, 0, -1]]])
    la = np.concatenate([lat[::10, ::10].ravel(), lat[[0, 0, -1, -1], [0, -1, 0, -1]]])
    t0 = datetime(evening.year, evening.month, evening.day, 16, tzinfo=timezone.utc)
    times = [t0 + timedelta(minutes=m) for m in range(0, 26 * 60, 5)]
    sz = np.array([solar_zenith(la, lo, t) for t in times])
    return times, sz.min(axis=1), sz.max(axis=1)


def night_window(lon: np.ndarray, lat: np.ndarray, evening: date,
                 sun_below: float) -> tuple[datetime, datetime]:
    """The dark span of the night that starts on `evening` (Eastern date).

    Dark means the sun's zenith angle is at least `sun_below` degrees at every
    point of the box. 90 is sunset; 96 is the end of civil twilight.
    """
    times, szmin, _ = _sun_series(lon, lat, evening)
    dark = szmin >= sun_below
    if not dark.any():
        raise SystemExit(f"no time on the night of {evening} is fully dark over the box")
    i0 = int(np.argmax(dark))
    i1 = i0 + int(np.argmin(dark[i0:])) - 1 if not dark[i0:].all() else len(dark) - 1
    return times[i0], times[i1]


def sunset_reaches_box(lon: np.ndarray, lat: np.ndarray, evening: date) -> datetime:
    """When the sun first sets anywhere in the box (its east edge) on `evening`."""
    times, _, szmax = _sun_series(lon, lat, evening)
    return times[int(np.argmax(szmax >= 90))]


def last_evening(lon: np.ndarray, lat: np.ndarray, sun_below: float) -> date:
    now = datetime.now(timezone.utc)
    today = now.astimezone(EASTERN).date()
    for back in (1, 2):
        _, end = night_window(lon, lat, today - timedelta(days=back), sun_below)
        if end <= now - timedelta(minutes=10):  # allow for NOAA's delivery delay
            return today - timedelta(days=back)
    raise SystemExit("no finished night found")


# ---------------------------------------------------------------- frames

class HiresRed:
    """The 0.5 km red band, averaged down to the output grid, as a ratio to the 2 km band."""

    def __init__(self, keys: dict[str, str], lon: np.ndarray, lat: np.ndarray):
        self.keys, self.lon, self.lat = keys, lon, lat
        self.sampler: Sampler | None = None
        self.lock = threading.Lock()

    def ratio(self, key: str, red_lo: np.ndarray, valid: np.ndarray) -> np.ndarray | None:
        hk = self.keys.get(key.split("_s")[1][:13])
        if hk is None:
            return None
        ds = open_scan(hk)
        with self.lock:
            if self.sampler is None:
                self.sampler = Sampler(ds, self.lon, self.lat)
        s = self.sampler
        a = s.window(ds)["CMI"].values.astype("float32")
        # Average 2 x 2 to 1 km first to keep memory down, then smooth to the output
        # pixel size so the sample is an area average, not one noisy 1 km pixel.
        h2, w2 = a.shape[0] // 2 * 2, a.shape[1] // 2 * 2
        a = a[:h2, :w2].reshape(h2 // 2, 2, w2 // 2, 2).mean(axis=(1, 3))
        a = np.nan_to_num(a, nan=0.0)
        sigma = max(0.0, 0.45 * s.step / 2 - 0.3)
        if sigma > 0:
            a = gaussian_filter(a, sigma)
        red_hi = map_coordinates(a, (s.coords - 0.5) / 2, order=1, mode="nearest")
        ok = valid & (red_lo > 0.03) & np.isfinite(red_hi)
        ratio = np.ones_like(red_lo)
        ratio[ok] = np.clip(red_hi[ok] / red_lo[ok], 0.6, 1.6)
        return ratio


def render(key: str, lights: np.ndarray, lon: np.ndarray, lat: np.ndarray, sampler: Sampler,
           day: bool, geom: Geometry | None, hires: HiresRed | None) -> tuple[Image.Image, datetime]:
    ds = open_scan(key)
    if sampler.key != (float(ds.x[0]), float(ds.y[0]), ds.sizes["x"], ds.sizes["y"]):
        raise BadScan("scan grid differs from the first scan")
    when = scan_start(key)
    win = sampler.window(ds)
    sza = solar_zenith(lat, lon, when) if day else None
    names = ["C07", "C13"]
    lit = sza is not None and (sza[sampler.valid] < 88).mean() > 0.002
    if lit:
        names += ["C01", "C02", "C03"]
    bands = {}
    for n in names:
        a = sampler.warp(win[f"CMI_{n}"].values)  # fill values stay NaN for the check
        check_band(n, a, sampler.valid)
        bands[n] = np.nan_to_num(a, nan=float(np.nanmedian(a[sampler.valid])))
    red_ratio = None
    if lit and hires is not None:
        try:
            red_ratio = hires.ratio(key, bands["C02"], sampler.valid)
        except Exception as e:  # the sharpening is optional; the frame still renders
            print(f"  0.5 km red band skipped for {key.rsplit('/', 1)[-1]}: {e}", flush=True)
    rgb = compose(bands, lights, sza if lit else None, geom, when, red_ratio)
    rgb[~sampler.valid] = 0
    im = Image.fromarray((np.clip(rgb, 0, 1) * 255).astype("uint8"))
    draw = ImageDraw.Draw(im)
    font = ImageFont.load_default(size=22)
    label = when.astimezone(EASTERN).strftime("%b %-d  %-I:%M %p %Z")
    draw.text((18, im.height - 40), label, fill=(235, 235, 235), font=font)
    return im, when


def write_outputs(frames_dir: Path, out: Path, formats: set[str], fps: float) -> None:
    if "mp4" in formats:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps),
             "-i", str(frames_dir / "%04d.png"),
             "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-crf", "24", "-movflags", "+faststart", str(out / "east_night.mp4")],
            check=True,
        )
    if formats & {"webp", "gif"}:
        frames = [Image.open(p) for p in sorted(frames_dir.glob("*.png"))]
        ms = round(1000 / fps)
        if "webp" in formats:
            frames[0].save(out / "east_night.webp", save_all=True, append_images=frames[1:],
                           duration=ms, loop=0, quality=70, method=4)
        if "gif" in formats:
            pal = [f.resize((f.width * 3 // 4, f.height * 3 // 4), Image.LANCZOS)
                   .quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
                   for f in frames]
            pal[0].save(out / "east_night.gif", save_all=True, append_images=pal[1:],
                        duration=ms, loop=0, optimize=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--night", type=date.fromisoformat,
                    help="Eastern date of the evening the night starts; default: the last full night")
    ap.add_argument("--afternoon", type=float,
                    help="start this many hours before sunset first reaches the box, "
                         "run through the night to first light, with the day layer")
    ap.add_argument("--sun-below", type=float, default=90.0,
                    help="solar zenith angle, degrees, that counts as dark (90 sunset, 96 civil dusk)")
    ap.add_argument("--day-night", action="store_true",
                    help="the last --hours ending now, with day and night blended")
    ap.add_argument("--hours", type=float, default=24, help="length of the --day-night loop")
    ap.add_argument("--no-hires", action="store_true", help="skip the 0.5 km red band detail")
    ap.add_argument("--max-dropped", type=float, default=0.1,
                    help="fail without writing videos when more than this share of frames is dropped")
    ap.add_argument("--start", help="UTC override, e.g. 2026-10-29T22:30")
    ap.add_argument("--end", help="UTC override")
    ap.add_argument("--every", type=int, default=10, help="minutes between frames")
    ap.add_argument("--fps", type=float, default=11, help="playback frames per second")
    ap.add_argument("--formats", default="mp4", help="comma list of mp4, webp, gif")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--bbox", default=DEFAULT_BBOX, help="lon_min,lat_min,lon_max,lat_max")
    ap.add_argument("--width", type=int, default=1400, help="output width in pixels")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    frames_dir = args.out / "frames"
    raw_dir = args.out / "frames_raw"

    t0 = time.time()
    grid = Grid(args.bbox, args.width)
    lights = grid.city_lights()
    lon, lat = grid.lonlat()
    print(f"grid {grid.width} x {grid.height}")

    day = args.day_night or args.afternoon is not None
    if args.start and args.end:
        start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    elif args.day_night:
        end = datetime.now(timezone.utc) - timedelta(minutes=15)  # NOAA delivery delay
        start = end - timedelta(hours=args.hours)
    else:
        evening = args.night or last_evening(lon, lat, args.sun_below)
        start, end = night_window(lon, lat, evening, args.sun_below)
        if args.afternoon is not None:
            start = sunset_reaches_box(lon, lat, evening) - timedelta(hours=args.afternoon)
    fmt = "%b %-d %-I:%M %p %Z"
    print(f"window: {start.astimezone(EASTERN):{fmt}} to {end.astimezone(EASTERN):{fmt}}"
          f" ({(end - start).total_seconds() / 3600:.1f} h)")
    slots = list_scans(start, end, args.every)
    print(f"{len(slots)} scans, lights in {time.time() - t0:.1f} s")

    # Shared by the workers.
    sampler = Sampler(open_scan(slots[0][0]), lon, lat)
    geom = Geometry(lon, lat, sampler.sat_lon) if day else None
    if day:
        rayleigh()
    hires = None if (not day or args.no_hires) else HiresRed(hires_red_keys(start, end), lon, lat)
    for d in (frames_dir, raw_dir):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir()

    def job(i_cands: tuple[int, list[str]]) -> dict:
        i, cands = i_cands
        rejected = []
        for key in cands:
            try:
                im, when = render(key, lights, lon, lat, sampler, day, geom, hires)
            except BadScan as e:
                rejected.append({"scan": key.rsplit("/", 1)[-1], "reason": str(e)})
                continue
            path = raw_dir / f"{i:04d}.png"
            im.save(path)
            mean = float(np.asarray(im.reduce(8), dtype="float32").mean())
            return {"slot": i, "scan": key.rsplit("/", 1)[-1], "time": when.isoformat(),
                    "path": str(path), "mean": mean, "rejected": rejected}
        return {"slot": i, "scan": None, "rejected": rejected}

    results = []
    with ThreadPoolExecutor(args.workers) as pool:
        for n, r in enumerate(pool.map(job, enumerate(slots)), 1):
            results.append(r)
            for rej in r["rejected"]:
                print(f"  rejected {rej['scan']}: {rej['reason']}", flush=True)
            if n % 10 == 0 or n == len(slots):
                print(f"{n}/{len(slots)} {time.time() - t0:.0f} s", flush=True)

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20
    print(f"frames done in {time.time() - t0:.0f} s, peak RSS {peak:.0f} MB")

    # Quality report: swapped scans, dropped slots, brightness jumps. In day-night
    # loops brightness changes fast at sunset, so the jump test is looser there.
    kept = [r for r in results if r["scan"]]
    for j in brightness_outliers([r["mean"] for r in kept], tol=0.4 if day else 0.25):
        kept[j]["outlier"] = True
        print(f"  dropped {kept[j]['scan']}: brightness jump ({kept[j]['mean']:.0f})")
    final = [r for r in kept if not r.get("outlier")]
    dropped = len(slots) - len(final)
    report = {
        "window_utc": [start.isoformat(), end.isoformat()],
        "slots": len(slots), "frames": len(final), "dropped": dropped,
        "swapped": [r for r in kept if r["rejected"]],
        "unfilled": [r for r in results if not r["scan"]],
        "outliers": [r for r in kept if r.get("outlier")],
    }
    (args.out / "report.json").write_text(json.dumps(report, indent=2))
    print(f"quality: {len(final)}/{len(slots)} frames kept, {len(report['swapped'])} swapped, "
          f"{dropped} dropped")
    if dropped > args.max_dropped * len(slots):
        raise SystemExit(f"too many bad frames ({dropped}/{len(slots)}); videos not written")
    for n, r in enumerate(final):
        shutil.copy(r["path"], frames_dir / f"{n:04d}.png")

    write_outputs(frames_dir, args.out, set(args.formats.split(",")), args.fps)


if __name__ == "__main__":
    main()
