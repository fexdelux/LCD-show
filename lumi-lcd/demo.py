#!/usr/bin/env python3
"""Event-driven LUMI screen and guided touch calibration."""
import argparse
from contextlib import ExitStack
import os
from pathlib import Path
from selectors import DefaultSelector, EVENT_READ
import signal
import sys

from framebuffer import Framebuffer
from touch import (TARGETS, Touch, apply_affine, fit_calibration, load_calibration,
                   open_touch, orient, save_calibration)


STATES = {
    "aguardando": ("Aguardando", "#93b9d9"),
    "escutando": ("Escutando", "#61e0bd"),
    "processando": ("Processando", "#ffd27a"),
    "falando": ("Falando", "#f9a5cb"),
}


def draw(fb, state, target=None, cursor=None, hint=None):
    from PIL import Image, ImageDraw, ImageFont

    width, height = fb.layout.width, fb.layout.height
    image = Image.new("RGB", (width, height), "#101922")
    canvas = ImageDraw.Draw(image)
    label, color = STATES[state]
    fonts = Path("/usr/share/fonts/truetype/dejavu")

    def text(label, y, size, fill, bold=False):
        name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        try:
            font = ImageFont.truetype(os.fspath(fonts / name), size)
        except OSError:
            font = ImageFont.load_default()
            left, top, right, bottom = font.getbbox(label)
            mask = Image.new("L", (right - left, bottom - top))
            ImageDraw.Draw(mask).text((-left, -top), label, fill=255, font=font)
            scale = max(1, min(size // mask.height, width // mask.width))
            mask = mask.resize((mask.width * scale, mask.height * scale), Image.NEAREST)
            image.paste(fill, ((width - mask.width) // 2, round(y - mask.height / 2)), mask)
        else:
            canvas.text((width // 2, y), label, anchor="mm", fill=fill, font=font)

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
    state = args.state
    draw(fb, state, TARGETS[0] if args.calibrate else None,
         hint="Toque nos alvos" if args.calibrate else "Toque para mudar de estado")
    while True:
        selector.select()
        try:
            taps = list(touch.taps())
        except BlockingIOError:
            continue
        for point in taps:
            if args.calibrate:
                samples.append(point)
                if len(samples) == len(TARGETS):
                    matrix = fit_calibration(samples)
                    save_calibration(args.calibration, identity, matrix)
                    print(f"Calibração salva em {args.calibration}.")
                    return
                draw(fb, state, TARGETS[len(samples)], hint="Toque nos alvos")
            else:
                cursor = (apply_affine(matrix, point) if matrix else
                          orient(*point, args.rotate, args.swap_axes, args.invert_x, args.invert_y))
                names = tuple(STATES)
                state = names[(names.index(state) + 1) % len(names)]
                draw(fb, state, cursor=cursor, hint="Toque para mudar de estado")


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
