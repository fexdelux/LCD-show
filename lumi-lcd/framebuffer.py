"""Linux fbdev inspection and bounded RGB565 drawing."""
from array import array
import ctypes as c
from dataclasses import dataclass
import fcntl
import mmap
import os
from pathlib import Path
import re
import stat
import sys


FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
GRAPHICS = Path("/sys/class/graphics")
PANEL_DRIVER = "fb_ili9486"


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
        # Raspberry Pi Linux fbtft sets nonstd=1 even with the RGB565 bitfields below.
        native_fbtft = fixed.id == PANEL_DRIVER.encode() and var.nonstd == 1
        if ((fixed.type, fixed.visual, var.bits_per_pixel, var.grayscale) != (0, 2, 16, 0)
                or (var.nonstd and not native_fbtft)):
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


def spi_panel(entry):
    """Require both the fbdev identity and the bound SPI parent's driver."""
    try:
        if (entry / "name").read_text().strip().lower() != PANEL_DRIVER:
            return False
        device = (entry / "device").resolve(strict=True)
        for parent in (device, *device.parents):
            if re.fullmatch(r"spi[0-9]+\.[0-9]+", parent.name):
                return (parent / "driver").resolve(strict=True).name == PANEL_DRIVER
    except OSError:
        pass
    return False


def candidates(sysfs=GRAPHICS, devices=Path("/dev")):
    return [devices / entry.name for entry in sorted(sysfs.glob("fb*"))
            if re.fullmatch(r"fb[0-9]+", entry.name) and spi_panel(entry)
            and (devices / entry.name).exists()]


def select_fb(explicit=None):
    if explicit is not None:
        if not re.fullmatch(r"/dev/fb[0-9]+", os.fspath(explicit)):
            raise ValueError("Informe um framebuffer no formato /dev/fbN.")
        return Path(explicit)
    found = candidates()
    if not found:
        raise ValueError("Nenhum framebuffer SPI ILI9486 disponível. Consulte LCD35-lite-status após o boot.")
    if len(found) > 1:
        raise ValueError("Vários framebuffers SPI ILI9486. Consulte LCD35-lite-status e use --fb /dev/fbN.")
    return found[0]


def verify_write_target(path, fd):
    entry = GRAPHICS / path.name
    info = os.fstat(fd)
    if not stat.S_ISCHR(info.st_mode) or not spi_panel(entry):
        raise ValueError(f"{path} não foi identificado como framebuffer SPI ILI9486.")
    actual = f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"
    if (entry / "dev").read_text().strip() != actual:
        raise ValueError(f"{path} não corresponde ao dispositivo identificado no sysfs.")


class Framebuffer:
    def __init__(self, path=None, writable=True):
        self.path = select_fb(path)
        self.writable = writable

    def __enter__(self):
        try:
            flags = os.O_RDWR if self.writable else os.O_RDONLY
            self.fd = os.open(self.path, flags | os.O_NOFOLLOW)
        except PermissionError as error:
            access = "leitura e gravação" if self.writable else "leitura"
            raise PermissionError(f"Sem acesso de {access} a {self.path}. Confira as permissões do dispositivo.") from error
        try:
            if self.writable:
                verify_write_target(self.path, self.fd)
            self.fixed, self.var = FixInfo(), VarInfo()
            fcntl.ioctl(self.fd, FBIOGET_FSCREENINFO, self.fixed)
            fcntl.ioctl(self.fd, FBIOGET_VSCREENINFO, self.var)
            if self.writable:
                if self.fixed.id.decode(errors="replace").lower() != PANEL_DRIVER:
                    raise ValueError(f"{self.path}: identidade retornada pelo ioctl não corresponde ao painel SPI.")
                self.layout = Layout.from_info(self.fixed, self.var)
                self.buffer = mmap.mmap(self.fd, self.layout.size, access=mmap.ACCESS_WRITE)
        except BaseException:
            os.close(self.fd)
            raise
        return self

    def describe(self):
        fix, var = self.fixed, self.var
        fields = [getattr(var, field).values() for field in ("red", "green", "blue", "transp")]
        try:
            Layout.from_info(fix, var)
            pixel_format = "RGB565"
        except ValueError as error:
            pixel_format = str(error)
        return (f"{self.path}: {fix.id.decode(errors='replace')} {var.xres}×{var.yres}"
                f" virtual={var.xres_virtual}×{var.yres_virtual}"
                f" offset={var.xoffset},{var.yoffset} bpp={var.bits_per_pixel}"
                f" stride={fix.line_length} memória={fix.smem_len} tipo={fix.type} visual={fix.visual}"
                f" bitfields RGBA (offset, bits, msb_right)={fields} | {pixel_format}")

    def draw(self, image):
        if image.size != (self.layout.width, self.layout.height):
            raise ValueError("Dimensões da imagem não correspondem ao framebuffer.")
        if image.mode != "RGB":
            image = image.convert("RGB")
        draw_rgb(self.buffer, self.layout, image.tobytes())

    def __exit__(self, *_):
        try:
            if self.writable:
                self.buffer.close()
        finally:
            os.close(self.fd)
