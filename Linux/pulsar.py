#!/usr/bin/env python3
"""
pulsar.py - Linux config tool for the HATOR Pulsar Wireless (SINOWEALTH 258A:002E / 002F)

Protocol recovered by static analysis of OemDrv.exe (CDrv8948 class, MsFw=5).
No dependencies beyond the Python stdlib (talks to /dev/hidraw* with ioctls).

SAFE BY DEFAULT: every command except `set-*` is read-only. `set-*` commands only print
a diff unless you pass --write. A write is a read-modify-write of the 146-byte profile
block (exactly what the vendor app does), and a backup of the original reply is saved.
Writes are refused if the block doesn't carry the expected markers.

    ./pulsar.py list                     find hidraw nodes + which feature reports they carry
    ./pulsar.py info                     firmware/status/power/active profile (raw + inferred)
    ./pulsar.py dump [-p N]              read + decode profile N (default: active profile)
    ./pulsar.py set-dpi STAGE DPI        stage 1..8, DPI 100..16000 in steps of 100
    ./pulsar.py set-rate HZ              125|250|500|1000
    ./pulsar.py set-stage N              select current DPI stage
    ./pulsar.py set-stages N             number of active DPI stages (1..8; vendor UI offers 7)
    ./pulsar.py set-color STAGE RRGGBB   DPI-stage indicator colour
    ./pulsar.py select-profile N         send the vendor "set profile" command (cmd 02)
    ./pulsar.py restore FILE             write a backup file back (needs --write)
    ./pulsar.py watch [--seconds N]      read-only: print live input reports from every node of the device, decoding the
                                         vendor event report (ID 7: USB/battery mode, polling rate, DPI stage)

  LED (effect layout recovered from OemDrv.exe 0x4198d0; hardware-untested):
    ./pulsar.py leds                     list effects and show the current one
    ./pulsar.py set-led EFFECT [--speed N] [--brightness N] [--color RRGGBB ...] [--direction 0|1]

  Buttons (matrix encoding verified by emulating the vendor encoder 0x416580; hardware-untested):
    ./pulsar.py buttons                  show the matrix this tool believes is on the mouse
    ./pulsar.py set-button POS FUNC      POS 1..20, FUNC: left|right|...|key:ctrl+c|media:mute|raw:11010000
    ./pulsar.py reset-buttons            write the Cfg.ini default matrix
    ./pulsar.py dump-buttons --experimental   try to read the matrix back (vendor app never does)
"""
import argparse, fcntl, glob, os, struct, sys, time

VID = 0x258A
PIDS = (0x002E, 0x002F)

CMD_ID, CMD_LEN = 5, 8          # command channel: feature report ID 5, 8 bytes total
DATA_ID, DATA_LEN = 8, 520      # data channel: feature report ID 8 (MsFw&0xF==5), 520 bytes total
BLOCK_LEN = 0x92                # "general" profile block (146 bytes)
HDR_LEN = 8                     # report[0]=ID, report[1..7]=header, block at report[8]
RATE_FROM_CODE = {1: 125, 2: 250, 3: 500, 4: 1000}
CODE_FROM_RATE = {v: k for k, v in RATE_FROM_CODE.items()}
N_STAGES = 8
DPI_MIN, DPI_MAX, DPI_STEP = 100, 16000, 100   # sensor 0x3335 table from OemDrv.exe 0x4770e0


# ---------------------------------------------------------------- hidraw ioctls
def _ioc(d, t, nr, size):
    return (d << 30) | (size << 16) | (ord(t) << 8) | nr


def hid_set_feature(fd, data):
    buf = bytearray(data)
    fcntl.ioctl(fd, _ioc(3, 'H', 0x06, len(buf)), buf, True)


def hid_get_feature(fd, rid, n):
    buf = bytearray(n)
    buf[0] = rid
    fcntl.ioctl(fd, _ioc(3, 'H', 0x07, n), buf, True)
    return bytes(buf)


def hid_report_descriptor(fd):
    sz = bytearray(4)
    fcntl.ioctl(fd, _ioc(2, 'H', 0x01, 4), sz, True)          # HIDIOCGRDESCSIZE
    size = struct.unpack("i", sz)[0]
    buf = bytearray(4 + 4096)
    struct.pack_into("I", buf, 0, size)
    fcntl.ioctl(fd, _ioc(2, 'H', 0x02, len(buf)), buf, True)  # HIDIOCGRDESC
    return bytes(buf[4:4 + size])


# ---------------------------------------------------------------- descriptor parsing
def parse_descriptor(desc):
    """Return list of top-level collections: {page, usage, features:{rid: bytes_incl_id}}"""
    i, page, rid, size, count = 0, 0, 0, 0, 0
    usage_pending = []
    depth, cols, cur = 0, [], None
    stack = []
    while i < len(desc):
        b = desc[i]
        if b == 0xFE:                       # long item
            i += 3 + desc[i + 1]
            continue
        sz = {0: 0, 1: 1, 2: 2, 3: 4}[b & 3]
        typ, tag = (b >> 2) & 3, b >> 4
        val = int.from_bytes(desc[i + 1:i + 1 + sz], "little") if sz else 0
        i += 1 + sz
        if typ == 1:                        # global
            if tag == 0: page = val
            elif tag == 7: size = val
            elif tag == 8: rid = val
            elif tag == 9: count = val
            elif tag == 10: stack.append((page, rid, size, count))
            elif tag == 11 and stack: page, rid, size, count = stack.pop()
        elif typ == 2:                      # local
            if tag == 0: usage_pending.append(val)
        elif typ == 0:                      # main
            if tag == 0xA:                  # collection
                if depth == 0:
                    cur = {"page": page, "usage": usage_pending[0] if usage_pending else 0,
                           "features": {}, "inputs": {}}
                    cols.append(cur)
                depth += 1
                usage_pending = []
            elif tag == 0xC:
                depth -= 1
                usage_pending = []
            elif tag in (0x8, 0xB) and cur is not None:
                bits = size * count
                key = "features" if tag == 0xB else "inputs"
                cur[key][rid] = cur[key].get(rid, 0) + bits
                usage_pending = []
            else:
                usage_pending = []
    for c in cols:
        for k in ("features", "inputs"):
            c[k] = {r: (bits + 7) // 8 + (1 if r else 0) for r, bits in c[k].items()}
    return cols


def sysfs_nodes():
    out = []
    for path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            uevent = open(os.path.join(path, "device/uevent")).read()
        except OSError:
            continue
        for line in uevent.splitlines():
            if line.startswith("HID_ID="):
                _, v, p = line.split("=")[1].split(":")
                if int(v, 16) == VID and int(p, 16) in PIDS:
                    out.append(("/dev/" + os.path.basename(path), int(p, 16)))
    return out


# ---------------------------------------------------------------- device
class Pulsar:
    def __init__(self, cmd_fd, data_fd, pid=0, retries=3, data_id=DATA_ID):
        self.cfd, self.dfd, self.pid, self.retries, self.data_id = cmd_fd, data_fd, pid, retries, data_id

    @classmethod
    def open(cls, node=None, data_id=DATA_ID):
        """Open the HID feature-report endpoints, closing any unused descriptors."""
        nodes = [(node, 0)] if node else sysfs_nodes()
        if not nodes:
            sys.exit("no 258a:002e/002f hidraw node found (is the receiver/cable connected? udev rule installed?)")
        cfd = dfd = None
        pid = 0
        errors = []
        for path, device_pid in nodes:
            fd = None
            try:
                fd = os.open(path, os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
                collections = parse_descriptor(hid_report_descriptor(fd))
            except PermissionError:
                if fd is not None:
                    os.close(fd)
                sys.exit(f"permission denied on {path}: install the HATOR Pulsar udev rule and log in again")
            except (OSError, ValueError, IndexError, struct.error) as exc:
                errors.append(f"{path}: {exc}")
                if fd is not None:
                    os.close(fd)
                continue
            has_cmd = any(col["features"].get(CMD_ID) == CMD_LEN for col in collections)
            has_data = any(col["features"].get(data_id) == DATA_LEN for col in collections)
            keep = False
            if has_cmd and cfd is None:
                cfd, pid, keep = fd, device_pid, True
            if has_data and dfd is None:
                dfd, pid, keep = fd, device_pid, True
            if not keep:
                os.close(fd)
            if cfd is not None and dfd is not None:
                break
        if cfd is None or dfd is None:
            for fd in {cfd, dfd} - {None}:
                try:
                    os.close(fd)
                except OSError:
                    pass
            detail = (" Details: " + "; ".join(errors)) if errors else ""
            sys.exit("found no complete vendor HID interface (feature report ID 5 x8 bytes and ID "
                     f"{data_id} x520 bytes). Run `pulsar.py list` to inspect the device." + detail)
        return cls(cfd, dfd, pid, data_id=data_id)

    def close(self):
        """Close each underlying HID descriptor once (command and data can share one fd)."""
        fds = {self.cfd, self.dfd}
        self.cfd = self.dfd = -1
        for fd in fds:
            if fd is not None and fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass

    # -- command channel (report ID 5): 05 c0 c1 c2 c3 c4 00 00
    def send_cmd(self, c):
        assert len(c) == 5
        rep = bytes([CMD_ID]) + bytes(c) + b"\0\0"
        for n in range(self.retries):
            try:
                hid_set_feature(self.cfd, rep)
                return
            except OSError:
                time.sleep(0.2)
        raise IOError("SendCmd failed")

    def get_cmd(self):
        for n in range(self.retries):
            try:
                return hid_get_feature(self.cfd, CMD_ID, CMD_LEN)       # [5, r0..r4, x, x]
            except OSError:
                time.sleep(0.2)
        raise IOError("GetCmd failed")

    def cmd(self, c, wait=0.02):
        self.send_cmd(c)
        time.sleep(wait)
        return self.get_cmd()

    # -- data channel (report ID 8, 520 bytes)
    def get_data(self):
        for n in range(self.retries):
            try:
                return hid_get_feature(self.dfd, self.data_id, DATA_LEN)
            except OSError:
                time.sleep(0.2)
        raise IOError("GetData failed")

    def set_data(self, cmdbyte, payload, trailer=b""):
        rep = bytearray(DATA_LEN)
        rep[0] = self.data_id
        rep[1] = cmdbyte
        rep[3] = len(payload)                      # declared length excludes the trailer
        rep[HDR_LEN:HDR_LEN + len(payload)] = payload
        rep[HDR_LEN + len(payload):HDR_LEN + len(payload) + len(trailer)] = trailer
        for n in range(self.retries):
            try:
                hid_set_feature(self.dfd, bytes(rep))
                return
            except OSError:
                time.sleep(0.2)
        raise IOError("SendData failed")

    # -- high level
    def active_profile(self):
        r = self.cmd([0x02, 0, 0, 0, 0])
        return r[2] if r[2] in (1, 2, 3) else None

    def select_profile(self, n):
        """cmd 02 <n>: vendor SetProfile (0x415150 sends [2, idx+1]). Returns the active profile afterwards."""
        self.send_cmd([0x02, n, 0, 0, 0])
        time.sleep(0.05)
        return self.active_profile()

    def read_profile_raw(self, n):
        self.send_cmd([(n << 4) | 1, 0, 0, 0, 0])
        time.sleep(0.02)
        return self.get_data()

    def write_profile(self, n, block):
        assert len(block) == BLOCK_LEN
        self.set_data((n << 4) | 1, bytes(block))
        time.sleep(0.05)

    def write_buttons(self, n, matrix):
        """Vendor 0x415720 (verified by emulation): cmd (n<<4)|2, declared len 0x50, 80 payload bytes,
        then a 0xA5 end marker *after* the payload (payload[80])."""
        assert len(matrix) == N_BTN
        payload = b"".join(struct.pack("<I", v) for v in matrix)
        self.set_data((n << 4) | 2, payload, trailer=b"\xa5")
        time.sleep(0.05)

    def read_buttons_experimental(self, n):
        """UNVERIFIED: the vendor app never reads the matrix. Mirrors the profile read with opcode |2."""
        self.send_cmd([(n << 4) | 2, 0, 0, 0, 0])
        time.sleep(0.02)
        return self.get_data()


# ---------------------------------------------------------------- block codec
def reg_from_dpi(dpi):
    if dpi % DPI_STEP or not DPI_MIN <= dpi <= DPI_MAX:
        raise ValueError(f"DPI must be {DPI_MIN}..{DPI_MAX} in steps of {DPI_STEP}")
    return dpi // DPI_STEP - 1


def dpi_from_reg(reg):
    return (reg + 1) * DPI_STEP


def block_of(report):
    if report[0] not in (DATA_ID, 4, 6, 8):
        raise ValueError("unexpected report id %#x" % report[0])
    return bytearray(report[HDR_LEN:HDR_LEN + BLOCK_LEN])


def markers_ok(b):
    return b[0] == 0x64 and b[0x90] == 0xA5 and b[1] != 0 and b[3] != 0


def decode(b):
    d = {
        "marker0": b[0], "serial?": b[1],
        "rate_hz": RATE_FROM_CODE.get(b[2] & 0xF),
        "rate_code": b[2] & 0xF,
        "xy_independent?": bool(b[2] & 0x80),
        "cur_stage": b[3] >> 4, "n_stages": b[3] & 0xF,
        "dpi_x": [dpi_from_reg(struct.unpack_from("<H", b, 5 + 2 * k)[0]) for k in range(N_STAGES)],
        "dpi_y": [dpi_from_reg(struct.unpack_from("<H", b, 0x15 + 2 * k)[0]) for k in range(N_STAGES)],
        "colors": ["%02X%02X%02X" % tuple(b[0x25 + 3 * k:0x28 + 3 * k]) for k in range(N_STAGES)],
        "led_effect_byte": b[0x3D], "flags91": b[0x91], "marker90": b[0x90],
    }
    return d


def print_decoded(b):
    d = decode(b)
    print(f"  markers     : b[0]={d['marker0']:#04x} (want 0x64)  b[0x90]={d['marker90']:#04x} (want 0xA5)"
          f"  -> {'OK' if markers_ok(b) else 'MISMATCH'}")
    print(f"  polling     : {d['rate_hz']} Hz (code {d['rate_code']})")
    print(f"  stages      : {d['n_stages']} active, current = {d['cur_stage']}   (xy-independent flag: {d['xy_independent?']})")
    for k in range(N_STAGES):
        mark = "<--" if k + 1 == d["cur_stage"] else ""
        yv = f"  Y={d['dpi_y'][k]}" if d["xy_independent?"] else ""
        print(f"    stage {k+1}: X={d['dpi_x'][k]:5d}{yv}  colour #{d['colors'][k]} {mark}")
    print(f"  flags[0x91] : {d['flags91']:#04x}")
    print_led(b)


def hexdump(b, w=16):
    for i in range(0, len(b), w):
        print(f"    {i:04x}  " + " ".join(f"{x:02x}" for x in b[i:i + w]))


# ---------------------------------------------------------------- patches
def patch_dpi(b, stage, dpi):
    reg = reg_from_dpi(dpi)
    struct.pack_into("<H", b, 5 + 2 * (stage - 1), reg)
    if b[2] & 0x80:
        struct.pack_into("<H", b, 0x15 + 2 * (stage - 1), reg)


def patch_rate(b, hz):
    b[2] = (b[2] & 0xF0) | CODE_FROM_RATE[hz]


def patch_stage(b, n):
    if not 1 <= n <= (b[3] & 0xF):
        raise ValueError(f"stage must be 1..{b[3] & 0xF}")
    b[3] = (n << 4) | (b[3] & 0xF)


def patch_nstages(b, n):
    """Number of active DPI stages (low nibble of b[3]); the current stage is clamped into range."""
    if not 1 <= n <= N_STAGES:
        raise ValueError(f"number of stages must be 1..{N_STAGES}")
    cur = min(b[3] >> 4, n) or 1
    b[3] = (cur << 4) | n


def patch_color(b, stage, rgb):
    b[0x25 + 3 * (stage - 1):0x28 + 3 * (stage - 1)] = bytes.fromhex(rgb)


# ---------------------------------------------------------------- LED effects (OemDrv.exe 0x4198d0)
# block[0x3D] = effect id; each effect owns a fixed slice of the block. "ctl" byte = (brightness << 4) | speed.
# Which nibble is brightness/speed is inferred: Steady/Colorful-Steady only write the HIGH nibble, Tail/Off
# only the LOW nibble, and Steady has brightness but no speed in the UI.
LED_EFFECTS = {   # id: (name, ctl, has_speed, has_brightness, colour_off, max_colours, count_off, zero_span)
    1: ("streaming",       0x3E, True,  True,  None, 0, None, 0),     # block[0x3F] = direction bit
    2: ("steady",          0x40, False, True,  0x41, 1, None, 0),
    3: ("breathing",       0x44, True,  True,  0x46, 7, 0x45, 21),
    4: ("tail",            0x5B, True,  False, None, 0, None, 0),
    5: ("neon",            0x5C, True,  True,  None, 0, None, 0),
    6: ("colorful-steady", 0x5D, False, True,  0x5E, 8, None, 30),
    7: ("flicker",         0x7C, True,  True,  0x7D, 2, None, 6),
    8: ("stars",           0x83, True,  True,  None, 0, None, 0),
    9: ("wave",            0x84, True,  True,  None, 0, None, 0),
    10: ("off",            0x85, True,  False, None, 0, None, 0),
}
LED_BY_NAME = {v[0]: k for k, v in LED_EFFECTS.items()}
LED_UI_NAMES = {1: "Colorful Streaming", 2: "Steady", 3: "Breathing", 4: "Colorful Tail", 5: "Neon",
                6: "Colorful Steady", 7: "Flicker", 8: "Stars twinkle", 9: "Wave", 10: "LED OFF"}


def led_effect_id(s):
    s = s.lower()
    if s.isdigit() and int(s) in LED_EFFECTS:
        return int(s)
    if s in LED_BY_NAME:
        return LED_BY_NAME[s]
    raise ValueError("effect must be 1..10 or one of: " + ", ".join(LED_BY_NAME))


def decode_led(b):
    eid = b[0x3D]
    out = {"effect": eid, "name": LED_EFFECTS.get(eid, ("unknown",))[0]}
    if eid in LED_EFFECTS:
        _, ctl, has_s, has_b, coff, maxc, cnt, _z = LED_EFFECTS[eid]
        out["ctl_byte"] = b[ctl]
        if has_s: out["speed"] = b[ctl] & 0xF
        if has_b: out["brightness"] = b[ctl] >> 4
        if coff is not None:
            n = b[cnt] if cnt is not None else maxc
            n = min(n, maxc)
            out["colors"] = ["%02X%02X%02X" % tuple(b[coff + 3 * k:coff + 3 * k + 3]) for k in range(n)]
        if eid == 1:
            out["direction_bit"] = b[0x3F]
    return out


def patch_led(b, effect, speed=None, brightness=None, colors=None, direction=None):
    name, ctl, has_s, has_b, coff, maxc, cnt, zero = LED_EFFECTS[effect]
    b[0x3D] = effect
    cur = b[ctl]
    if speed is not None:
        if not has_s: raise ValueError(f"effect '{name}' has no speed setting")
        if not 0 <= speed <= 15: raise ValueError("speed must be 0..15")
        cur = (cur & 0xF0) | speed
    if brightness is not None:
        if not has_b: raise ValueError(f"effect '{name}' has no brightness setting")
        if not 0 <= brightness <= 15: raise ValueError("brightness must be 0..15")
        cur = (cur & 0x0F) | (brightness << 4)
    b[ctl] = cur
    if direction is not None:
        if effect != 1: raise ValueError("--direction only applies to the 'streaming' effect")
        b[0x3F] = 1 if direction else 0       # vendor: block[0x3F] = (UI direction value == 3)
    if colors:
        if coff is None: raise ValueError(f"effect '{name}' takes no colours")
        if len(colors) > maxc: raise ValueError(f"effect '{name}' takes at most {maxc} colour(s)")
        if zero:
            b[coff:coff + zero] = bytes(zero)
        if cnt is not None:
            b[cnt] = len(colors)
        for k, c in enumerate(colors):
            b[coff + 3 * k:coff + 3 * k + 3] = bytes.fromhex(c)


def print_led(b):
    d = decode_led(b)
    print(f"  effect      : {d['effect']} ({d['name']})")
    for k in ("speed", "brightness", "colors", "direction_bit"):
        if k in d:
            print(f"    {k:11}: {d[k]}")
    print(f"    ctl byte   : {d.get('ctl_byte', 0):#04x}   (stage colours at 0x25.. are NOT used by these effects)")


# ---------------------------------------------------------------- button matrix (OemDrv.exe 0x416580 / 0x415720)
# 20 slots x one little-endian dword [b0 class, b1, b2, b3]. The slot number is the 4th field of Cfg.ini K<n>_1.
# Classes were extracted by *emulating the vendor encoder*, not by reading its tables by eye:
#   0x11 mouse button  b1 = HID button bit mask (1 left, 2 right, 4 middle, 8 back, 0x10 forward)
#   0x12 scroll        b1 = +1 up / 0xFF down
#   0x31 multi-click   [31 01 32 N]  left click repeated N times
#   0x21 keyboard      b1 = HID modifier mask, b2/b3 = up to two HID key usages
#   0x22 consumer      b1..b3 = 24-bit consumer usage bitmask (bits below)
#   0x50/0x41/0x60     special functions (DPI, tilt, fire key...); names NOT verified, use raw:
N_BTN = 20
BTN_UNASSIGNED = 0x150     # vendor initialises every slot to this ([50 01 00 00])
BTN_DEFAULTS = {1: 0x111, 2: 0x211, 3: 0x411, 4: 0x811, 5: 0x1011, 6: 0x750, 7: 0x41, 8: 0x450, 9: 0x150, 10: 0x150}
BTN_NAMED = {
    "left": 0x111, "right": 0x211, "middle": 0x411, "back": 0x811, "forward": 0x1011,
    "scroll-up": 0x112, "scroll-down": 0xFF12, "double-click": 0x2320131, "triple-click": 0x3320131,
    "disable": 0x150,
    "dpi-loop": 0x241,     # hardware-confirmed: next DPI stage, wraps from the last active stage to stage 1
    "dpi-up": 0x141,       # hardware-confirmed: next DPI stage, saturates at the last active stage (does not wrap)
}
# Firmware defaults whose exact behaviour is not decoded; labels are hints only (physical slots confirmed on hardware:
# slot 7 = DPI button, slot 8 = report-rate button; slot 6 = button under the wheel; slots 9, 10 have no button).
SPECIAL_HINTS = {0x41: "factory default of slot 7, DPI button: stage select",
                 0x450: "factory default of slot 8, report-rate button: cycles polling rate",
                 0x750: "factory default of slot 6, button under wheel: function unknown"}
MEDIA = {   # name: dword (bit positions emulated from vendor 0x4163a0 for the matching HID consumer usage)
    "play-pause": 0x822, "next": 0x122, "prev": 0x222, "stop": 0x422, "vol-up": 0x4022, "vol-down": 0x8022,
    "mute": 0x1022, "media-player": 0x10022, "explorer": 0x20022, "mail": 0x100022, "calculator": 0x200022,
    "search": 0x1000022, "home": 0x2000022, "browser-back": 0x4000022, "browser-forward": 0x8000022,
    "www-stop": 0x10000022, "refresh": 0x20000022, "favorites": 0x40000022,
}
MODS = {"ctrl": 1, "shift": 2, "alt": 4, "win": 8, "gui": 8, "super": 8, "rctrl": 0x10, "rshift": 0x20, "ralt": 0x40, "rwin": 0x80}
KEYS = {chr(ord("a") + i): 0x04 + i for i in range(26)}
KEYS.update({str(d): 0x1E + (d - 1) for d in range(1, 10)}); KEYS["0"] = 0x27
KEYS.update({f"f{i}": 0x3A + i - 1 for i in range(1, 13)})
# HID keyboard usages for F13-F24 (needed for virtual macro trigger keys).
KEYS.update({f"f{i}": 0x68 + i - 13 for i in range(13, 25)})
KEYS.update({"enter": 0x28, "esc": 0x29, "backspace": 0x2A, "tab": 0x2B, "space": 0x2C, "minus": 0x2D, "equal": 0x2E,
             "lbracket": 0x2F, "rbracket": 0x30, "backslash": 0x31, "semicolon": 0x33, "quote": 0x34, "grave": 0x35,
             "comma": 0x36, "period": 0x37, "slash": 0x38, "capslock": 0x39, "printscreen": 0x46, "scrolllock": 0x47,
             "pause": 0x48, "insert": 0x49, "home": 0x4A, "pageup": 0x4B, "delete": 0x4C, "end": 0x4D,
             "pagedown": 0x4E, "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52})


def parse_button(spec):
    s = spec.strip().lower()
    if s in BTN_NAMED:
        return BTN_NAMED[s]
    if s.startswith("media:"):
        n = s[6:]
        if n not in MEDIA: raise ValueError("media: one of " + ", ".join(MEDIA))
        return MEDIA[n]
    if s.startswith("key:"):
        mods, keys = 0, []
        for part in s[4:].split("+"):
            if part in MODS: mods |= MODS[part]
            elif part in KEYS: keys.append(KEYS[part])
            else: raise ValueError(f"unknown key/modifier '{part}'")
        if len(keys) > 2: raise ValueError("at most 2 non-modifier keys")
        keys += [0] * (2 - len(keys))
        return 0x21 | (mods << 8) | (keys[0] << 16) | (keys[1] << 24)
    if s.startswith("raw:"):
        raw = bytes.fromhex(s[4:])
        if len(raw) != 4: raise ValueError("raw: needs exactly 4 bytes, e.g. raw:50070000")
        return struct.unpack("<I", raw)[0]
    raise ValueError("FUNC: " + ", ".join(BTN_NAMED) + ", key:ctrl+c, media:<name>, raw:AABBCCDD")


def describe_button(v):
    raw = struct.pack("<I", v)
    for k, x in BTN_NAMED.items():
        if x == v: return k
    for k, x in MEDIA.items():
        if x == v: return "media:" + k
    if raw[0] == 0x21:
        names = [n for n, bit in MODS.items() if raw[1] & bit and n not in ("gui", "super")]
        rev = {u: n for n, u in KEYS.items()}
        keys = [rev.get(u, f"usage{u:#04x}") for u in raw[2:4] if u]
        return "key:" + "+".join(names + keys)
    if v in SPECIAL_HINTS:
        return "special (" + SPECIAL_HINTS[v] + ")"
    if raw[0] in (0x50, 0x41, 0x60):
        return "special (unverified meaning)"
    return "?"


def default_matrix():
    m = [BTN_UNASSIGNED] * N_BTN
    for pos, v in BTN_DEFAULTS.items():
        m[pos - 1] = v
    return m


def buttons_state_path(n):
    return os.path.expanduser(f"~/.config/pulsar-linux/buttons-p{n}.json")


def load_matrix_state(n):
    """Load the last locally remembered matrix. This is not a live device read-back."""
    import json
    try:
        with open(buttons_state_path(n), encoding="utf-8") as f:
            matrix = json.load(f)
        if (isinstance(matrix, list) and len(matrix) == N_BTN
                and all(isinstance(x, int) and 0 <= x <= 0xFFFFFFFF for x in matrix)):
            return list(matrix), True
    except (OSError, ValueError, TypeError):
        pass
    return default_matrix(), False


def _atomic_json(path, value):
    import json
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    temporary = path + f".tmp-{os.getpid()}"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def save_matrix_state(n, m):
    if len(m) != N_BTN or not all(isinstance(x, int) and 0 <= x <= 0xFFFFFFFF for x in m):
        raise ValueError(f"button matrix must contain {N_BTN} unsigned 32-bit values")
    _atomic_json(buttons_state_path(n), list(m))


def save_matrix_backup(n, matrix, known=False):
    """Back up the local button-map base before replacing it; `known` indicates prior local state only."""
    import json
    bdir = os.path.expanduser("~/.config/pulsar-linux")
    os.makedirs(bdir, mode=0o700, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000_000:09d}"
    path = os.path.join(bdir, f"buttons-backup-{stamp}-p{n}.json")
    with open(path, "x", encoding="utf-8") as f:
        json.dump({"profile": n, "previous_local_state_known": bool(known), "matrix": list(matrix)}, f, indent=2)
        f.write("\n")
    os.chmod(path, 0o600)
    return path


def print_matrix(m, note=""):
    if note: print(note)
    for i, v in enumerate(m, 1):
        raw = struct.pack("<I", v).hex(" ")
        flag = "" if v != BTN_UNASSIGNED else "  (unassigned)"
        print(f"  slot {i:2d}: {raw}   {describe_button(v)}{flag}")


# ---------------------------------------------------------------- device events (input report ID 7)
# Vendor service thread 0x410c00 does ReadFile on the 8-byte input report (ID 7) and packs data bytes 1..4 into one
# WM 0x7EB word; handler 0x46acc0 then uses them as below. Bytes 4+ of the report are never looked at by the vendor app.
EVT_ID = 7
EVT_MODE = {0x10: "USB (wired)", 0x11: "battery (wireless)"}     # same codes as the ReadPower reply (cmd 0x90, r[2])


def decode_event(rep):
    """Decode a raw input report (report ID first). Returns None if it is not a vendor event report.
    Hardware-confirmed (DPI+ presses): 07 10 03 04 17 -> stage 4, reg 0x17 = 2400 DPI; 07 10 03 07 9f -> stage 7, 16000 DPI."""
    if len(rep) != 8 or rep[0] != EVT_ID:
        return None
    mode, rate, stage, reg = rep[1], rep[2], rep[3], rep[4]
    if mode not in EVT_MODE:
        return f"vendor event type {mode:#04x} (the vendor app ignores it): {bytes(rep[1:5]).hex(' ')}"
    hz = RATE_FROM_CODE.get(rate)
    return (f"mode={EVT_MODE[mode]}  rate={hz if hz else '?'} Hz  DPI stage={stage}  DPI={dpi_from_reg(reg)} (reg {reg:#04x})")


def decode_mouse(rep):
    """7-byte report of the plain mouse node (no report ID), hardware-confirmed: buttons, X int16, Y int16, wheel int8, pad."""
    if len(rep) != 7:
        return None
    b = rep[0]
    names = [n for bit, n in ((1, "left"), (2, "right"), (4, "middle"), (8, "back"), (0x10, "forward")) if b & bit]
    x, y = struct.unpack_from("<hh", rep, 1)
    w = struct.unpack_from("b", rep, 5)[0]
    return dict(buttons=names, mask=b, x=x, y=y, wheel=w)


def watch_fds(fds, seconds, out=print, clock=time.monotonic, show_motion=False):
    """Print input reports from `fds` ({label: fd}) for `seconds` seconds. Read-only.
    Pure motion reports of the mouse node are summarised unless show_motion."""
    import select
    t0 = clock()
    n = 0
    last_mask = {}
    moved = 0
    while True:
        left = seconds - (clock() - t0)
        if left <= 0:
            break
        ready, _, _ = select.select(list(fds.values()), [], [], min(left, 0.5))
        for label, fd in fds.items():
            if fd not in ready:
                continue
            try:
                rep = os.read(fd, 256)
            except BlockingIOError:
                continue
            except OSError as e:
                out(f"{label}: read error {e}")
                return n
            n += 1
            ts = f"+{clock() - t0:7.3f}s {label:8}"
            m = decode_mouse(rep)
            if m and not show_motion:
                changed = m["mask"] != last_mask.get(label, 0)
                last_mask[label] = m["mask"]
                if not changed and not m["wheel"]:
                    moved += 1
                    continue
                if moved:
                    out(f"{ts} ({moved} motion report(s) hidden)")
                    moved = 0
                out(f"{ts} buttons={'+'.join(m['buttons']) or 'none'}" + (f"  wheel={m['wheel']:+d}" if m["wheel"] else ""))
                continue
            ev = decode_event(rep)
            out(f"{ts} len={len(rep):<3} {rep.hex(' ')}" + (f"\n{'':19}-> {ev}" if ev else ""))
    return n


def cmd_watch(a):
    nodes = [(a.node, 0)] if a.node else sysfs_nodes()
    if not nodes:
        sys.exit("no 258a:002e/002f hidraw node found")
    fds = {}
    for path, _ in nodes:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except PermissionError:
            sys.exit(f"permission denied on {path}: install 99-hator-pulsar.rules or run with sudo")
        fds[os.path.basename(path)] = fd
        cols = parse_descriptor(hid_report_descriptor(fd))
        print(f"{path}: " + "; ".join(f"page {c['page']:#06x} usage {c['usage']:#x} inputs={ {k: v for k, v in c['inputs'].items()} }"
                                      for c in cols))
    print(f"watching {len(fds)} node(s) for {a.seconds:g}s. Press mouse buttons / DPI button / turn the wheel (don't move the mouse"
          " unless you want motion reports). Ctrl+C to stop.")
    try:
        n = watch_fds(fds, a.seconds, show_motion=a.motion)
    except KeyboardInterrupt:
        n = -1
    print("done." if n < 0 else f"done, {n} report(s).")


# ---------------------------------------------------------------- CLI
def cmd_list(_):
    nodes = sysfs_nodes()
    if not nodes:
        print("no matching hidraw nodes")
    for path, pid in nodes:
        fd = os.open(path, os.O_RDWR)
        print(f"{path}  258a:{pid:04x}")
        for c in parse_descriptor(hid_report_descriptor(fd)):
            print(f"   collection usage_page={c['page']:#06x} usage={c['usage']:#x} "
                  f"feature(id:bytes)={ {k: v for k, v in c['features'].items()} } "
                  f"input(id:bytes)={ {k: v for k, v in c['inputs'].items()} }")


def cmd_info(a, dev):
    print("cmd 0x01 (query)  :", dev.cmd([1, 0, 0, 0, 0]).hex(" "))
    print("cmd 0x80 (status) :", dev.cmd([0x80, 0, 0, 0, 0]).hex(" "))
    r = dev.cmd([0x90, 0, 0, 0, 0])
    print("cmd 0x90 (power)  :", r.hex(" "))
    if r[2] in (0x10, 0x11):
        print(f"   inferred: {'USB connected/charging' if r[2] == 0x10 else 'on battery'}, "
              f"{r[3]}%, {r[4] | (r[5] << 8)} mV")
    print("active profile    :", dev.active_profile())


PROFILE_FOR_PID = {0x002E: 1, 0x002F: 2}    # vendor: FindDev sets [this+0x68] when the 2nd PID (002F, dongle) matched and Apply_24G
                                            # then demands profile 2 for it, profile 1 for 002E (0x414340 / 0x417b6a, vtable+0x2c)


def pick_profile(a, dev):
    """-p wins; otherwise follow the vendor app (profile chosen by the USB PID), falling back to the device's active profile."""
    if a.profile:
        return a.profile
    want = PROFILE_FOR_PID.get(dev.pid)
    active = dev.active_profile()
    if want is None:
        return active or 1
    if active and active != want:
        print(f"note: device reports active profile {active}, but the vendor app uses profile {want} for 258a:{dev.pid:04x}; "
              f"using {want} (override with -p).")
    return want


def cmd_dump(a, dev):
    n = pick_profile(a, dev)
    rep = dev.read_profile_raw(n)
    print(f"profile {n}: raw report (first 0x{HDR_LEN + BLOCK_LEN:x} bytes)")
    hexdump(rep[:HDR_LEN + BLOCK_LEN])
    print_decoded(block_of(rep))


def save_backup(n, rep):
    """Store the raw profile reply before a write, without overwriting an earlier backup."""
    bdir = os.path.expanduser("~/.config/pulsar-linux")
    os.makedirs(bdir, mode=0o700, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000_000:09d}"
    bfile = os.path.join(bdir, f"backup-{stamp}-p{n}.bin")
    fd = os.open(bfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(bytes(rep))
        f.flush()
        os.fsync(f.fileno())
    return bfile


def cmd_set(a, dev):
    n = pick_profile(a, dev)
    rep = dev.read_profile_raw(n)
    old = block_of(rep)
    if not markers_ok(old):
        print("refusing: profile block doesn't have the expected markers; layout assumption is wrong for this device.")
        print_decoded(old)
        sys.exit(2)
    new = bytearray(old)
    if a.cmd == "set-dpi":
        patch_dpi(new, a.stage, a.dpi)
    elif a.cmd == "set-rate":
        patch_rate(new, a.hz)
    elif a.cmd == "set-stage":
        patch_stage(new, a.n)
    elif a.cmd == "set-stages":
        try:
            patch_nstages(new, a.n)
        except ValueError as e:
            sys.exit(str(e))
    elif a.cmd == "set-color":
        patch_color(new, a.stage, a.rgb)
    elif a.cmd == "set-led":
        cols = [c.lstrip("#") for c in (a.color or [])]
        for c in cols:
            if len(c) != 6: sys.exit(f"bad colour '{c}', want RRGGBB")
        try:
            patch_led(new, led_effect_id(a.effect), a.speed, a.brightness, cols or None, a.direction)
        except ValueError as e:
            sys.exit(str(e))
    diff = [(i, old[i], new[i]) for i in range(BLOCK_LEN) if old[i] != new[i]]
    print(f"profile {n}: {len(diff)} byte(s) change: " + ", ".join(f"[{i:#04x}] {x:02x}->{y:02x}" for i, x, y in diff))
    if not a.write:
        print("dry run. add --write to apply.")
        return
    print("backup:", save_backup(n, rep))
    dev.write_profile(n, new)
    if a.reselect:
        time.sleep(0.2)
        print("select-profile ->", dev.select_profile(n))
    chk = block_of(dev.read_profile_raw(n))
    print("read-back:", "matches" if chk == new else "DIFFERS (device may normalise some fields)")
    print_decoded(chk)



def cmd_leds(a, dev):
    n = pick_profile(a, dev)
    print("effects (id: name):", ", ".join(f"{k}:{v[0]}" for k, v in LED_EFFECTS.items()))
    print(f"profile {n} current setting:")
    print_led(block_of(dev.read_profile_raw(n)))


def _backup_and_note(n, what):
    bdir = os.path.expanduser("~/.config/pulsar-linux")
    os.makedirs(bdir, exist_ok=True)
    return bdir


def cmd_buttons(a, dev):
    n = pick_profile(a, dev) if dev else (a.profile or 1)
    m, from_state = load_matrix_state(n)
    print_matrix(m, f"profile {n}: " + ("last matrix written by this tool" if from_state else
                                       "Cfg.ini DEFAULT matrix (nothing written by this tool yet; the real mouse state is unknown)"))


def cmd_dump_buttons(a, dev):
    if not a.experimental:
        sys.exit("refusing: the vendor app never reads the matrix back, so opcode (n<<4)|2 is a guess. "
                 "Pass --experimental to try (read-only, but unverified).")
    n = pick_profile(a, dev)
    rep = dev.read_buttons_experimental(n)
    print(f"profile {n}: raw reply (first 0x60 bytes)")
    hexdump(rep[:0x60])
    body = rep[HDR_LEN:HDR_LEN + 80]
    if len(body) == 80 and any(body):
        print_matrix(list(struct.unpack("<20I", body)), "decoded if this IS the matrix:")
    else:
        print("reply body is empty/zero: the device probably doesn't support reading the matrix back.")


def cmd_set_button(a, dev):
    n = pick_profile(a, dev)
    m, from_state = load_matrix_state(n)
    new = list(m)
    if a.cmd == "reset-buttons":
        new = default_matrix()
    else:
        try:
            new[a.pos - 1] = parse_button(a.func)
        except ValueError as e:
            sys.exit(str(e))
    if BTN_NAMED["left"] not in new:
        sys.exit("refusing: no slot would be left-click (the vendor app enforces this too: 'At least one button must be configured as click').")
    diff = [(i + 1, m[i], new[i]) for i in range(N_BTN) if m[i] != new[i]]
    if not from_state:
        print("note: base matrix = Cfg.ini defaults (the real matrix can't be read back). Other slots will be overwritten with those defaults.")
    print_matrix(new, f"profile {n}: matrix to send ({len(diff)} slot(s) differ from base)")
    if not a.write:
        print("dry run. add --write to apply.")
        return
    save_matrix_backup(n, m, known=from_state)
    dev.write_buttons(n, new)
    save_matrix_state(n, new)
    if a.reselect:
        time.sleep(0.2)
        print("select-profile ->", dev.select_profile(n))
    print("written. If a button misbehaves: ./pulsar.py --write reset-buttons")


def cmd_select(a, dev):
    print("active before:", dev.active_profile())
    print("active after :", dev.select_profile(a.n))


def cmd_restore(a, dev):
    rep = open(a.file, "rb").read()
    n = int(a.file.rsplit("-p", 1)[1].split(".")[0]) if "-p" in a.file else pick_profile(a, dev)
    blk = block_of(rep)
    if not markers_ok(blk):
        sys.exit("backup file doesn't look like a valid profile block")
    print_decoded(blk)
    if not a.write:
        print("dry run. add --write to apply.")
        return
    current = dev.read_profile_raw(n)
    if not markers_ok(block_of(current)):
        sys.exit("refusing: the current device profile has unexpected markers; no restore was written")
    print("backup of current profile:", save_backup(n, current))
    dev.write_profile(n, blk)
    print("restored profile", n)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--node", help="hidraw node to use (default: auto-detect)")
    p.add_argument("-p", "--profile", type=int, choices=(1, 2, 3), help="profile (default: device's active profile)")
    p.add_argument("--write", action="store_true", help="actually write (set-* and restore)")
    p.add_argument("--reselect", action="store_true", help="after a write, send the profile-select command (cmd 02)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("info")
    sub.add_parser("dump")
    s = sub.add_parser("set-dpi"); s.add_argument("stage", type=int, choices=range(1, 9)); s.add_argument("dpi", type=int)
    s = sub.add_parser("set-rate"); s.add_argument("hz", type=int, choices=sorted(CODE_FROM_RATE))
    s = sub.add_parser("set-stage"); s.add_argument("n", type=int, choices=range(1, 9))
    s = sub.add_parser("set-stages"); s.add_argument("n", type=int, choices=range(1, 9))
    s = sub.add_parser("set-color"); s.add_argument("stage", type=int, choices=range(1, 9)); s.add_argument("rgb")
    sub.add_parser("leds")
    s = sub.add_parser("set-led"); s.add_argument("effect")
    s.add_argument("--speed", type=int); s.add_argument("--brightness", type=int)
    s.add_argument("--color", action="append", help="RRGGBB, repeat for multi-colour effects")
    s.add_argument("--direction", type=int, choices=(0, 1), help="streaming effect only (block[0x3F])")
    sub.add_parser("buttons")
    s = sub.add_parser("dump-buttons"); s.add_argument("--experimental", action="store_true")
    s = sub.add_parser("set-button"); s.add_argument("pos", type=int, choices=range(1, N_BTN + 1)); s.add_argument("func")
    sub.add_parser("reset-buttons")
    s = sub.add_parser("restore"); s.add_argument("file")
    s = sub.add_parser("select-profile"); s.add_argument("n", type=int, choices=(1, 2, 3))
    s = sub.add_parser("watch"); s.add_argument("--seconds", type=float, default=30.0); s.add_argument("--motion", action="store_true", help="also print raw motion reports")
    a = p.parse_args(argv)
    if a.cmd == "list":
        return cmd_list(a)
    if a.cmd == "watch":
        return cmd_watch(a)
    if a.cmd == "buttons" and a.profile:
        return cmd_buttons(a, None)             # pure local view, no device needed
    dev = Pulsar.open(a.node)
    table = {"info": cmd_info, "dump": cmd_dump, "restore": cmd_restore, "select-profile": cmd_select,
             "leds": cmd_leds, "buttons": cmd_buttons, "dump-buttons": cmd_dump_buttons,
             "set-button": cmd_set_button, "reset-buttons": cmd_set_button}
    try:
        table.get(a.cmd, cmd_set)(a, dev)
    finally:
        dev.close()


if __name__ == "__main__":
    main()
