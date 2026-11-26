"""Manage the LCD35 boot files, using only Python's standard library."""
import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import stat
import subprocess
import sys
import tempfile


ASSET = Path(__file__).resolve().parent.parent / "usr/tft35a-overlay.dtb"
OVERLAY_SHA = "601ea7056da5d7864648798fd3656b4205f01d6b9a6a8a5cfad6ca5601bbbe1e"
CONFIG = "config.txt"
OVERLAY = "overlays/tft35a.dtbo"
STATE = ".lumi-lcd35.json"
JOURNAL = ".lumi-lcd35-pending.json"
BEGIN = "# BEGIN LUMI LCD35"
END = "# END LUMI LCD35"
ROTATIONS = ("0", "90", "180", "270")
PACKAGES = ("python3-pil", "python3-evdev", "fbset")


class BootError(Exception):
    pass


def safe_path(path, directory=False, missing=False):
    """Reject symlinks and unexpected types, including in parent components."""
    if ".." in Path(path).parts:
        raise BootError(f"Use um caminho sem componentes ..: {path}")
    path = Path(os.path.abspath(path))
    for parent in reversed(path.parents):
        if not stat.S_ISDIR(parent.lstat().st_mode):
            raise BootError(f"Diretório inválido ou link simbólico: {parent}")
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        if missing:
            return path
        raise
    check = stat.S_ISDIR if directory else stat.S_ISREG
    if not check(mode):
        raise BootError(f"Tipo inesperado ou link simbólico: {path}")
    return path


def boot_path(explicit):
    if explicit is not None:
        return safe_path(explicit, directory=True)
    modern = Path("/boot/firmware/config.txt")
    return safe_path(modern.parent if os.path.lexists(modern) else "/boot", directory=True)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def snapshot(path):
    path = safe_path(path, missing=True)
    if not path.exists():
        return None
    data = path.read_bytes()
    info = path.stat()
    return {"data": base64.b64encode(data).decode(), "sha256": sha(data),
            "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid}


def payload(record):
    data = base64.b64decode(record["data"], validate=True)
    if sha(data) != record["sha256"]:
        raise BootError("Hash inválido no backup ou diário.")
    if not all(type(record[key]) is int and record[key] >= 0 for key in ("mode", "uid", "gid")):
        raise BootError("Permissões inválidas no backup ou diário.")
    if record["mode"] > 0o7777:
        raise BootError("Modo inválido no backup ou diário.")
    return data


def record_for(data, previous=None, mode=0o644):
    metadata = previous or {"mode": mode, "uid": os.geteuid(), "gid": os.getegid()}
    return {**metadata, "data": base64.b64encode(data).decode(), "sha256": sha(data)}


def fat_boot(root):
    """Linux FAT exposes mount-wide owner and permission masks."""
    kind = ""
    depth = -1
    for line in read_text("/proc/self/mountinfo").splitlines():
        before, _, after = line.partition(" - ")
        mount = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), before.split()[4])
        location = Path(mount)
        if (root == location or location in root.parents) and len(location.parts) >= depth:
            kind = after.split()[0]
            depth = len(location.parts)
    return kind in ("vfat", "msdos", "exfat")


def equivalent(current, target, fat):
    if fat and current is not None and target is not None:
        return current["sha256"] == target["sha256"]
    return current == target


def replace(path, record, fat=False):
    directory = os.open(path.parent, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if record is None:
            path.unlink(missing_ok=True)
        else:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".lumi-", delete=False) as tmp:
                staging = Path(tmp.name)
                try:
                    tmp.write(payload(record))
                    info = os.fstat(tmp.fileno())
                    if fat:
                        if not record["mode"] & 0o200:
                            os.fchmod(tmp.fileno(), stat.S_IMODE(info.st_mode) & ~0o222)
                    else:
                        if (info.st_uid, info.st_gid) != (record["uid"], record["gid"]):
                            os.fchown(tmp.fileno(), record["uid"], record["gid"])
                        os.fchmod(tmp.fileno(), record["mode"])
                    tmp.flush()
                    os.fsync(tmp.fileno())
                    os.replace(staging, path)
                finally:
                    staging.unlink(missing_ok=True)
        os.fsync(directory)
    finally:
        os.close(directory)


def json_record(value):
    return record_for(json.dumps(value, indent=2).encode() + b"\n", mode=0o600)


def rollback(root, changes, fat=False):
    pending = []
    seen = set()
    for name, previous, proposed in reversed(changes):
        if name not in (CONFIG, OVERLAY, STATE) or name in seen:
            raise BootError("Caminho inesperado no diário.")
        seen.add(name)
        for record in (previous, proposed):
            if record is not None:
                payload(record)
        path = root / name
        current = snapshot(path)
        if equivalent(current, previous, fat):
            continue
        if not equivalent(current, proposed, fat):
            raise BootError(f"{path} mudou durante a transação. Preserve o diário e revise manualmente.")
        pending.append((path, previous))
    for path, previous in pending:
        replace(path, previous, fat)


def transact(root, changes, fat=False):
    changes = [(name, before, after) for name, before, after in changes if before != after]
    if not changes:
        return
    journal = root / JOURNAL
    attempted = []
    try:
        replace(journal, json_record(changes), fat)
        for name, before, after in changes:
            if snapshot(root / name) != before:
                raise BootError(f"{root / name} mudou durante a operação.")
            attempted.append((name, before, after))
            replace(root / name, after, fat)
    except BaseException:
        rollback(root, attempted, fat)
        replace(journal, None)
        raise
    replace(journal, None)


def managed_block(base, rotate, spi=True, reset_scope=False):
    separator = b"" if not base or base.endswith(b"\n") else b"\n"
    newline = "\r\n" if b"\r\n" in base and b"\n" not in base.replace(b"\r\n", b"") else "\n"
    lines = [BEGIN, "[all]"]
    overlay = f"dtoverlay=tft35a:rotate={rotate}"
    if spi and reset_scope:
        # A preceding overlay may export its own spi parameter. Close the scope
        # after loading ours, where an empty dtoverlay cannot suppress HAT loading.
        lines.extend((overlay, "dtoverlay=", "dtparam=spi=on"))
    else:
        if spi:
            lines.append("dtparam=spi=on")
        lines.append(overlay)
    lines.extend((END, ""))
    return separator + newline.join(lines).encode()


def strip_block(config, block):
    if block is None:
        if BEGIN.encode() in config or END.encode() in config:
            raise BootError("Marcadores LUMI sem manifesto. Revise config.txt manualmente.")
        return config
    if not config.endswith(block):
        raise BootError("Bloco LUMI alterado, movido ou com conteúdo posterior. Revise config.txt manualmente.")
    return strip_block(config[:-len(block)], None)


def check_config(config):
    """Accept only SPI settings whose effect is unambiguous across filters."""
    unconditional = True
    spi_enabled = False
    overlay_scope = False
    if b"\r" in config.replace(b"\r\n", b""):
        raise BootError("config.txt contém terminadores CR isolados.")
    for number, raw in enumerate(config.decode("utf-8-sig").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if len(raw.encode()) > 98 or any(ord(char) < 32 and char != "\t" for char in raw):
            raise BootError(f"config.txt:{number}: linha inválida ou longa demais para o firmware.")
        if line.startswith("["):
            if not re.fullmatch(r"\[[^\[\]]+\]", line):
                raise BootError(f"config.txt:{number}: filtro inválido.")
            unconditional = line == "[all]"
            continue
        key, _, value = map(str.strip, line.partition("="))
        name = re.split(r"[:,]", value, 1)[0].strip()
        conflict = (
            re.match(r"include(?:\s|=|$)", line)
            or key in ("overlay_prefix", "os_prefix", "device_tree", "gpio")
            or key == "dtoverlay" and (
                (not name and value) or re.match(r"(?:spi0|tft|lcd|mhs|mis35|piscreen|ads7846|waveshare|pitft|"
                                     r"panel-mipi-dbi|mcp251|sc16is|enc28j60|w5500)", name, re.I)
                or re.search(r"\bspi0(?:[-_.0-9]|\b)", value, re.I))
        )
        if conflict:
            raise BootError(f"config.txt:{number}: possível conflito ({line}); revise antes de instalar.")
        if key == "dtoverlay":
            if name or unconditional:
                overlay_scope = bool(name)
        if key in ("dtparam", "device_tree_param"):
            for param in value.split(","):
                setting, _, enabled = map(str.strip, param.partition("="))
                if setting in ("spi", "spi0"):
                    if (setting, enabled) != ("spi", "on") or not unconditional or spi_enabled or overlay_scope:
                        raise BootError(f"config.txt:{number}: SPI duplicado, condicional ou conflitante. Revise.")
                    spi_enabled = True
    return spi_enabled, overlay_scope


def read_manifest(saved, root, fat):
    if saved is None:
        return None
    manifest = json.loads(payload(saved))
    if manifest["version"] != 1 or manifest["paths"] != [CONFIG, OVERLAY]:
        raise BootError("Manifesto incompatível.")
    for entry in ("original_config", "overlay"):
        payload(manifest[entry])
    if manifest["overlay"]["sha256"] != OVERLAY_SHA:
        raise BootError("Overlay do manifesto não corresponde ao ativo validado.")
    if type(manifest["created_overlay"]) is not bool:
        raise BootError("Manifesto inválido.")
    if not equivalent(snapshot(root / OVERLAY), manifest["overlay"], fat):
        raise BootError("Overlay ausente ou alterado desde a instalação. Revise antes de continuar.")
    block = base64.b64decode(manifest["block"], validate=True)
    if sha(block) != manifest["block_sha256"] or block not in {
        managed_block(base, angle, spi, scope) for base in (b"", b"x", b"x\r\ny", b"x\r\n")
        for angle in ROTATIONS for spi in (True, False) for scope in (True, False)
    }:
        raise BootError("Bloco do manifesto inválido.")
    manifest["block"] = block
    return manifest


def plan(root, action, rotate="90", fat=False):
    saved = snapshot(root / STATE)
    manifest = read_manifest(saved, root, fat)
    current = snapshot(root / CONFIG)
    if current is None:
        raise BootError(f"Arquivo obrigatório ausente: {root / CONFIG}")
    base = strip_block(payload(current), manifest["block"] if manifest else None)
    if action == "restore":
        if manifest is None:
            return []
        if manifest["created_overlay"]:
            check_config(base)
        changes = [(CONFIG, current, record_for(base, current))]
        if manifest["created_overlay"]:
            changes.append((OVERLAY, snapshot(root / OVERLAY), None))
        return changes + [(STATE, saved, None)]

    spi_enabled, overlay_scope = check_config(base)
    overlay = snapshot(root / OVERLAY)
    if overlay is None:
        asset = ASSET.read_bytes()
        if sha(asset) != OVERLAY_SHA:
            raise BootError("O ativo tft35a do repositório não corresponde ao binário auditado.")
        target = record_for(asset)
    elif overlay["sha256"] == OVERLAY_SHA:
        target = overlay
    else:
        raise BootError("tft35a.dtbo existente difere do binário auditado. Faça backup e revise-o manualmente.")

    block = managed_block(base, rotate, spi=not spi_enabled, reset_scope=overlay_scope)
    updated = record_for(base + block, current)
    strip_block(payload(updated), block)
    state = {
        "version": 1, "paths": [CONFIG, OVERLAY],
        "original_config": manifest["original_config"] if manifest else current,
        "overlay": manifest["overlay"] if manifest else target,
        "created_overlay": manifest["created_overlay"] if manifest else overlay is None,
        "block": base64.b64encode(block).decode(), "block_sha256": sha(block),
    }
    # Keep the prior manifest bytes and metadata when reinstalling identically.
    next_state = json_record(state)
    if saved and payload(saved) == payload(next_state):
        next_state = saved
    return [(OVERLAY, overlay, target), (CONFIG, current, updated), (STATE, saved, next_state)]


def read_text(path):
    try:
        return Path(path).read_text(errors="replace").strip()
    except OSError:
        return ""


def host():
    model = read_text("/proc/device-tree/model").rstrip("\0")
    os_release = read_text("/etc/os-release")
    print(f"Modelo: {model or 'indisponível'} | arquitetura: {platform.machine()} | kernel: {platform.release()}")
    for line in os_release.splitlines():
        if line.startswith("PRETTY_NAME="):
            print(line)
    return model, os_release


def drivers():
    modules = Path("/lib/modules") / platform.release()
    builtin = read_text(modules / "modules.builtin")
    available = {}
    for module, spi in (("fb_ili9486", "spi0.0"), ("ads7846", "spi0.1"), ("spi_bcm2835", None)):
        bound = Path(f"/sys/bus/spi/devices/{spi}/driver") if spi else None
        try:
            info = subprocess.run(["modinfo", "-n", module], capture_output=True, text=True, check=False)
            found = info.returncode == 0
        except FileNotFoundError:
            found = False
        available[module] = (
            found or Path("/sys/module", module).exists()
            or any(Path(line).stem.replace("-", "_") == module for line in builtin.splitlines())
            or bool(bound and bound.is_symlink() and bound.resolve().name == module)
            or any(modules.glob(f"kernel/**/{module.replace('_', '[-_]')}.ko*"))
        )
        print(f"{module}: {'encontrado' if available[module] else 'sem evidência neste kernel'}")
    if not all(available.values()):
        print("Verifique os módulos do kernel em execução; para outro driver/overlay consulte lumi-lcd/README.md.")
    return all(available.values())


@contextlib.contextmanager
def boot_lock(root):
    # Opening the directory itself leaves no lockfile in the boot partition.
    fd = os.open(root, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def status(root, explicit, fb):
    host()
    if root is not None:
        for name in (CONFIG, OVERLAY, STATE, JOURNAL):
            try:
                item = snapshot(root / name)
            except (OSError, BootError) as error:
                print(f"{root / name}: {error}")
                continue
            print(f"{root / name}: " + (item["sha256"] if item else "ausente"))
            if item and name == CONFIG:
                print(payload(item).decode(errors="replace"))
    if explicit:
        print("Modo imagem: os dispositivos do host não representam o sistema da imagem.")
        return
    drivers()
    for spi in sorted(Path("/sys/bus/spi/devices").glob("spi*")):
        driver = spi / "driver"
        print(f"{spi.name}: {read_text(spi / 'modalias')} "
              f"driver={driver.resolve().name if driver.is_symlink() else '(sem vínculo)'}")
    from framebuffer import Framebuffer, candidates

    print("/proc/fb:", read_text("/proc/fb") or "(vazio ou indisponível)")
    print("Dispositivos:", ", ".join(map(str, sorted(Path("/dev").glob("fb[0-9]*")))) or "(nenhum)")
    for entry in sorted(Path("/sys/class/graphics").glob("fb[0-9]*")):
        print(entry, read_text(entry / "name"), read_text(entry / "virtual_size"),
              f"bpp={read_text(entry / 'bits_per_pixel')} stride={read_text(entry / 'stride')}",
              entry.resolve())
    options = candidates()
    print("Candidatos SPI:", ", ".join(map(str, options)) or "(nenhum)")
    if fb or len(options) == 1:
        try:
            with Framebuffer(fb or options[0], writable=False) as dev:
                print(dev.describe())
        except (OSError, ValueError) as error:
            print(f"ioctl: {error}")
    elif options:
        print("Seleção ambígua. Informe --fb /dev/fbN.")
    for event in sorted(Path("/sys/class/input").glob("event*")):
        print(f"/dev/input/{event.name}: {read_text(event / 'device/name')} "
              f"ABS={read_text(event / 'device/capabilities/abs')} "
              f"KEY={read_text(event / 'device/capabilities/key')}")
    modules = read_text("/proc/modules").splitlines()
    relevant = re.compile(r"ili9486|ads7846|fbtft|spi[_-]bcm|tft35a|spi0|pinctrl", re.I)
    print("Módulos carregados:", ", ".join(line.split()[0] for line in modules if relevant.search(line)) or "(nenhum)")
    try:
        logs = subprocess.run(["dmesg"], capture_output=True, text=True, check=False)
        print("dmesg:", "\n".join(line for line in logs.stdout.splitlines() if relevant.search(line))[-4000:]
              or logs.stderr.strip() or "(sem mensagens relevantes)")
    except OSError as error:
        print(f"dmesg indisponível: {error}")


def stop(signum, _frame):
    raise InterruptedError(f"Sinal {signum} recebido.")


def main():
    action = sys.argv[1]
    parser = argparse.ArgumentParser(description=f"LUMI LCD35 — {action}")
    parser.add_argument("--boot-dir", type=Path, help="partição de boot explícita (modo imagem)")
    if action == "status":
        parser.add_argument("--fb", help="framebuffer explícito /dev/fbN")
    else:
        parser.add_argument("--dry-run", action="store_true", help="mostrar plano sem gravar")
    if action == "install":
        parser.add_argument("--rotate", choices=ROTATIONS, default="90")
        parser.add_argument("--install-demo", action="store_true", help="instalar dependências opcionais via apt")
    args = parser.parse_args(sys.argv[2:])
    explicit = args.boot_dir is not None
    if action == "status":
        if explicit and args.fb:
            parser.error("--fb consulta o host e não é aceito com --boot-dir")
        try:
            root = boot_path(args.boot_dir)
        except (OSError, BootError) as error:
            print(f"Boot: {error}")
            root = None
        status(root, explicit, args.fb)
        return

    if not args.dry_run and os.geteuid() != 0:
        raise BootError("Alterações requerem root; consulte o plano com --dry-run.")
    demo = getattr(args, "install_demo", False)
    if explicit and demo:
        raise BootError("--install-demo requer execução no Pi e é incompatível com --boot-dir.")
    if explicit:
        print("Modo imagem: somente a partição de boot explícita será considerada.")
    elif action == "install":
        model, os_release = host()
        if not model.startswith("Raspberry Pi 3 ") or platform.machine() not in ("aarch64", "arm64"):
            raise BootError("Ambiente alvo: Raspberry Pi 3 com Raspberry Pi OS Lite ARM64.")
        if not re.search(r"^ID=(?:debian|raspbian)$", os_release, re.M) or sys.maxsize <= 2**32:
            raise BootError("Sistema ou userland não reconhecido para o alvo ARM64.")
        if not drivers():
            raise BootError("Drivers necessários não confirmados no kernel alvo.")
    root = boot_path(args.boot_dir)
    safe_path(root / "overlays", directory=True)
    fat = fat_boot(root)
    with contextlib.nullcontext() if args.dry_run else boot_lock(root):
        pending = snapshot(root / JOURNAL)
        if pending is not None:
            if action != "restore" or args.dry_run:
                raise BootError(f"Transação pendente em {root / JOURNAL}. Revise e execute restore.")
            rollback(root, json.loads(payload(pending)), fat)
            replace(root / JOURNAL, None)
        changes = plan(root, action, getattr(args, "rotate", "90"), fat)
        for name, before, after in changes:
            if before != after:
                verb = "remover" if after is None else "gravar"
                print(f"Plano: {verb} {root / name}", flush=True)
                if name == CONFIG and action == "install":
                    marker = payload(after).rindex(BEGIN.encode())
                    print(payload(after)[marker:].decode(), end="", flush=True)
        if not any(before != after for _, before, after in changes):
            print("Arquivos de boot já estão no estado solicitado.")
        if demo:
            print("Plano: apt-get install --no-install-recommends " + " ".join(PACKAGES), flush=True)
        if args.dry_run:
            return
        if demo:
            subprocess.run(["apt-get", "install", "--no-install-recommends", *PACKAGES], check=True)
        transact(root, changes, fat)
    print("Concluído. Reinicie manualmente quando estiver pronto.")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop)
    try:
        main()
    except (BootError, OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print(f"LUMI LCD35: {exc}", file=sys.stderr)
        sys.exit(1)
