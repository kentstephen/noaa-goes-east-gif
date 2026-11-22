"""A loop in near-side perspective (the globe seen from a camera above the box)
instead of Web Mercator. The daily job renders both hero loops with this.

    uv run experiments/perspective_loop.py --camera-km 5000 --fill --trim-top 78 \
        --seconds 8 --day-seconds 2 --label none \
        --bbox=-113.41,23.44,-61.94,49.12 --out out/perspective/desktop-5000km

--camera-km   camera height above the center of the box. Lower bulges more:
              5000 is close to a flat map over a box the size of the US, 1500 is
              a clear fisheye.
--fill        trim the frame to the largest rectangle with scan data in every
              pixel. Without it the frame holds the whole lon/lat box, and its
              corners can pass the edge of the scan sector (black), or at low
              camera heights the horizon.
--trim-top    rows to cut off the top of the frame, counted in output pixels after
              --fill. Makes the frame wider in shape and keeps its south edge.

Every other argument goes to geocolor_night.py unchanged. This file swaps the
output grid and runs geocolor_night.main(); the color recipe, the scan checks and
the video writing are the ones the daily job uses.
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from pyproj import Proj
from scipy.ndimage import binary_erosion, map_coordinates

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import geocolor_night as g  # noqa: E402

MercatorGrid = g.Grid
R = g.R_EARTH


def box_edge(w: float, s: float, e: float, n: float, k: int = 400):
    """Points along the four sides of a lon/lat box."""
    t = np.linspace(0, 1, k)
    lon = np.concatenate([w + (e - w) * t, np.full(k, e), e - (e - w) * t, np.full(k, w)])
    lat = np.concatenate([np.full(k, s), s + (n - s) * t, np.full(k, n), n - (n - s) * t])
    return lon, lat


def largest_rectangle(ok: np.ndarray) -> tuple[int, int, int, int]:
    """Row, column, height and width of the largest all-True rectangle."""
    rows, cols = ok.shape
    run = np.zeros(cols, int)  # all-True pixels above and including this row
    best, where = 0, (0, 0, rows, cols)
    for r in range(rows):
        run = np.where(ok[r], run + 1, 0)
        stack: list[tuple[int, int]] = []
        for c in range(cols + 1):
            cur = int(run[c]) if c < cols else 0
            start = c
            while stack and stack[-1][1] >= cur:
                start, h = stack.pop()
                if h * (c - start) > best:
                    best, where = h * (c - start), (r - h + 1, start, h, c - start)
            stack.append((start, cur))
    return where


def recent_scan():
    """The newest scan that opens, for the extent of the scan sector."""
    now = datetime.now(timezone.utc)
    for cands in reversed(g.list_scans(now - timedelta(hours=2), now - timedelta(minutes=20), 10)):
        for key in cands:
            try:
                return g.open_scan(key)
            except Exception as e:
                print(f"  not opened: {key.rsplit('/', 1)[-1]}: {g.describe_error(e)}", flush=True)
    raise SystemExit("no recent scan could be opened to find the edge of the sector")


class PerspectiveGrid:
    """The output grid: near-side perspective centered on the box, `width` pixels wide."""

    camera_m = 5_000_000.0
    fill = False
    trim_top = 0

    def __init__(self, bbox: str, width: int):
        w, s, e, n = map(float, bbox.split(","))
        self.bbox, self.width = bbox, width
        self.proj = Proj(proj="nsper", h=self.camera_m, lat_0=(s + n) / 2, lon_0=(w + e) / 2,
                         a=R, b=R)
        x, y = self.proj(*box_edge(w, s, e, n))
        self._set(np.min(x), np.max(x), np.min(y), np.max(y))
        if self.fill:
            self._trim_to_scan()
        if self.trim_top:
            px = (self.xmax - self.xmin) / self.width
            self._set(self.xmin, self.xmax, self.ymin, self.ymax - self.trim_top * px)

    def _set(self, xmin: float, xmax: float, ymin: float, ymax: float) -> None:
        self.xmin, self.xmax, self.ymin, self.ymax = xmin, xmax, ymin, ymax
        self.height = round(self.width * (ymax - ymin) / (xmax - xmin))

    def _trim_to_scan(self) -> None:
        lon, lat = self.lonlat()
        valid = g.Sampler(recent_scan(), lon, lat).valid
        # A 3 pixel margin, so the trimmed edge is not the last row of the scan.
        r0, c0, h, w = largest_rectangle(binary_erosion(valid, iterations=3, border_value=1))
        px = (self.xmax - self.xmin) / self.width
        print(f"fill: {100 * (~valid).mean():.2f}% of the frame had no scan data; trimmed "
              f"{c0} left, {self.width - c0 - w} right, {r0} top, {self.height - r0 - h} bottom "
              f"of {self.width} x {self.height}")
        self._set(self.xmin + c0 * px, self.xmin + (c0 + w) * px,
                  self.ymax - (r0 + h) * px, self.ymax - r0 * px)

    def lonlat(self) -> tuple[np.ndarray, np.ndarray]:
        xs = self.xmin + (np.arange(self.width) + 0.5) * (self.xmax - self.xmin) / self.width
        ys = self.ymax - (np.arange(self.height) + 0.5) * (self.ymax - self.ymin) / self.height
        xx, yy = np.meshgrid(xs, ys)
        lon, lat = self.proj(xx, yy, inverse=True)
        lon, lat = np.asarray(lon, dtype="float64"), np.asarray(lat, dtype="float64")
        off = ~np.isfinite(lon) | ~np.isfinite(lat)  # past the horizon
        lon[off], lat[off] = np.nan, np.nan
        return lon, lat

    def city_lights(self) -> np.ndarray:
        """Black Marble fetched in Web Mercator over the frame's lon/lat range, then
        sampled at each output pixel."""
        lon, lat = self.lonlat()
        around = ",".join(f"{v:.4f}" for v in (np.nanmin(lon) - 0.5, np.nanmin(lat) - 0.5,
                                               np.nanmax(lon) + 0.5, np.nanmax(lat) + 0.5))
        # Up to 2000 wide, and no taller than 2600, which the map server accepts.
        probe = MercatorGrid(around, 1000)
        src = MercatorGrid(around, min(2000, int(2600 * 1000 / max(1000, probe.height))))
        img = src.city_lights()
        x = R * np.radians(lon)
        y = R * np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))
        col = (x - src.xmin) / (src.xmax - src.xmin) * src.width - 0.5
        row = (src.ymax - y) / (src.ymax - src.ymin) * src.height - 0.5
        coords = np.nan_to_num(np.stack([row, col]))
        return np.stack([map_coordinates(img[..., c], coords, order=1, mode="nearest")
                         for c in range(3)], -1).astype("float32")


def time_by_the_box(fn, bbox: str, width: int):
    """Run a loop-window function on the Mercator box, not on the perspective frame.

    The frame reaches past the box, so sunset reaches its corners earlier, and a
    corner past the horizon has no sun angle at all. Timed off the frame, the loop
    would start up to hours early. Timed off the box, it covers the same scans as
    the Mercator loop of that box.
    """
    lon, lat = MercatorGrid(bbox, width).lonlat()
    return lambda _lon, _lat, *a, **k: fn(lon, lat, *a, **k)


def main() -> None:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--camera-km", type=float, default=5000)
    ap.add_argument("--fill", action="store_true")
    ap.add_argument("--trim-top", type=int, default=0)
    ap.add_argument("--bbox", default=g.DEFAULT_BBOX)
    ap.add_argument("--width", type=int, default=1400)
    own, rest = ap.parse_known_args()

    PerspectiveGrid.camera_m = own.camera_km * 1000
    PerspectiveGrid.fill = own.fill
    PerspectiveGrid.trim_top = own.trim_top
    g.Grid = PerspectiveGrid
    for name in ("timed_window", "night_window", "last_evening", "sunset_reaches_box"):
        setattr(g, name, time_by_the_box(getattr(g, name), own.bbox, own.width))
    sys.argv = ["geocolor_night.py", f"--bbox={own.bbox}", "--width", str(own.width), *rest]
    g.main()


if __name__ == "__main__":
    main()
