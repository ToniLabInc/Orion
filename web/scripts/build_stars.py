"""Horizon's sky: the Yale Bright Star Catalogue, packed for the HUD.

Fetches the Bright Star Catalogue, 5th Revised Ed. (Hoffleit and Warren 1991; CDS catalogue V/50) once, checks it is
the file this script was written for, and writes web/src/core/stars.bin: every star of visual magnitude 6.5 or
brighter (8 404 of its 9 110 entries) with its J2000 position, its magnitude and its B−V colour, brightest first (so a
prefix is every star down to a magnitude), and the lines of the Orion figure between them, in the order they are
drawn. The engine reads it (src/core/sky.ts).

    uv run --no-project python web/scripts/build_stars.py    # the standard library alone

The file is little-endian, one array per field (each compresses well on its own):

    "HZSK", u8 version (1), u8 0, u16 stars N, u16 lines L, u16 0
    N x u16  right ascension, as a fraction of 24 h x 65 536 (20 arcsecond steps)
    N x i16  declination, degrees x 32 767 / 90 (10 arcsecond steps)
    N x u8   visual magnitude, (V + 2) x 25 (0.04 steps)
    N x i8   B-V, x 50 (0.02 steps), within -0.4 .. 2.5
    L x 2 x u16  the figure's lines, as indices into the stars

310 faint stars have no B-V in the catalogue: theirs is the main sequence's mean for their spectral class and
subclass (`INTRINSIC`), so a planetarium would show them in about the colour they have. Positions are J2000 without
proper motion: the fastest star moves about two arcminutes in 26 years, a fraction of a pixel on the HUD.

Acknowledgement (for the README's licences): the Bright Star Catalogue, 5th Revised Ed. (Preliminary Version),
Hoffleit D., Warren Jr W.H., Astronomical Data Center, NSSDC/ADC (1991), from the CDS, Strasbourg (VizieR V/50).
"""

import gzip
import hashlib
import re
import struct
import urllib.request
from itertools import pairwise
from pathlib import Path
from typing import Final, NamedTuple

URL: Final = "https://cdsarc.cds.unistra.fr/ftp/V/50/catalog.gz"
SHA256: Final = "3dc44b1e90be8fbe5bcc7656032560f51275f985c7e3f783c9028e1838ec7bed"  # the file this was written for
OUT: Final = Path(__file__).resolve().parents[1] / "src" / "core" / "stars.bin"
LIMIT: Final = 6.5  # the faintest magnitude kept: what an eye sees on a dark night
BV_RANGE: Final = (-0.4, 2.5)  # beyond it (a few carbon stars, to +5.7) the colour no longer changes on screen

# The figure, by Harvard Revised number, in the order it draws itself: the head to the shoulders, the body, the belt
# and the legs; the raised arm and its club; the shield; the sword hanging below the belt, through M42.
FIGURE: Final = (
    (1879, 2061),  # Meissa - Betelgeuse
    (1879, 1790),  # Meissa - Bellatrix
    (2061, 1948),  # Betelgeuse - Alnitak
    (1790, 1852),  # Bellatrix - Mintaka
    (1948, 1903),  # Alnitak - Alnilam
    (1903, 1852),  # Alnilam - Mintaka
    (1948, 2004),  # Alnitak - Saiph
    (1852, 1713),  # Mintaka - Rigel
    (2061, 2124),  # Betelgeuse - mu Ori
    (2124, 2199),  # mu - xi Ori
    (2199, 2159),  # xi - nu Ori
    (2159, 2135),  # nu - chi2 Ori
    (2135, 2047),  # chi2 - chi1 Ori
    (1790, 1543),  # Bellatrix - pi3 Ori
    (1570, 1544),  # pi1 - pi2 Ori
    (1544, 1543),  # pi2 - pi3 Ori
    (1543, 1552),  # pi3 - pi4 Ori
    (1552, 1567),  # pi4 - pi5 Ori
    (1567, 1601),  # pi5 - pi6 Ori
    (1892, 1895),  # 42 Ori - theta1 Ori C (the Trapezium)
    (1895, 1899),  # theta1 Ori C - iota Ori (Hatysa)
)

# Main-sequence B-V by spectral class and subclass (Schmidt-Kaler 1982, as tabulated by Pecaut and Mamajek 2013),
# interpolated between the subclasses given, each class running on to the next one's first (B9.5 is nearly A0).
INTRINSIC: Final = {
    "O": ((5, -0.33), (9, -0.31), (10, -0.30)),
    "B": ((0, -0.30), (2, -0.24), (5, -0.17), (8, -0.11), (10, 0.00)),
    "A": ((0, 0.00), (2, 0.05), (5, 0.15), (7, 0.20), (10, 0.30)),
    "F": ((0, 0.30), (2, 0.35), (5, 0.44), (8, 0.52), (10, 0.58)),
    "G": ((0, 0.58), (2, 0.63), (5, 0.68), (8, 0.74), (10, 0.81)),
    "K": ((0, 0.81), (2, 0.91), (5, 1.15), (7, 1.33), (10, 1.40)),
    "M": ((0, 1.40), (2, 1.49), (5, 1.64)),
}
CLASS: Final = re.compile(r"([OBAFGKM])(\d(?:\.\d)?)")


class Star(NamedTuple):
    hr: int
    ra: float  # hours, J2000
    dec: float  # degrees, J2000
    vmag: float
    bv: float


def fetch() -> bytes:
    """The catalogue's text, checked against the file this script was written for.

    Returns:
        The catalogue, 9 110 lines of 197 characters.

    Raises:
        SystemExit: when the download's checksum differs.
    """
    with urllib.request.urlopen(URL, timeout=60) as response:
        packed = response.read()
    digest = hashlib.sha256(packed).hexdigest()
    if digest != SHA256:
        raise SystemExit(f"{URL} is not the file this script reads (sha256 {digest}); check its ReadMe and update")
    return gzip.decompress(packed)


def intrinsic(sptype: str) -> float:
    """The main sequence's B-V for a spectral type such as "K2III", "sgG6" or "B9.5V".

    Args:
        sptype: the catalogue's spectral type.

    Returns:
        B-V, interpolated between the table's subclasses.

    Raises:
        ValueError: when the type names no class from O to M.
    """
    found = CLASS.search(sptype)
    if not found:
        raise ValueError(f"no spectral class in {sptype!r}")
    points = INTRINSIC[found.group(1)]
    sub = float(found.group(2))
    if sub <= points[0][0]:
        return points[0][1]
    for (s0, b0), (s1, b1) in pairwise(points):
        if sub <= s1:
            return b0 + (b1 - b0) * (sub - s0) / (s1 - s0)
    return points[-1][1]


def parse(text: bytes) -> list[Star]:
    """Every star of the catalogue with a J2000 position and a magnitude no fainter than LIMIT.

    Args:
        text: the catalogue (bytes 76-90 the J2000 position, 103-107 V, 110-114 B-V, 128-147 the spectral type).

    Returns:
        The stars, brightest first (ties by HR number).
    """
    stars = []
    for raw in text.decode("ascii").splitlines():
        line = raw.ljust(197)
        if not line[75:83].strip() or not line[102:107].strip():
            continue  # one of the 14 entries that are novae or galaxies, kept only for the numbering
        vmag = float(line[102:107])
        if vmag > LIMIT:
            continue
        ra = int(line[75:77]) + int(line[77:79]) / 60 + float(line[79:83]) / 3600
        dec = int(line[84:86]) + int(line[86:88]) / 60 + int(line[88:90]) / 3600
        bv = float(line[109:114]) if line[109:114].strip() else intrinsic(line[127:147])
        stars.append(Star(int(line[0:4]), ra, -dec if line[83] == "-" else dec, vmag, bv))
    stars.sort(key=lambda s: (s.vmag, s.hr))
    return stars


def pack(stars: list[Star]) -> bytes:
    """The binary the engine reads (see the module docstring).

    Args:
        stars: the stars, in the order they are stored.

    Returns:
        The file's bytes.

    Raises:
        SystemExit: when a star of the figure isn't among them.
    """
    index = {s.hr: i for i, s in enumerate(stars)}
    try:
        lines = [(index[a], index[b]) for a, b in FIGURE]
    except KeyError as missing:
        raise SystemExit(f"the figure's star HR {missing} is fainter than {LIMIT} or missing") from None
    n = len(stars)
    out = bytearray(b"HZSK" + struct.pack("<BBHHH", 1, 0, n, len(lines), 0))
    out += struct.pack(f"<{n}H", *(round(s.ra / 24 * 65536) % 65536 for s in stars))
    out += struct.pack(f"<{n}h", *(round(s.dec / 90 * 32767) for s in stars))
    out += struct.pack(f"<{n}B", *(round((s.vmag + 2) * 25) for s in stars))
    out += struct.pack(f"<{n}b", *(round(min(max(s.bv, BV_RANGE[0]), BV_RANGE[1]) * 50) for s in stars))
    out += struct.pack(f"<{2 * len(lines)}H", *(i for line in lines for i in line))
    return bytes(out)


def main() -> None:
    stars = parse(fetch())
    data = pack(stars)
    OUT.write_bytes(data)
    print(f"{len(stars)} stars to magnitude {LIMIT}, {len(FIGURE)} figure lines: {OUT} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
