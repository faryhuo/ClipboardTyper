"""Regenerate the vector logo and antialiased PNG/ICO assets using only stdlib."""
from pathlib import Path
import struct
import sys
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from clipboard_typer.ui.branding import ASSETS, LOGO_RECTS  # noqa: E402


def png(size):
    samples = 4
    shapes = [(a, b, c, d, r, tuple(bytes.fromhex(color[1:])))
              for a, b, c, d, r, color in LOGO_RECTS]
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            rgba = [0, 0, 0, 0]
            for sy in range(samples):
                for sx in range(samples):
                    px = (x+(sx+.5)/samples)*64/size
                    py = (y+(sy+.5)/samples)*64/size
                    pixel = None
                    for a, b, c, d, r, color in shapes:
                        if not (a <= px <= c and b <= py <= d):
                            continue
                        dx = max(a+r-px, 0, px-(c-r))
                        dy = max(b+r-py, 0, py-(d-r))
                        if dx*dx+dy*dy <= r*r:
                            pixel = color
                    if pixel:
                        for channel in range(3):
                            rgba[channel] += pixel[channel]
                        rgba[3] += 1
            count = rgba[3]
            raw.extend([round(v/count) if count else 0 for v in rgba[:3]])
            raw.append(round(count*255/(samples*samples)))
    def chunk(kind, data):
        return struct.pack(">I", len(data))+kind+data+struct.pack(">I", zlib.crc32(kind+data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main():
    ASSETS.mkdir(exist_ok=True)
    rects = "\n".join(f'<rect x="{a}" y="{b}" width="{c-a}" height="{d-b}" rx="{r}" fill="{color}"/>'
                      for a, b, c, d, r, color in LOGO_RECTS)
    (ASSETS / "logo.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">\n'+rects+'\n</svg>\n', encoding="utf-8")
    sizes = (16, 24, 32, 48, 64, 128, 256)
    images = [png(size) for size in sizes]
    offset = 6+16*len(sizes)
    directory = bytearray(struct.pack("<HHH", 0, 1, len(sizes)))
    for size, data in zip(sizes, images):
        directory.extend(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
        if size in (32, 64, 256):
            (ASSETS / f"logo-{size}.png").write_bytes(data)
    (ASSETS / "clipboard-typer.ico").write_bytes(directory+b"".join(images))
    print(f"Brand assets generated in {ASSETS}")


if __name__ == "__main__":
    main()
