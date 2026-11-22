"""Still frames of the two hero boxes in several projections, side by side.
An experiment: the daily job does not use this.

    uv run experiments/projection_examples.py out/projection-examples

Renders one daytime scan and one night scan of each box in Web Mercator (what the
loops use), the satellite's own view, orthographic, and near-side perspective from
5,000 km and 1,500 km. Each frame is the smallest rectangle in that projection that
holds the whole lon/lat box. Writes one PNG per frame and one comparison sheet per
box and time.
"""
import resource
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from pyproj import Proj
from scipy.ndimage import map_coordinates

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import geocolor_night as g  # noqa: E402

WIDTH = 1400
R = g.R_EARTH
BOXES = {
    "desktop": (-113.41, 23.44, -61.94, 49.12),
    "mobile": (-89.82, 19.4, -68.5, 49.99),
}
TIMES = {  # UTC: 1:00 PM and 10:00 PM Eastern on 2026-11-21
    "day": datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc),
    "night": datetime(2026, 11, 22, 3, 0, tzinfo=timezone.utc),
}


def frame(proj: Proj, box, width: int) -> tuple[np.ndarray, np.ndarray]:
    """Lon/lat of every pixel of the smallest rectangle (in the projection's plane)
    that holds the whole lon/lat box."""
    w, s, e, n = box
    t = np.linspace(0, 1, 400)
    elon = np.concatenate([w + (e - w) * t, np.full(400, e), e - (e - w) * t, np.full(400, w)])
    elat = np.concatenate([np.full(400, s), s + (n - s) * t, np.full(400, n), n - (n - s) * t])
    x, y = proj(elon, elat)
    xmin, xmax, ymin, ymax = np.min(x), np.max(x), np.min(y), np.max(y)
    height = round(width * (ymax - ymin) / (xmax - xmin))
    xs = xmin + (np.arange(width) + 0.5) * (xmax - xmin) / width
    ys = ymax - (np.arange(height) + 0.5) * (ymax - ymin) / height
    xx, yy = np.meshgrid(xs, ys)
    lon, lat = proj(xx, yy, inverse=True)
    lon, lat = np.asarray(lon, dtype="float64"), np.asarray(lat, dtype="float64")
    off = ~np.isfinite(lon) | ~np.isfinite(lat)  # past the horizon
    lon[off], lat[off] = np.nan, np.nan
    return lon, lat


def lights_for(lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    """Black Marble fetched in Web Mercator over the frame's lon/lat range, then
    sampled at each output pixel."""
    around = ",".join(f"{v:.4f}" for v in (np.nanmin(lon) - 0.5, max(np.nanmin(lat) - 0.5, -80),
                                           np.nanmax(lon) + 0.5, min(np.nanmax(lat) + 0.5, 80)))
    probe = g.Grid(around, 1000)
    src = g.Grid(around, min(2000, int(2600 * 1000 / max(1000, probe.height))))
    img = src.city_lights()
    x = R * np.radians(lon)
    y = R * np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))
    col = (x - src.xmin) / (src.xmax - src.xmin) * src.width - 0.5
    row = (src.ymax - y) / (src.ymax - src.ymin) * src.height - 0.5
    coords = np.nan_to_num(np.stack([row, col]))
    return np.stack([map_coordinates(img[..., c], coords, order=1, mode="nearest")
                     for c in range(3)], -1).astype("float32")


def projections(box, sat: dict) -> dict[str, tuple[str, Proj]]:
    w, s, e, n = box
    lon_0, lat_0 = (w + e) / 2, (s + n) / 2
    return {
        "1-mercator-now": ("Web Mercator (now)", Proj(proj="merc", a=R, b=R)),
        "2-satellite-view": (f"Native satellite view (from {abs(sat['lon_0']):.1f} W, equator)",
                             Proj(proj="geos", sweep="x", **sat)),
        "3-orthographic": (f"Orthographic, centered {lat_0:.1f} N {abs(lon_0):.1f} W",
                           Proj(proj="ortho", lat_0=lat_0, lon_0=lon_0, a=R, b=R)),
        "4-perspective-5000km": ("Near-side perspective, camera at 5,000 km",
                                 Proj(proj="nsper", h=5_000_000, lat_0=lat_0, lon_0=lon_0, a=R, b=R)),
        "5-perspective-1500km": ("Near-side perspective, camera at 1,500 km",
                                 Proj(proj="nsper", h=1_500_000, lat_0=lat_0, lon_0=lon_0, a=R, b=R)),
    }


def font(size: int):
    for p in ("/System/Library/Fonts/Helvetica.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass
    return ImageFont.load_default()


def sheet(panels: list[tuple[str, Image.Image]], cols: int, panel_w: int, path: Path) -> None:
    """The panels in a grid, each under its title."""
    cap, gap = 44, 16
    scaled = [(t, im.resize((panel_w, round(im.height * panel_w / im.width)), Image.LANCZOS))
              for t, im in panels]
    rows = [scaled[i:i + cols] for i in range(0, len(scaled), cols)]
    row_h = [max(im.height for _, im in r) + cap for r in rows]
    out = Image.new("RGB", (cols * panel_w + (cols + 1) * gap, sum(row_h) + (len(rows) + 1) * gap),
                    (28, 30, 34))
    draw, f, y = ImageDraw.Draw(out), font(22), gap
    for r, h in zip(rows, row_h):
        for c, (title, im) in enumerate(r):
            x = gap + c * (panel_w + gap)
            draw.text((x, y + 8), title, font=f, fill=(235, 235, 235))
            out.paste(im, (x, y + cap))
        y += h + gap
    out.save(path)


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    scans = {name: (g.list_scans(t, t, 10, reach=1)[0], g.hires_red_keys(t, t))
             for name, t in TIMES.items()}
    first = g.open_scan(scans["day"][0][0])
    # The satellite's position comes from the file, not from published figures.
    p = first["goes_imager_projection"].attrs
    sat = {"h": float(p["perspective_point_height"]), "lon_0": float(p["longitude_of_projection_origin"]),
           "a": float(p["semi_major_axis"]), "b": float(p["semi_minor_axis"])}
    g.rayleigh()

    for bname, box in BOXES.items():
        panels: dict[str, list] = {t: [] for t in TIMES}
        for pkey, (title, proj) in projections(box, sat).items():
            lon, lat = frame(proj, box, WIDTH)
            lights = lights_for(lon, lat)
            sampler = g.Sampler(first, lon, lat)
            geom = g.Geometry(lon, lat, sampler.sat_lon)
            print(f"{bname} {pkey}: {lon.shape[1]} x {lon.shape[0]}, "
                  f"{1 - sampler.valid.mean():.2%} of pixels outside the scan sector", flush=True)
            for tname, (cands, hkeys) in scans.items():
                hires = g.HiresRed(hkeys, lon, lat)
                for key in cands:
                    try:
                        im = g.render(key, lights, lon, lat, sampler, True, geom, hires,
                                      ("none", None))[0]
                        break
                    except g.BadScan as e:
                        print(f"  rejected {key.rsplit('/', 1)[-1]}: {e}", flush=True)
                else:
                    raise SystemExit(f"no usable scan for {tname}")
                im.save(out / f"{bname}_{tname}_{pkey}.png")
                panels[tname].append((f"{title}   ({im.width} x {im.height})", im))
        for tname, ps in panels.items():
            cols, panel_w = (2, 1100) if bname == "desktop" else (5, 560)
            sheet(ps, cols, panel_w, out / f"sheet_{bname}_{tname}.png")
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (2**20 if sys.platform == "darwin" else 2**10)
    print(f"done, peak RSS {peak:.0f} MB")


if __name__ == "__main__":
    main()
