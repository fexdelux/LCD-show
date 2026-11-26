"""Read resistive touch events and persist a five-point affine calibration."""
import errno
import itertools
import json
import math
import os
from pathlib import Path
import re
import tempfile


# Positions in normalized display coordinates, ordered clockwise, then center.
TARGETS = ((0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9), (0.5, 0.5))


def normalize(raw, limits):
    low, high = limits
    if high <= low:
        raise ValueError("Faixa ABS inválida.")
    return (raw - low) / (high - low)


def orient(x, y, rotation=0, swap=False, invert_x=False, invert_y=False):
    if swap:
        x, y = y, x
    if invert_x:
        x = 1 - x
    if invert_y:
        y = 1 - y
    return {0: (x, y), 90: (1 - y, x), 180: (1 - x, 1 - y), 270: (y, 1 - x)}[rotation]


def apply_affine(matrix, point):
    x, y = point
    return tuple(a * x + b * y + c for a, b, c in matrix)


def fit_calibration(samples):
    if len(samples) != len(TARGETS):
        raise ValueError("São necessárias cinco amostras, incluindo o centro.")
    # Select the largest triangle of measured corners to avoid a poorly conditioned fit.
    def determinant(indices):
        p, q, r = [samples[index] for index in indices]
        return (q[0] - p[0]) * (r[1] - p[1]) - (r[0] - p[0]) * (q[1] - p[1])

    indices = max(itertools.combinations(range(4), 3), key=lambda group: abs(determinant(group)))
    det = determinant(indices)
    if not math.isfinite(det) or abs(det) < 0.04:
        raise ValueError("Amostras insuficientemente separadas.")
    p, q, r = [samples[index] for index in indices]
    matrix = []
    for u, v, w in zip(*(TARGETS[index] for index in indices)):
        a = ((v - u) * (r[1] - p[1]) - (w - u) * (q[1] - p[1])) / det
        b = ((q[0] - p[0]) * (w - u) - (r[0] - p[0]) * (v - u)) / det
        matrix.append((a, b, u - a * p[0] - b * p[1]))
    for sample, target in zip(samples, TARGETS):
        error = math.dist(apply_affine(matrix, sample), target)
        if not math.isfinite(error) or error > 0.08:
            raise ValueError("Quarto canto ou centro fora da tolerância. Repita a calibração.")
    return matrix


def save_calibration(path, identity, matrix):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump({"version": 1, "device": identity, "matrix": matrix}, stream)
            stream.write("\n")
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def load_calibration(path, identity):
    with open(path) as stream:
        data = json.load(stream)
    if data.get("version") != 1 or data.get("device") != identity:
        raise ValueError("Calibração de outro dispositivo ou outra orientação.")
    matrix = data["matrix"]
    if (len(matrix) != 2 or any(len(row) != 3 for row in matrix)
            or not all(math.isfinite(value) for row in matrix for value in row)):
        raise ValueError("Matriz de calibração inválida.")
    return matrix


TOUCH_NAME = re.compile(r"\b(?:ads7846|xpt2046|tft35a)\b", re.IGNORECASE)


def touch_paths():
    for entry in sorted(Path("/sys/class/input").glob("event[0-9]*")):
        try:
            name = (entry / "device/name").read_text(errors="replace")
        except FileNotFoundError:
            continue
        if TOUCH_NAME.search(name):
            yield f"/dev/input/{entry.name}"


def open_touch(explicit=None):
    import evdev
    from evdev import ecodes as e

    paths = [os.fspath(explicit)] if explicit is not None else touch_paths()
    selected = []
    unreadable = []
    try:
        for path in paths:
            try:
                device = evdev.InputDevice(path)
            except PermissionError:
                unreadable.append(path)
                continue
            except OSError as error:
                if explicit is None and error.errno in (errno.ENOENT, errno.ENODEV):
                    continue
                raise OSError(f"{path}: {error}. Confira a conexão do touchscreen.") from error
            selected.append(device)  # Also closed if capability inspection fails.
            cap = device.capabilities(absinfo=False)
            if not (device.info.bustype == e.BUS_SPI and TOUCH_NAME.search(device.name or "")
                    and {e.ABS_X, e.ABS_Y} <= set(cap.get(e.EV_ABS, []))
                    and e.BTN_TOUCH in cap.get(e.EV_KEY, [])):
                selected.pop().close()
        if unreadable:
            raise PermissionError("Sem acesso ao touch em " + ", ".join(unreadable)
                                  + ". Confira as permissões ou indique outro ADS7846 SPI com --touch.")
        if len(selected) == 1:
            return selected.pop()
        raise ValueError(f"Encontrados {len(selected)} touchscreens ADS7846 SPI com ABS_X, ABS_Y e BTN_TOUCH. "
                         "Consulte LCD35-lite-status ou use --touch /dev/input/eventN.")
    finally:
        for device in selected:
            device.close()


class Touch:
    def __init__(self, device):
        from evdev import ecodes

        self.e = ecodes
        self.device = device
        axes = [device.absinfo(axis) for axis in (ecodes.ABS_X, ecodes.ABS_Y)]
        if any(axis is None or axis.max <= axis.min for axis in axes):
            raise ValueError("Faixas ABS_X/ABS_Y inválidas no touchscreen.")
        self.limits = [(axis.min, axis.max) for axis in axes]
        self.identity = {"name": device.name, "phys": device.phys,
                         "limits": [list(axis) for axis in self.limits]}
        self.values = [axis.value for axis in axes]
        self.pressed = False
        self.tapped = False
        self.dropped = False

    def axis_values(self):
        return [self.device.absinfo(axis).value for axis in (self.e.ABS_X, self.e.ABS_Y)]

    def taps(self):
        e = self.e
        try:
            events = list(self.device.read())
        except BlockingIOError:
            return
        except OSError as error:
            raise OSError(f"Leitura do touchscreen falhou ({error}). Confira a conexão e reinicie a demo.") from error
        if not events:
            raise OSError("Touchscreen desconectado. Reconecte e reinicie a demo.")
        for event in events:
            if event.type == e.EV_SYN and event.code == e.SYN_DROPPED:
                self.dropped = True
                self.tapped = False
                self.pressed = False
            elif self.dropped:
                if event.type == e.EV_SYN and event.code == e.SYN_REPORT:
                    self.values = self.axis_values()
                    self.dropped = False
            elif event.type == e.EV_ABS and event.code in (e.ABS_X, e.ABS_Y):
                self.values[int(event.code == e.ABS_Y)] = event.value
            elif event.type == e.EV_KEY and event.code == e.BTN_TOUCH:
                if not event.value and self.pressed:
                    self.tapped = True
                self.pressed = bool(event.value)
            elif event.type == e.EV_SYN and event.code == e.SYN_REPORT and self.tapped:
                self.tapped = False
                yield tuple(normalize(value, limits) for value, limits in zip(self.values, self.limits))
