"""Linux fbdev inspection and bounded RGB565 drawing."""
from array import array
import ctypes as c
from dataclasses import dataclass
import fcntl
import mmap
import os
from pathlib import Path
import re
import sys


FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602


class Bitfield(c.Structure):
    _fields_ = [(name, c.c_uint32) for name in ("offset", "length", "msb_right")]

    def values(self):
        return self.offset, self.length, self.msb_right


class VarInfo(c.Structure):
    _fields_ = (
        [(name, c.c_uint32) for name in
         ("xres", "yres", "xres_virtual", "yres_virtual", "xoffset", "yoffset",
          "bits_per_pixel", "grayscale")]
        + [(name, Bitfield) for name in ("red", "green", "blue", "transp")]
        + [(name, c.c_uint32) for name in
           ("nonstd", "activate", "height", "width", "accel_flags", "pixclock",
            "left_margin", "right_margin", "upper_margin", "lower_margin",
            "hsync_len", "vsync_len", "sync", "vmode", "rotate", "colorspace")]
        + [("reserved", c.c_uint32 * 4)]
    )


class FixInfo(c.Structure):
    _fields_ = [
        ("id", c.c_char * 16), ("smem_start", c.c_ulong), ("smem_len", c.c_uint32),
        ("type", c.c_uint32), ("type_aux", c.c_uint32), ("visual", c.c_uint32),
        ("xpanstep", c.c_uint16), ("ypanstep", c.c_uint16), ("ywrapstep", c.c_uint16),
        ("line_length", c.c_uint32), ("mmio_start", c.c_ulong), ("mmio_len", c.c_uint32),
        ("accel", c.c_uint32), ("capabilities", c.c_uint16), ("reserved", c.c_uint16 * 2),
    ]


@dataclass(frozen=True)
class Layout:
    width: int
    height: int
    stride: int
    xoffset: int
    yoffset: int
    size: int

    @classmethod
    def from_info(cls, fixed, var):
        if (fixed.type, fixed.visual, var.bits_per_pixel, var.grayscale, var.nonstd) != (0, 2, 16, 0, 0):
            raise ValueError("Formato não suportado: esperado RGB565 packed truecolor de 16 bpp.")
        if ([var.red.values(), var.green.values(), var.blue.values()]
                != [(11, 5, 0), (5, 6, 0), (0, 5, 0)] or var.transp.length):
            raise ValueError("Bitfields incompatíveis com RGB565.")
        if (var.xoffset + var.xres > var.xres_virtual or var.yoffset + var.yres > var.yres_virtual
                or var.vmode & 256):  # FB_VMODE_YWRAP
            raise ValueError("Janela do framebuffer fora dos limites virtuais ou com YWRAP.")
        layout = cls(var.xres, var.yres, fixed.line_length, var.xoffset, var.yoffset, fixed.smem_len)
        layout.validate(layout.size)
        return layout

    def validate(self, length):
        row_end = (self.xoffset + self.width) * 2
        end = (self.yoffset + self.height - 1) * self.stride + row_end
        if (min(self.width, self.height, self.stride) <= 0 or min(self.xoffset, self.yoffset) < 0
                or row_end > self.stride or end > min(length, self.size)):
            raise ValueError("Stride, offsets ou tamanho do framebuffer inválidos.")


def rgb565(rgb, byteorder=sys.byteorder):
    if len(rgb) % 3 or byteorder not in ("little", "big"):
        raise ValueError("Esperado RGB de 8 bits por canal e ordem little ou big.")
    pixels = ((red >> 3) << 11 | (green >> 2) << 5 | blue >> 3
              for red, green, blue in zip(rgb[0::3], rgb[1::3], rgb[2::3]))
    packed = array("H", pixels)
    if byteorder != sys.byteorder:
        packed.byteswap()
    return packed.tobytes()


def draw_rgb(buffer, layout, rgb, byteorder=sys.byteorder):
    layout.validate(len(buffer))
    if len(rgb) != layout.width * layout.height * 3:
        raise ValueError("Imagem não corresponde à área visível do framebuffer.")
    row_size = layout.width * 2
    packed = rgb565(rgb, byteorder)
    start = layout.yoffset * layout.stride + layout.xoffset * 2
    for row in range(layout.height):
        offset = start + row * layout.stride
        buffer[offset:offset + row_size] = packed[row * row_size:(row + 1) * row_size]


def candidates(sysfs=Path("/sys/class/graphics"), devices=Path("/dev")):
    found = []
    for fb in sorted(sysfs.glob("fb*")):
        device = devices / fb.name
        if not re.fullmatch(r"fb[0-9]+", fb.name) or not device.exists():
            continue
        spi_device = (fb / "device").resolve()
        if not any(part.startswith("spi") and re.fullmatch(r"spi\d+\.\d+", part)
                   for part in spi_device.parts):
            continue
        try:
            name = (fb / "name").read_text().lower()
        except OSError:
            continue
        if "ili9486" in name:
            found.append(device)
    return found


def select_fb(explicit=None):
    if explicit is not None:
        if not re.fullmatch(r"/dev/fb[0-9]+", os.fspath(explicit)):
            raise ValueError("Informe um framebuffer no formato /dev/fbN.")
        return Path(explicit)
    found = candidates()
    if len(found) != 1:
        raise ValueError(f"Encontrados {len(found)} framebuffers SPI ILI9486. Consulte LCD35-lite-status e use --fb.")
    return found[0]


class Framebuffer:
    def __init__(self, path=None, writable=True):
        self.path = select_fb(path)
        self.writable = writable

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR if self.writable else os.O_RDONLY)
        try:
            self.fixed, self.var = FixInfo(), VarInfo()
            fcntl.ioctl(self.fd, FBIOGET_FSCREENINFO, self.fixed)
            fcntl.ioctl(self.fd, FBIOGET_VSCREENINFO, self.var)
            if self.writable:
                self.layout = Layout.from_info(self.fixed, self.var)
                self.buffer = mmap.mmap(self.fd, self.layout.size, access=mmap.ACCESS_WRITE)
        except BaseException:
            os.close(self.fd)
            raise
        return self

    def describe(self):
        fix, var = self.fixed, self.var
        fields = [getattr(var, field).values() for field in ("red", "green", "blue", "transp")]
        return (f"{self.path}: {fix.id.decode(errors='replace')} {var.xres}×{var.yres}"
                f" virtual={var.xres_virtual}×{var.yres_virtual}"
                f" offset={var.xoffset},{var.yoffset} bpp={var.bits_per_pixel}"
                f" stride={fix.line_length} memória={fix.smem_len} tipo={fix.type} visual={fix.visual}"
                f" bitfields RGBA (offset, bits, msb_right)={fields}")

    def draw(self, image):
        if image.size != (self.layout.width, self.layout.height):
            raise ValueError("Dimensões da imagem não correspondem ao framebuffer.")
        draw_rgb(self.buffer, self.layout, image.convert("RGB").tobytes())

    def __exit__(self, *_):
        try:
            if self.writable:
                self.buffer.close()
        finally:
            os.close(self.fd)
