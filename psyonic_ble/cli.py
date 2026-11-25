"""psyonic-ble: command line control of a PSYONIC Ability Hand over Bluetooth LE."""

import argparse
import asyncio
import contextlib
import os
import re
import shlex
import sys
import threading

from . import __version__
from . import protocol as p
from .client import Hand, find, scan

DEFAULT_PERIOD = 1.0
SUCCESS_RE = re.compile(r"success|error", re.I)


# ---------------------------------------------------------------- helpers

def out(*args, **kw):
    print(*args, **kw, flush=True)


def check_write(replies: list[str], what: str) -> bool:
    ok = any("success" in r.lower() for r in replies)
    if ok:
        out(f"{what}: saved")
    else:
        out(f"{what}: no 'success' reply ({'; '.join(replies) or 'no reply'})")
    return ok


async def first_sample(hand: Hand, stream: p.Stream, timeout: float = 3.0):
    async with contextlib.aclosing(hand.stream(stream.command, timeout)) as samples:
        async for _, data in samples:
            value = p.decode_stream(stream, data)
            if stream.kind == "text" and isinstance(value, str) and value:
                return value
            if isinstance(value, list):
                return value
    raise TimeoutError(f"no {stream.command} data within {timeout} s")


async def read_flags(hand: Hand, indices: list[int]) -> dict[int, int]:
    vals = {}
    for i in indices:
        replies = await hand.request(f"RZ{i}", wait=1.0, until=rf"BIN{i}\s*=")
        vals.update(p.parse_flag_reply(" ".join(replies)))
    return vals


async def read_word(hand: Hand, index: int, fmt: str) -> str | None:
    replies = await hand.request(p.word_read_cmd(index, fmt), wait=1.0, until=rf"mem\[0*{index}\]")
    return p.parse_word_reply(" ".join(replies)).get(index, "; ".join(replies) or None)


def flag_state(v: int | None) -> str:
    return "?" if v is None else "enabled" if v == 0 else "disabled"


# ---------------------------------------------------------------- commands (no connection)

async def cmd_scan(args):
    hands = await scan(args.timeout, args.name)
    if not hands:
        out("No Ability Hand found. Is it powered and not connected to the PSYONIC app?")
        return 1
    for dev, name, rssi in hands:
        out(f"{name:28s} {dev.address}  RSSI {rssi}")


def list_grips():
    out("idx  name            (open configuration = idx + 32, -1 = open current grip)")
    for name, idx in p.GRIPS.items():
        out(f"{idx:3d}  {name}")
    out("19-30: free user grip slots")


def list_streams():
    for name, s in p.STREAMS.items():
        out(f"{name:13s} {s.command:4s} {s.description}")


def list_flags():
    out("idx  name                         default   description")
    for f in p.FLAGS:
        out(f"{f.index:3d}  {f.name:28s} {flag_state(f.default_raw):9s} {f.description}")
    out("\nRaw values (RZ, We/Wd): 0 = enabled, 1 = disabled.")


def list_words():
    out("idx  name                      fmt    description")
    for w in p.WORDS:
        out(f"{w.index:03d}  {w.name:25s} {w.fmt:6s} {w.description}")


# ---------------------------------------------------------------- commands (connected)

async def cmd_grip(hand: Hand, args):
    if args.grip == "list":
        return list_grips()
    idx = p.resolve_grip(args.grip)
    if args.open and idx >= 0:
        idx += p.GRIP_OPEN_OFFSET
    replies = await hand.request(p.grip_cmd(idx, args.period), wait=args.wait)
    for r in replies:
        out(r)


async def cmd_open(hand: Hand, args):
    for r in await hand.request(p.grip_cmd(-1, args.period), wait=args.wait):
        out(r)


async def cmd_stream(hand: Hand, args):
    if args.kind == "list":
        return list_streams()
    stream = p.STREAMS[args.kind]
    if args.csv and stream.kind != "text":
        out("t," + ",".join(stream.labels))
    count = 0
    async with contextlib.aclosing(hand.stream(stream.command, args.duration)) as samples:
        async for t, data in samples:
            value = p.decode_stream(stream, data)
            if value is None:
                continue
            if isinstance(value, str):
                out(value if not args.csv else f"{t:.3f},{value}")
            elif args.csv:
                out(f"{t:.3f}," + ",".join(f"{v:.4g}" for v in value))
            elif stream.kind == "fsr":
                out(f"{t:8.3f}  " + " ".join(f"{v:4.0f}" for v in value))
            else:
                out(f"{t:8.3f}  " + "  ".join(f"{l}={v:8.3f}" for l, v in zip(stream.labels, value)))
            count += 1
            if args.count and count >= args.count:
                break


async def cmd_battery(hand: Hand, args):
    volts = (await first_sample(hand, p.STREAMS["battery"]))[0]
    out(f"{volts:.2f} V")


async def cmd_temp(hand: Hand, args):
    out(await first_sample(hand, p.STREAMS["temperature"]))


async def cmd_info(hand: Hand, args):
    out(f"device:          {hand.name} ({hand.device.address}), MTU {hand.client.mtu_size}")
    items = [("name", "RC"), ("firmware hash", "Rc"), ("driver hashes", "Rj"), ("power switch", "Rk"),
             ("control mode", "R1"), ("hardware profile", "Rn003"), ("motor check", "RT")]
    if args.ids:
        items.append(("unique ids", "R#"))
    for label, cmd in items:
        replies = await hand.request(cmd, wait=args.wait)
        text = " | ".join(replies) if replies else "(no reply)"
        if cmd == "R1" and (m := re.search(r"\d+", text)):
            text += f"  ({p.CONTROL_MODES.get(int(m.group()), 'unknown')})"
        out(f"{label + ':':16s} {text}")


async def cmd_mode(hand: Hand, args):
    if args.mode:
        n = p.WRITABLE_CONTROL_MODES[args.mode]
        check_write(await hand.request(f"W0{n}", wait=args.wait, until=SUCCESS_RE), f"control mode {args.mode}")
    replies = await hand.request("R1", wait=args.wait)
    text = " | ".join(replies)
    m = re.search(r"\d+", text)
    out(f"control mode: {p.CONTROL_MODES.get(int(m.group()), 'unknown') if m else '?'}  (raw: {text or 'no reply'})")


async def cmd_flag(hand: Hand, args):
    if args.action == "list":
        return list_flags()
    indices = [p.resolve_flag(f) for f in args.flags] or ([f.index for f in p.FLAGS] if args.action == "get" else [])
    if args.action in ("enable", "disable"):
        if not indices:
            raise ValueError(f"which flag(s) to {args.action}?")
        for i in indices:
            if (i in p.RESERVED_FLAGS or (i in p.DANGEROUS_FLAGS and args.action == "enable")) and not args.force:
                raise ValueError(f"flag {i} is reserved or dangerous (see ICD 4.6.3); add --force if you are sure")
        for i in indices:
            cmd = f"{'We' if args.action == 'enable' else 'Wd'}{i}"
            check_write(await hand.request(cmd, wait=args.wait, until=SUCCESS_RE), f"{args.action} flag {i}")
    vals = await read_flags(hand, indices)
    for i in indices:
        f = p.FLAGS_BY_INDEX.get(i)
        v = vals.get(i)
        changed = "  *" if f and f.default_raw is not None and v is not None and v != f.default_raw else ""
        out(f"{i:3d}  {(f.name if f else '(undocumented)'):28s} {flag_state(v)}{changed}")
    if args.action == "get" and not args.flags:
        out("\n* = differs from factory default")
    if args.action != "get":
        out("Power-cycle the hand if the setting affects startup or communication.")


async def cmd_word(hand: Hand, args):
    if args.action == "list":
        return list_words()
    if args.action == "get":
        indices = [p.resolve_word(w) for w in args.words] or [w.index for w in p.WORDS]
        for i in indices:
            fmt = args.fmt or (p.WORDS_BY_INDEX[i].fmt if i in p.WORDS_BY_INDEX else "hex")
            w = p.WORDS_BY_INDEX.get(i)
            out(f"{i:03d}  {(w.name if w else '(undocumented)'):25s} {await read_word(hand, i, fmt)}")
        return
    if len(args.words) != 2:
        raise ValueError("usage: word set <name|index> <value>")
    i = p.resolve_word(args.words[0])
    w = p.WORDS_BY_INDEX.get(i)
    if (w is None or i in p.READONLY_WORDS) and not args.force:
        raise ValueError(f"word {i} is undocumented or read-only/calibration; add --force if you are sure")
    fmt = args.fmt or (w.fmt if w else "hex")
    cmd = p.word_write_cmd(i, fmt, args.words[1])
    check_write(await hand.request(cmd, wait=args.wait, until=SUCCESS_RE), f"word {i:03d}")
    out(f"{i:03d} = {await read_word(hand, i, fmt)}")


COMM_FLAGS = [16, 35, 46, 47]
COMM_SETUP = {"uart": ["We16", "Wd35", "We46", "We47"], "rs485": ["We16", "We35", "We46", "We47"], "i2c": ["Wd16"]}


async def cmd_comm(hand: Hand, args):
    if args.mode or args.baud:
        cmds = list(COMM_SETUP.get(args.mode, []))
        if args.baud:
            if args.baud not in p.BAUD_ENUM:
                raise ValueError(f"unsupported baud; choose from {', '.join(map(str, p.BAUD_ENUM))}")
            if args.baud > 460800 and args.mode != "rs485":
                out("warning: baud rates above 460800 only work over RS485")
            cmds.append(f"Wp001:2:0x{p.BAUD_ENUM[args.baud]:02X}")
        for c in cmds:
            check_write(await hand.request(c, wait=args.wait, until=SUCCESS_RE), c)
    vals = await read_flags(hand, COMM_FLAGS)
    for i in COMM_FLAGS:
        out(f"{i:3d}  {p.FLAGS_BY_INDEX[i].name:15s} {flag_state(vals.get(i))}")
    mode = "?" if 16 not in vals else "I2C" if vals[16] else "RS485 (5 V)" if vals.get(35) == 0 else "UART (3.3 V)"
    cfg = await read_word(hand, 1, "hex")
    baud = "default (460800)"
    if cfg and (m := re.search(r"0x([0-9a-fA-F]+)", cfg)):
        enum = (int(m.group(1), 16) >> 16) & 0xFF
        baud = next((str(b) for b, e in p.BAUD_ENUM.items() if e == enum), "default (460800)" if enum == 0 else f"enum {enum}")
    out(f"comm lines: {mode}, baud {baud}  (api-config0 = {cfg})")
    if args.mode or args.baud:
        out("Power-cycle the hand so the new communication settings take effect.")


async def cmd_led(hand: Hand, args):
    for r in await hand.request(p.hexcolor_cmd(args.color), wait=args.wait):
        out(r)


async def cmd_vibrate(hand: Hand, args):
    cmd = "CB" if args.intensity == 0 else f"CA{args.intensity}"
    for r in await hand.request(cmd, wait=args.wait):
        out(r)


async def cmd_name(hand: Hand, args):
    if args.new_name:
        check_write(await hand.request(f"WJ{args.new_name}", wait=args.wait, until=SUCCESS_RE), "name")
        out("The advertised name changes after a power cycle.")
    out(" | ".join(await hand.request("RC", wait=args.wait)) or "(no reply)")


async def cmd_raw(hand: Hand, args):
    for c in args.commands:
        data = bytes.fromhex(c) if args.hex else c.encode("utf-8").decode("unicode_escape").encode("latin-1")
        replies = await hand.request(data, wait=args.wait)
        out(f"-> {p.printable(data)}")
        for r in replies:
            out(f"<- {r}")


async def cmd_shell(hand: Hand, args):
    """Interactive prompt over one connection: subcommands, or anything else is sent raw."""
    with contextlib.suppress(ImportError):
        import readline  # noqa: F401  (line editing + history for input())
    parser = build_parser()
    commands = set(COMMANDS) - {"shell", "scan"}
    loop = asyncio.get_running_loop()
    lines: asyncio.Queue[str | None] = asyncio.Queue()
    prompt_ready = threading.Event()
    prompt = f"{hand.name}> "

    def reader():   # input() blocks, so it lives in a daemon thread
        while True:
            prompt_ready.wait()
            prompt_ready.clear()
            try:
                line = input(prompt)
            except EOFError:
                line = None
            loop.call_soon_threadsafe(lines.put_nowait, line)
            if line is None:
                return

    threading.Thread(target=reader, daemon=True).start()
    out(f"connected to {hand.name}. Subcommands (info, stream, flag, grip, ...) or raw ICD commands "
        f"(g03:0.5, RZ16, ...). 'help', Ctrl-C stops a command, Ctrl-D/exit quits.")
    while True:
        for data in hand.drain():
            out(f"<- {p.printable(data)}")
        prompt_ready.set()
        try:
            line = await lines.get()
        except asyncio.CancelledError:
            asyncio.current_task().uncancel()
            out()
            return
        if line is None or line.strip() in ("exit", "quit"):
            return
        line = line.strip()
        if not line:
            continue
        if line in ("help", "?"):
            parser.print_help()
            continue
        try:
            tokens = shlex.split(line)
            if tokens[0] in commands or tokens[0] in ("-h", "--help"):
                with contextlib.suppress(SystemExit):   # argparse exits on errors and --help
                    sub = parser.parse_args(tokens)
                    sub.wait = args.wait if sub.wait is None else sub.wait
                    validate(sub)
                    await COMMANDS[sub.command](hand, sub)
            else:
                for r in await hand.request(line, wait=args.wait):
                    out(f"<- {r}")
        except asyncio.CancelledError:
            asyncio.current_task().uncancel()
            out("\ninterrupted")
        except (ValueError, TimeoutError) as e:
            out(f"error: {e}")
        if not hand.client.is_connected:
            out("disconnected")
            return


COMMANDS = {
    "scan": cmd_scan, "grip": cmd_grip, "open": cmd_open, "stream": cmd_stream,
    "battery": cmd_battery, "temp": cmd_temp, "info": cmd_info, "mode": cmd_mode, "flag": cmd_flag,
    "word": cmd_word, "comm": cmd_comm, "led": cmd_led, "vibrate": cmd_vibrate, "name": cmd_name,
    "raw": cmd_raw, "shell": cmd_shell,
}
OFFLINE = {"scan"}


# ---------------------------------------------------------------- argument parsing

def add_global_options(ap: argparse.ArgumentParser, defaults: bool):
    """Connection options, accepted before and after the subcommand."""
    d = (lambda v: v) if defaults else (lambda v: argparse.SUPPRESS)
    ap.add_argument("-a", "--address", default=d(os.environ.get("PSYONIC_BLE_ADDRESS")),
                    help="BLE address of the hand (default: first hand found)")
    ap.add_argument("-n", "--name", default=d(os.environ.get("PSYONIC_BLE_NAME")),
                    help="pick the hand whose advertised name contains this text")
    ap.add_argument("-t", "--timeout", type=float, default=d(8.0), help="scan timeout in s (default 8)")
    ap.add_argument("-v", "--verbose", action="store_true", default=d(False),
                    help="print all BLE traffic to stderr")


def validate(args):
    """Catch argument errors before spending seconds on scanning and connecting."""
    if args.command == "grip":
        p.resolve_grip(args.grip)
    elif args.command == "flag":
        [p.resolve_flag(f) for f in args.flags]
    elif args.command == "word":
        [p.resolve_word(w) for w in args.words[:1 if args.action == "set" else None]]
    elif args.command == "led":
        p.hexcolor_cmd(args.color)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="psyonic-ble",
        description="Set up, test and read out a PSYONIC Ability Hand over Bluetooth LE (ICD chapter 4).",
        epilog="Examples:\n"
               "  psyonic-ble scan\n"
               "  psyonic-ble grip power -p 0.5\n"
               "  psyonic-ble open\n"
               "  psyonic-ble stream position -d 5 --csv > pos.csv\n"
               "  psyonic-ble flag get vibration comm-uart\n"
               "  psyonic-ble comm uart --baud 230400\n"
               "  psyonic-ble shell\n"
               "  psyonic-ble raw g03:0.5 RZ16\n\n"
               "Address/name defaults come from $PSYONIC_BLE_ADDRESS / $PSYONIC_BLE_NAME.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    add_global_options(ap, defaults=True)
    common = argparse.ArgumentParser(add_help=False)
    add_global_options(common, defaults=False)
    sub = ap.add_subparsers(dest="command", required=True, metavar="command")

    def add(name, help_, **kw):
        sp = sub.add_parser(name, help=help_, description=help_, parents=[common], **kw)
        sp.add_argument("-w", "--wait", type=float, default=None,
                        help="seconds to wait for a reply (default 1.0)")
        return sp

    add("scan", "list hands in range")

    sp = add("grip", "test motion: move to a preset grip ('grip list' shows them)")
    sp.add_argument("grip", help="grip name (power, pinch, key, point, ...), index 0-63, or 'list'")
    sp.add_argument("-p", "--period", type=float, default=DEFAULT_PERIOD, help="motion period in s (default 1.0)")
    sp.add_argument("--open", action="store_true", help="go to this grip's open configuration (idx + 32)")

    sp = add("open", "test motion: open the hand (open configuration of the current grip)")
    sp.add_argument("-p", "--period", type=float, default=DEFAULT_PERIOD, help="motion period in s (default 1.0)")

    sp = add("stream", "stream sensor data ('stream list' shows the kinds)")
    sp.add_argument("kind", choices=[*p.STREAMS, "list"], metavar="kind",
                    help=", ".join([*p.STREAMS, "list"]))
    sp.add_argument("-d", "--duration", type=float, default=None, help="stop after N s (default: until Ctrl-C)")
    sp.add_argument("-c", "--count", type=int, default=None, help="stop after N samples")
    sp.add_argument("--csv", action="store_true", help="CSV output with a time column")

    add("battery", "read the supply voltage")
    add("temp", "read motor driver temperatures")

    sp = add("info", "name, firmware, control mode, hardware and motor check")
    sp.add_argument("--ids", action="store_true", help="also read the unique MCU ids")

    sp = add("mode", "read or set the control mode")
    sp.add_argument("mode", nargs="?", choices=list(p.WRITABLE_CONTROL_MODES), help="new control mode (saved)")

    sp = add("flag", "list, read, enable or disable binary settings")
    sp.add_argument("action", choices=["list", "get", "enable", "disable"])
    sp.add_argument("flags", nargs="*", help="flag names or indices (get: default all documented)")
    sp.add_argument("--force", action="store_true", help="allow reserved/dangerous flags")

    sp = add("word", "list, read or write 32-bit word settings")
    sp.add_argument("action", choices=["list", "get", "set"])
    sp.add_argument("words", nargs="*", help="get: names/indices; set: <name|index> <value>")
    sp.add_argument("--float", dest="fmt", action="store_const", const="float", help="force float format")
    sp.add_argument("--hex", dest="fmt", action="store_const", const="hex", help="force hex format")
    sp.add_argument("--force", action="store_true", help="allow undocumented/read-only words")

    sp = add("comm", "read or set the COMM line mode (I2C / UART / RS485) for the wired API")
    sp.add_argument("mode", nargs="?", choices=list(COMM_SETUP),
                    help="uart: 3.3 V UART + PPP stuffing; rs485: 5 V RS485 + PPP; i2c: back to I2C")
    sp.add_argument("--baud", type=int, help="set the UART/RS485 baud rate (e.g. 230400, 460800)")

    sp = add("led", "show an RGB colour on the hand/IO board LED")
    sp.add_argument("color", help="hex colour, e.g. 00FE45")

    sp = add("vibrate", "override the vibration motor for 10 s (0 clears)")
    sp.add_argument("intensity", type=int, help="0-87")

    sp = add("name", "read or write the hand's advertised name suffix")
    sp.add_argument("new_name", nargs="?")

    sp = add("raw", "send raw ICD commands and print the replies")
    sp.add_argument("commands", nargs="+", help=r"e.g. g03:0.5 RZ16 'G3\x41' (escapes allowed)")
    sp.add_argument("--hex", action="store_true", help="commands are hex byte strings")

    add("shell", "interactive prompt over a single connection")
    return ap


async def run(args) -> int | None:
    args.wait = 1.0 if args.wait is None else args.wait
    if args.command in OFFLINE:
        return await COMMANDS[args.command](args)
    if "list" in (getattr(args, "grip", None), getattr(args, "kind", None), getattr(args, "action", None)):
        return await COMMANDS[args.command](None, args)   # tables only, no connection needed
    validate(args)
    device = await find(args.address, args.name, args.timeout)
    async with Hand(device, verbose=args.verbose) as hand:
        if args.verbose:
            print(f"connected to {hand.name} ({device.address}), MTU {hand.client.mtu_size}", file=sys.stderr)
        return await COMMANDS[args.command](hand, args)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args)) or 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, TimeoutError, ConnectionError) as e:
        print(f"psyonic-ble: {e}", file=sys.stderr)
        return 1
    except Exception as e:   # bleak/BlueZ errors: keep it to one line
        print(f"psyonic-ble: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
