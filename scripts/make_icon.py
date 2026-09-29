"""Draw the LocalAgent icon (a spark between code brackets, on an indigo-to-cyan rounded square).

Writes assets/localagent.ico (the shortcut and the desktop window) and frontend/public/icon.png
(the page's favicon and the logo in the top bar; ``npm run build`` copies it into the package).

No imaging library: the shapes are signed-distance fields sampled 4x per axis and averaged,
which is enough for smooth edges at icon sizes. Run it only when the icon changes; both files
are committed so setup does not need to generate anything.
"""

import math
import struct
import zlib
from pathlib import Path

SIZES = (16, 32, 48, 64, 128, 256)
PAGE_ICON_SIZE = 128
SUPERSAMPLE = 4
TOP_LEFT = (79, 70, 229)  # indigo
BOTTOM_RIGHT = (6, 182, 212)  # cyan
INK = (255, 255, 255)


def _rounded_box(x: float, y: float, half_w: float, half_h: float, radius: float) -> float:
    """Signed distance to a rounded rectangle centred on the origin (negative inside)."""
    dx, dy = abs(x) - (half_w - radius), abs(y) - (half_h - radius)
    outside = math.hypot(max(dx, 0.0), max(dy, 0.0))
    return outside + min(max(dx, dy), 0.0) - radius


def _segment(x: float, y: float, ax: float, ay: float, bx: float, by: float, width: float) -> float:
    """Signed distance to a thick line segment from (ax, ay) to (bx, by)."""
    px, py = x - ax, y - ay
    sx, sy = bx - ax, by - ay
    t = 0.0 if (sx * sx + sy * sy) == 0 else max(0.0, min(1.0, (px * sx + py * sy) / (sx * sx + sy * sy)))
    return math.hypot(px - sx * t, py - sy * t) - width


def _spark(x: float, y: float, radius: float) -> float:
    """Negative inside a four-pointed spark centred on the origin (a superellipse with concave sides)."""
    return (abs(x) / radius) ** 0.6 + (abs(y) / radius) ** 0.6 - 1


def _sample(u: float, v: float) -> tuple[bool, bool]:
    """Whether a point of the unit square is on the background, and whether it is on the white ink."""
    x, y = u * 2 - 1, v * 2 - 1  # -1..1
    background = _rounded_box(x, y, 1.0, 1.0, 0.46) <= 0
    stroke = 0.075
    brackets = min(
        _segment(x, y, -0.36, -0.36, -0.72, 0.0, stroke), _segment(x, y, -0.72, 0.0, -0.36, 0.36, stroke),
        _segment(x, y, 0.36, -0.36, 0.72, 0.0, stroke), _segment(x, y, 0.72, 0.0, 0.36, 0.36, stroke),
    )
    return background, brackets <= 0 or _spark(x, y, 0.36) <= 0


def render(size: int) -> bytes:
    """RGBA pixels for one icon size, as PNG scanlines."""
    step = 1.0 / (size * SUPERSAMPLE)
    rows = []
    for row in range(size):
        pixels = bytearray()
        for column in range(size):
            covered, colour = 0, [0.0, 0.0, 0.0]
            for sub_y in range(SUPERSAMPLE):
                for sub_x in range(SUPERSAMPLE):
                    u = (column * SUPERSAMPLE + sub_x + 0.5) * step
                    v = (row * SUPERSAMPLE + sub_y + 0.5) * step
                    background, ink = _sample(u, v)
                    if background:
                        covered += 1
                        t = (u + v) / 2  # the gradient runs along the diagonal
                        paint = INK if ink else [a + (b - a) * t for a, b in zip(TOP_LEFT, BOTTOM_RIGHT)]
                        colour = [c + p for c, p in zip(colour, paint)]
            average = [round(c / covered) for c in colour] if covered else [0, 0, 0]
            pixels += bytes(average) + bytes([round(255 * covered / SUPERSAMPLE ** 2)])
        rows.append(bytes(pixels))
    return b"".join(b"\x00" + r for r in rows)


def png(size: int, raw: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def ico(images: list[tuple[int, bytes]]) -> bytes:
    """An .ico holding PNG-encoded images (supported since Windows Vista)."""
    offset = 6 + 16 * len(images)
    directory, body = b"", b""
    for size, data in images:
        directory += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        body += data
        offset += len(data)
    return struct.pack("<HHH", 0, 1, len(images)) + directory + body


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    images = {size: png(size, render(size)) for size in SIZES}
    target = root / "assets" / "localagent.ico"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(ico(list(images.items())))
    print(f"wrote {target} ({target.stat().st_size:,} bytes, sizes {', '.join(str(s) for s in SIZES)})")
    page_icon = root / "frontend" / "public" / "icon.png"
    page_icon.parent.mkdir(parents=True, exist_ok=True)
    page_icon.write_bytes(images[PAGE_ICON_SIZE])
    print(f"wrote {page_icon}")


if __name__ == "__main__":
    main()
