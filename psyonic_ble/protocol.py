"""
Ability Hand BLE protocol: UUIDs, command encoders, setting tables and stream decoders.

Everything here is pure (no I/O) and follows ABILITY-HAND-ICD.pdf chapter 4.
The BLE individual finger command ('M', ICD 4.10) is deliberately not implemented: on firmware
f44b09f it moved the fingers to positions unrelated to the target.
Finger order everywhere: index, middle, ring, pinky, thumb flexor, thumb rotator.
"""

import re
import struct
from dataclasses import dataclass

# ICD 4.2: Nordic UART service
NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"   # write: PC -> hand
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"   # notify: hand -> PC

FINGERS = ["index", "middle", "ring", "pinky", "thumb_flexor", "thumb_rotator"]

# ICD 4.3.3: grip configuration list, used by the 'g' command. Open configs are idx + 32.
GRIPS = {
    "chuck": 0, "chuck-ok": 1, "pinch": 2, "power": 3, "key": 4, "handshake": 5, "trigger": 6,
    "point": 7, "relax": 8, "horns": 9, "rude-point": 10, "mode-switch": 11, "cylinder": 12,
    "mouse": 13, "hook": 14, "sleeve": 15, "peace": 16, "hangloose": 17, "pinch-fixed": 18,
    "waggle": 31,
}
GRIP_OPEN_OFFSET = 32

# ICD 4.4 'W0-' / 4.5 'R1'
CONTROL_MODES = {
    0: "bluetooth", 1: "direct", 2: "linear-transducer", 3: "i2c-top-rings", 4: "single-site",
    5: "direct-flipped", 6: "linear-transducer-flipped", 7: "single-site-flipped", 8: "i2c-mid-rings",
}
WRITABLE_CONTROL_MODES = {"bluetooth": 0, "direct": 1, "linear-transducer": 2, "i2c-top-rings": 3,
                          "single-site": 4, "i2c-mid-rings": 8}

# ICD 3.1.2.1: enumerated UART/RS485 baud rates (>460800 is RS485 only)
BAUD_ENUM = {1200: 1, 2400: 2, 4800: 3, 9600: 4, 14400: 5, 19200: 6, 28800: 7, 31250: 8,
             38400: 9, 56000: 10, 57600: 11, 76800: 12, 115200: 13, 230400: 14, 250000: 15,
             460800: 16, 921600: 17, 1000000: 18}


@dataclass(frozen=True)
class Flag:
    index: int
    name: str
    default: int | None   # ICD 'Default' column: logical, 1 = on, 0 = off, None = not stated
    description: str

    @property
    def default_raw(self) -> int | None:
        """Default as RZ reads it back (inverted: 0 = enabled)."""
        return None if self.default is None else 1 - self.default


# ICD 4.6.3, Table 21. RZ/We/Wd use the raw value, 0 = ENABLED and 1 = DISABLED, but the table's
# Default column is logical (comm-uart defaults to 0, and the hand ships in I2C mode).
FLAGS = [Flag(*f) for f in [
    (0, "vibration", 1, "Vibration when touch sensors are pressed"),
    (1, "contact-reflex", 0, "Contact reflex, triggered by touch sensors"),
    (2, "no-adapter-encoder", 0, "Check for adapter encoder"),
    (3, "poweroff-grip", 0, "Perform power-off grip on power-off signal"),
    (5, "electrode-flip", 0, "Swap open/close electrode roles"),
    (6, "gearbox-32-to-1", 0, "Flag motor gearboxes as 32.45:1"),
    (7, "ms-open-on-open", 1, "Direct control mode switch: open on open"),
    (8, "ms-double-impulse-close", 0, "Direct control mode switch: double impulse close"),
    (9, "ms-double-impulse-open", 0, "Direct control mode switch: double impulse open"),
    (10, "ms-cocontract-short", 0, "Direct control mode switch: short co-contract"),
    (11, "ms-cocontract-long", 0, "Direct control mode switch: long co-contract"),
    (12, "ms-long-open", 0, "Direct control mode switch: long open"),
    (13, "ms-long-close", 0, "Direct control mode switch: long close"),
    (14, "no-fs-loading", 0, "Do not load saved finger positions on startup"),
    (15, "disable-auto-lp-mode", 0, "Ignore I2C wake-from-low-power signal"),
    (16, "comm-uart", 0, "UART/RS485 on the COMM lines (I2C disabled)"),
    (17, "slip-detection", 0, "Slip detection"),
    (18, "tap-freeze", 0, "IMU triple tap -> freeze"),
    (19, "first-over", 1, "'First over' strategy for direct control"),
    (20, "tap-modeswitch", 0, "IMU double tap -> next grip"),
    (22, "disable-dc-filter", 0, "No low-pass filter on direct control electrodes"),
    (25, "no-tap-ms-while-closed", 0, "No tap mode switching while closed"),
    (26, "coapt-force-2input-oc", 0, "Hide from Coapt on startup"),
    (27, "force-high-power", 0, "Always highest power state"),
    (28, "disable-waggle", 0, "Waggle grip becomes an empty user grip"),
    (30, "ultrafast", 0, "Experimental: faster fingers over small ranges"),
    (31, "pullstring-lockout", 0, "Pullstring/linear transducer lockout"),
    (32, "idle-relax", 0, "Relax when open and idle"),
    (33, "fast-uart", 0, "UART fast response mode"),
    (35, "rs485", 0, "RS485 on the COMM lines (v10 hardware)"),
    (36, "rs485-master-demo", 0, "Hand acts as UART/RS485 master (mirror demo)"),
    (37, "rgb-disabled", None, "Disable RGB light on the power switch"),
    (39, "disable-airtemp-protection", 0, "DANGEROUS: disable ambient overtemperature cut-off"),
    (40, "disable-fast-startup", 0, "Higher stall threshold for startup re-zeroing"),
    (46, "ppp-stuffing", 0, "PPP byte stuffing of API reply frames (hand TX)"),
    (47, "ppp-unstuffing", 0, "PPP unstuffing of API receive frames (hand RX)"),
]]
FLAGS_BY_INDEX = {f.index: f for f in FLAGS}
FLAGS_BY_NAME = {f.name: f for f in FLAGS}
RESERVED_FLAGS = {4, 21, 23, 24, 29, 34, 38, *range(41, 46), *range(48, 64)}
DANGEROUS_FLAGS = {39}


@dataclass(frozen=True)
class Word:
    index: int
    name: str
    fmt: str          # 'float' (Rm/Wm) or 'hex' (Rn/Wn)
    description: str


# ICD 4.6.1 / 4.6.2. Words share one memory bank; the format is not checked by the hand.
WORDS = [Word(*w) for w in [
    (0, "idle-timeout", "float", "Seconds without EMG activity (hand open) before relaxing"),
    (1, "api-config0", "hex", "UART/RS485 API: byte1 slave address, byte2 baud enum (0 = defaults)"),
    (3, "hardware-profile", "hex", "Hardware version: 1 = pre-v10, 2 = v10 (read only, rewritten at boot)"),
    (4, "user-power-limit", "hex", "Power limit, 0 = off, 0x20-0xFF = 10-100 % (not applied in API mode)"),
    (5, "hot-thresholds-offsets", "hex", "Overtemp hot/cold thresholds + calibration (do not modify)"),
    (6, "temp-offsets", "hex", "Temperature calibration offsets (do not modify)"),
    (7, "collision-voltage", "float", "API mode stall/collision threshold in V (0 = default 3.0 V)"),
    (8, "api-postcontact-voltage", "float", "API mode voltage cap after a collision (default 0 V)"),
    (17, "wakeup-word-fingers", "float", "Sensor type init bytes: index, middle, ring, pinky"),
    (18, "wakeup-word-thumb", "float", "Sensor type init bytes: thumb flexor, thumb rotator"),
]]
WORDS_BY_INDEX = {w.index: w for w in WORDS}
WORDS_BY_NAME = {w.name: w for w in WORDS}
READONLY_WORDS = {3, 5, 6}


@dataclass(frozen=True)
class Stream:
    command: str
    labels: list[str]
    kind: str         # 'float' (n x float32), 'fsr' (30 touch values) or 'text' (console plot)
    description: str


STREAM_STOP = "P1"
XYZ = ["tap", "acc_x", "acc_y", "acc_z", "gyro_x", "gyro_y", "gyro_z"]
FSR_LABELS = [f"{f}{s}" for f in FINGERS[:5] for s in range(6)]

# ICD 4.7. Float streams are little-endian IEEE 754 single precision.
STREAMS = {
    "position": Stream("P5", FINGERS, "float", "Finger positions [deg]"),
    "velocity": Stream("P8", FINGERS, "float", "Finger velocities"),
    "current": Stream("PE", FINGERS, "float", "Motor currents"),
    "goal": Stream("PC", FINGERS, "float", "Grip-mode position goals [deg] (API goals not shown)"),
    "progress": Stream("PD", FINGERS, "float", "Per-finger motion progress 0-1"),
    "touch": Stream("P0", FINGERS, "float", "Touch sensor summary, 6 floats"),
    "direct": Stream("P2", [f"ch{i}" for i in range(6)], "float", "Direct control (EMG) signals"),
    "fsr": Stream("P4", FSR_LABELS, "fsr", "All 30 touch sensors, 6 per finger"),
    "imu": Stream("P9", XYZ, "float", "Tap detection value + raw accelerometer/gyroscope"),
    "grip": Stream("PB", ["close", "open"], "float", "Grip percentage 0-1"),
    "encoder": Stream("PF", ["adapter_encoder"], "float", "Adapter encoder position"),
    "battery": Stream("PH0", ["volts"], "float", "Supply voltage [V]"),
    "temperature": Stream("PI", [], "text", "Motor driver junction temperatures [C] (console text)"),
    "fsr-text": Stream("P41", [], "text", "Sensor 0 of each finger + checksum status (console text)"),
    "battery-text": Stream("PH1", [], "text", "Supply voltage (console text)"),
    "encoder-text": Stream("PG", [], "text", "Adapter encoder (console text)"),
}


# ---------------------------------------------------------------- encoders

def resolve_grip(grip: str | int) -> int:
    """Grip name or index (0-63, -1 = open current grip) -> index."""
    if isinstance(grip, int) or re.fullmatch(r"-?\d+", str(grip)):
        idx = int(grip)
        if not -1 <= idx <= 63:
            raise ValueError(f"grip index {idx} out of range (-1..63)")
        return idx
    key = str(grip).lower().replace("_", "-")
    if key in GRIPS:
        return GRIPS[key]
    matches = [n for n in GRIPS if n.startswith(key)]
    if len(matches) == 1:
        return GRIPS[matches[0]]
    raise ValueError(f"unknown grip {grip!r}; choose from {', '.join(GRIPS)} or 0-63")


def grip_cmd(index: int, period: float) -> str:
    """ICD 4.3.2: 'gxx:period' with a 2-character grip index."""
    if period <= 0:
        raise ValueError("period must be > 0")
    return f"g{index:02d}:{period:.3f}"


def resolve_flag(flag: str | int) -> int:
    if isinstance(flag, int) or str(flag).isdigit():
        idx = int(flag)
        if not 0 <= idx <= 63:
            raise ValueError(f"flag index {idx} out of range (0..63)")
        return idx
    key = str(flag).lower().replace("_", "-")
    if key in FLAGS_BY_NAME:
        return FLAGS_BY_NAME[key].index
    raise ValueError(f"unknown flag {flag!r}; see 'psyonic-ble flag list'")


def resolve_word(word: str | int) -> int:
    if isinstance(word, int) or str(word).isdigit():
        idx = int(word)
        if not 0 <= idx <= 215:
            raise ValueError(f"word index {idx} out of range (0..215)")
        return idx
    key = str(word).lower().replace("_", "-")
    if key in WORDS_BY_NAME:
        return WORDS_BY_NAME[key].index
    raise ValueError(f"unknown word {word!r}; see 'psyonic-ble word list'")


def word_read_cmd(index: int, fmt: str) -> str:
    return f"{'Rm' if fmt == 'float' else 'Rn'}{index:03d}"


def word_write_cmd(index: int, fmt: str, value: str) -> str:
    if fmt == "float":
        return f"Wm{index:03d}:{float(value)}"
    return f"Wn{index:03d}:0x{int(value, 0) & 0xFFFFFFFF:08X}"


def hexcolor_cmd(color: str) -> str:
    color = color.lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", color):
        raise ValueError("color must be 6 hex digits, e.g. 00FE45")
    return f"CN{color.upper()}"


# ---------------------------------------------------------------- reply parsers

def parse_flag_reply(text: str) -> dict[int, int]:
    return {int(m.group(1)): int(m.group(2)) for m in re.finditer(r"BIN(\d+)\s*=\s*(\d)", text)}


def parse_word_reply(text: str) -> dict[int, str]:
    return {int(m.group(1)): m.group(2).strip() for m in re.finditer(r"mem\[(\d+)\]\s*=\s*(\S+)", text)}


def unpack_12bit(data: bytes, count: int) -> list[int]:
    """Values packed back to back as 12-bit little-endian fields (MATLAB unpack_8bit_into_12bit.m)."""
    n = int.from_bytes(data, "little")
    return [(n >> (12 * i)) & 0xFFF for i in range(count)]


def decode_stream(stream: Stream, data: bytes) -> list[float] | str | None:
    """Decode one notification of a stream. Returns values, a text line, or None if it doesn't fit."""
    if stream.kind == "float":
        n = len(stream.labels)
        if len(data) == 4 * n:
            return list(struct.unpack(f"<{n}f", data))
    elif stream.kind == "fsr":
        # ICD says 60 bytes of int16; PSYONIC's MATLAB plotter expects 45 bytes of packed 12-bit
        if len(data) == 45:
            return unpack_12bit(data, 30)
        if len(data) == 60:
            return list(struct.unpack("<30h", data))
    text = data.decode("ascii", errors="replace").strip().rstrip("\x00").strip()
    if stream.kind == "text" or (text and text.isprintable()):
        return text
    return None


def printable(data: bytes) -> str:
    text = data.decode("ascii", errors="replace").strip().rstrip("\x00").strip()
    return text if text and text.isprintable() else data.hex(" ")
