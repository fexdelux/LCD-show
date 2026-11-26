#!/usr/bin/env python3
"""Event-driven LUMI screen and guided touch calibration."""
import argparse
from contextlib import ExitStack
from functools import lru_cache
import os
from pathlib import Path
from selectors import DefaultSelector, EVENT_READ
import signal
import sys
from time import monotonic

from framebuffer import Framebuffer
from touch import (TARGETS, Touch, apply_affine, fit_calibration, load_calibration,
                   open_touch, orient, save_calibration)


STATES = {
    "aguardando": ("Aguardando", "#93b9d9"),
    "escutando": ("Escutando", "#61e0bd"),
    "processando": ("Processando", "#ffd27a"),
    "falando": ("Falando", "#f9a5cb"),
}


FRAME_INTERVAL = 1 / 8


@lru_cache(maxsize=6)
def font_for(size, bold):
    from PIL import ImageFont

    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(f"/usr/share/fonts/truetype/dejavu/{name}", size), False
    except OSError:
        return ImageFont.load_default(), True


def draw(fb, state, target=None, cursor=None, hint=None):
    from PIL import Image, ImageDraw

    width, height = fb.layout.width, fb.layout.height
    image = Image.new("RGB", (width, height), "#101922")
    canvas = ImageDraw.Draw(image)
    label, color = STATES[state]
    def text(label, y, size, fill, bold=False):
        font, fallback = font_for(size, bold)
        if not fallback:
            canvas.text((width // 2, y), label, anchor="mm", fill=fill, font=font)
            return
        left, top, right, bottom = font.getbbox(label)
        mask = Image.new("L", (right - left, bottom - top))
        ImageDraw.Draw(mask).text((-left, -top), label, fill=255, font=font)
        scale = max(1, min(size // mask.height, width // mask.width))
        mask = mask.resize((mask.width * scale, mask.height * scale), getattr(Image, "Resampling", Image).NEAREST)
        image.paste(fill, ((width - mask.width) // 2, round(y - mask.height / 2)), mask)

    text("LUMI", height * 0.36, max(12, width // 9), "#f5f8fa", bold=True)
    text(label, height * 0.59, max(10, width // 16), color)
    if hint:
        text(hint, height * 0.78, max(10, width // 32), "#b9c8d1")
    for point, ink in ((target, "#ffd27a"), (cursor, "#61e0bd")):
        if point is not None:
            x, y = (round(min(1, max(0, value)) * (limit - 1))
                    for value, limit in zip(point, (width, height)))
            canvas.ellipse((x - 8, y - 8, x + 8, y + 8), outline=ink, width=2)
            canvas.line((x - 12, y, x + 12, y), fill=ink)
            canvas.line((x, y - 12, x, y + 12), fill=ink)
    fb.draw(image)


def run(args, resources):
    if os.geteuid() == 0:
        raise ValueError("Execute a demo com usuário sem privilégios.")
    fb = resources.enter_context(Framebuffer(args.fb))
    if args.without_touch:
        draw(fb, args.state)
        while True:
            signal.pause()
    device = open_touch(args.touch)
    resources.callback(device.close)
    touch = Touch(device)
    identity = {**touch.identity, "rotation": args.rotate,
                "resolution": [fb.layout.width, fb.layout.height]}
    matrix = None
    if not args.calibrate and args.calibration and args.calibration.exists():
        matrix = load_calibration(args.calibration, identity)

    selector = resources.enter_context(DefaultSelector())
    selector.register(device.fd, EVENT_READ)
    samples = []
    names = tuple(STATES)
    state = names.index(args.state)
    frame = (args.state, TARGETS[0] if args.calibrate else None, None)
    shown = None
    next_frame = 0
    hint = "Toque nos alvos" if args.calibrate else "Toque para mudar de estado"
    while True:
        now = monotonic()
        if frame != shown and now >= next_frame:
            draw(fb, *frame, hint=hint)
            shown = frame
            next_frame = now + FRAME_INTERVAL
        timeout = None if frame == shown else max(0, next_frame - monotonic())
        if not selector.select(timeout):
            continue
        taps = list(touch.taps())
        if not taps:
            continue
        if args.calibrate:
            # Accept one sample per visible target, discarding queued repeats.
            if frame != shown:
                continue
            samples.append(taps[0])
            if len(samples) == len(TARGETS):
                matrix = fit_calibration(samples)
                save_calibration(args.calibration, identity, matrix)
                print(f"Calibração salva em {args.calibration}.")
                return
            frame = (names[state], TARGETS[len(samples)], None)
        else:
            point = taps[-1]
            cursor = (apply_affine(matrix, point) if matrix else
                      orient(*point, args.rotate, args.swap_axes, args.invert_x, args.invert_y))
            state = (state + len(taps)) % len(names)
            frame = (names[state], None, cursor)


def stop(*_):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fb", help="/dev/fbN")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--touch", help="/dev/input/eventN ou link estável")
    group.add_argument("--without-touch", action="store_true")
    parser.add_argument("--state", choices=STATES, default="aguardando")
    parser.add_argument("--rotate", type=int, choices=(0, 90, 180, 270), default=90,
                        help="orientação do overlay, usada também no mapeamento sem calibração")
    parser.add_argument("--swap-axes", action="store_true")
    parser.add_argument("--invert-x", action="store_true")
    parser.add_argument("--invert-y", action="store_true")
    parser.add_argument("--calibration", type=Path, help="arquivo JSON de calibração")
    parser.add_argument("--calibrate", action="store_true", help="coletar quatro cantos e centro")
    args = parser.parse_args()
    if args.calibrate and (args.without_touch or not args.calibration):
        parser.error("--calibrate requer touchscreen e --calibration")
    signal.signal(signal.SIGTERM, stop)
    try:
        with ExitStack() as resources:
            run(args, resources)
    except KeyboardInterrupt:
        return 0
    except (ImportError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"LUMI demo: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
