"""Pinned boundary datasets: where they come from, which exact version, and the checksum they must have.

Files live in GEO_DATA_DIR/raw (default worker/data/geo; the Docker image fetches them at build time).
A file whose sha256 doesn't match is never used: it is downloaded again, and if that still doesn't
match, the job fails with a clear error. That makes "boundary version" a precise fact we can record.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import httpx


@dataclass(frozen=True)
class Dataset:
    key: str
    url: str
    sha256: str
    version: str
    license: str

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[1]

    @property
    def label(self) -> str:
        """Short, exact version string stored on jobs (boundary version)."""
        return f"{self.key}@{self.version}#{self.sha256[:12]}"


NE_LICENSE = "Public domain (Natural Earth, https://www.naturalearthdata.com/about/terms-of-use/)"
# Natural Earth 10m cultural vectors, downloaded 2026-11-14 from the official CDN.
DATASETS: dict[str, Dataset] = {
    "ne_admin0": Dataset(
        "ne_admin0", "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_countries.zip",
        "ce1ac7036499a0edd641fbc093cd209a98f96a49d2eca8480aaacad35138a7f6", "5.1.1", NE_LICENSE),
    "ne_admin1": Dataset(
        "ne_admin1", "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_1_states_provinces.zip",
        "efc59726337323058f9446210adc96673179cd344e053666ee3d28cb58ba2b05", "5.1.1", NE_LICENSE),
    "ne_urban": Dataset(
        "ne_urban", "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_urban_areas.zip",
        "2c593114e954ce1adb297ddee5465f05d9e81fa0a26deb13f8500d58a00d7051", "4.0.0", NE_LICENSE),
}


class GeoDataError(RuntimeError):
    """Boundary data missing or corrupt. The message is safe to show."""


def data_dir() -> Path:
    default = Path(__file__).resolve().parents[2] / "data" / "geo"
    return Path(os.getenv("GEO_DATA_DIR", str(default)))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure(ds: Dataset, *, root: Path | None = None, client: httpx.Client | None = None) -> Path:
    """Path of a verified copy of the dataset, downloading it if needed."""
    raw = (root or data_dir()) / "raw"
    path = raw / ds.filename
    if path.exists() and sha256_file(path) == ds.sha256:
        return path
    raw.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True,
                                    headers={"User-Agent": "LeadFinder-geodata/1.0"})
    try:
        with client.stream("GET", ds.url) as resp:
            if resp.status_code != 200:
                raise GeoDataError(f"Couldn't download {ds.filename} (HTTP {resp.status_code}).")
            with tmp.open("wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
    except httpx.HTTPError as exc:
        raise GeoDataError(f"Couldn't download {ds.filename}: {type(exc).__name__}") from exc
    finally:
        if own:
            client.close()
    got = sha256_file(tmp)
    if got != ds.sha256:
        tmp.unlink(missing_ok=True)
        raise GeoDataError(f"{ds.filename} has an unexpected checksum ({got[:12]}...); refusing to use it.")
    tmp.replace(path)
    return path


def ensure_all(root: Path | None = None) -> dict[str, Path]:
    return {k: ensure(ds, root=root) for k, ds in DATASETS.items()}


if __name__ == "__main__":  # python -m app.geo.data  (used by the Dockerfile at build time)
    for key, p in ensure_all().items():
        print(f"{key}: {p} OK ({DATASETS[key].label})")
