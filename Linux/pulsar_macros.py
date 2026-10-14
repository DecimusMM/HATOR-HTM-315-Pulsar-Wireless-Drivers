#!/usr/bin/env python3
"""
pulsar_macros.py - host-side macros for the HATOR Pulsar mouse.

How it works: a macro is bound (Buttons tab -> `macro:NAME`) to a spare trigger key, F13..F24, which is written into the
mouse's button matrix. When you press the mouse button, the mouse sends that key; this daemon sees it and plays the
macro through /dev/uinput. Nothing is stored on the mouse itself, so the daemon must be running.

Script format, one step per line (lines beginning with # are comments):
    key ctrl+shift+esc     press the combo, then release it
    down shift             hold a key
    up shift               release a key
    type Hello World       type text (US layout)
    wait 100               pause in milliseconds
    exec notify-send hi    run a Bash command in the background
    exec-wait command      run a Bash command and wait for it to finish
Commands run as your logged-in user via /bin/bash -lc. Treat macro commands like terminal commands.

Run:  python3 pulsar_macros.py            (needs python-evdev: sudo pacman -S python-evdev)
      python3 pulsar_macros.py --all      (listen on every keyboard device, not just 258a:*)
      python3 pulsar_macros.py --verbose  (log input event names; disabled by default for privacy)
Needs read access to /dev/input/event* and write access to /dev/uinput. Macro `exec` steps run shell commands as the current user.
"""
import json, os, select, sys, threading, time, subprocess, tempfile

CFG = os.path.join(os.path.expanduser("~"), ".config", "pulsar-linux", "macros.json")
TRIGGERS = [f"f{n}" for n in range(13, 25)]

MODS = {"ctrl": "LEFTCTRL", "control": "LEFTCTRL", "shift": "LEFTSHIFT", "alt": "LEFTALT", "altgr": "RIGHTALT",
        "super": "LEFTMETA", "meta": "LEFTMETA", "win": "LEFTMETA"}
NAMED = {"esc": "ESC", "enter": "ENTER", "return": "ENTER", "space": "SPACE", "tab": "TAB", "backspace": "BACKSPACE",
         "del": "DELETE", "delete": "DELETE", "insert": "INSERT", "home": "HOME", "end": "END", "pageup": "PAGEUP",
         "pagedown": "PAGEDOWN", "up": "UP", "down": "DOWN", "left": "LEFT", "right": "RIGHT", "capslock": "CAPSLOCK",
         "minus": "MINUS", "equal": "EQUAL", "comma": "COMMA", "dot": "DOT", "slash": "SLASH", "semicolon": "SEMICOLON",
         "print": "SYSRQ", "menu": "COMPOSE"}

# US-layout characters for `type`: char -> (key name, needs shift)
CHARS = {" ": ("SPACE", False), "\t": ("TAB", False), "\n": ("ENTER", False),
         "-": ("MINUS", False), "_": ("MINUS", True), "=": ("EQUAL", False), "+": ("EQUAL", True),
         ",": ("COMMA", False), "<": ("COMMA", True), ".": ("DOT", False), ">": ("DOT", True),
         "/": ("SLASH", False), "?": ("SLASH", True), ";": ("SEMICOLON", False), ":": ("SEMICOLON", True),
         "'": ("APOSTROPHE", False), '"': ("APOSTROPHE", True), "[": ("LEFTBRACE", False), "{": ("LEFTBRACE", True),
         "]": ("RIGHTBRACE", False), "}": ("RIGHTBRACE", True), "\\": ("BACKSLASH", False), "|": ("BACKSLASH", True),
         "`": ("GRAVE", False), "~": ("GRAVE", True)}
for _c, _s in zip("1234567890", "!@#$%^&*()"):
    CHARS[_c] = (_c, False)
    CHARS[_s] = (_c, True)


# ---------------------------------------------------------------- storage
def load():
    """Load only structurally valid macro records; a damaged config fails closed."""
    try:
        with open(CFG, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    result, used = {}, set()
    for name, spec in data.items():
        if (not isinstance(name, str) or not isinstance(spec, dict)
                or spec.get("trigger") not in TRIGGERS or spec.get("trigger") in used
                or not isinstance(spec.get("script"), str)):
            continue
        try:
            repeat = int(spec.get("repeat", 1))
        except (TypeError, ValueError):
            repeat = 1
        result[name] = {"trigger": spec["trigger"], "script": spec["script"],
                        "repeat": max(1, min(repeat, 9999)), "hold": bool(spec.get("hold", False))}
        used.add(spec["trigger"])
    return result


def save(macros):
    if not isinstance(macros, dict):
        raise ValueError("macro storage must be a dictionary")
    directory = os.path.dirname(CFG)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="macros-", suffix=".tmp", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(macros, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CFG)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def free_trigger(macros):
    used = {m["trigger"] for m in macros.values()}
    for t in TRIGGERS:
        if t not in used:
            return t
    raise ValueError("no free trigger keys left (F13-F24 are all used)")


# ---------------------------------------------------------------- parsing
def key_name(tok):
    """'ctrl' -> 'LEFTCTRL', 'a' -> 'A', 'f5' -> 'F5'. Raises ValueError for unknown keys."""
    t = tok.strip().lower()
    if t in MODS:
        return MODS[t]
    if t in NAMED:
        return NAMED[t]
    if len(t) == 1 and t.isalnum():
        return t.upper()
    if t.startswith("f") and t[1:].isdigit() and 1 <= int(t[1:]) <= 24:
        return t.upper()
    raise ValueError(f"unknown key '{tok}'")


def parse_script(text):
    """Validate a macro and return executable steps; shell commands remain explicitly user-authored."""
    if not isinstance(text, str):
        raise ValueError("macro script must be text")
    if len(text) > 64_000:
        raise ValueError("macro script is too large (maximum 64,000 characters)")
    steps = []
    for n, raw in enumerate(text.splitlines(), 1):
        if n > 1000:
            raise ValueError("macro script has too many lines (maximum 1000)")
        # Preserve # inside shell commands; only whole-line comments are comments.
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        op, _, arg = line.partition(" ")
        op, arg = op.lower(), arg.strip()
        try:
            if op == "key":
                steps.append(("tap", [key_name(k) for k in arg.split("+")]))
            elif op in ("down", "up"):
                steps.append((op, key_name(arg)))
            elif op == "type":
                bad = [c for c in arg if not (c.isalnum() and c.isascii()) and c not in CHARS]
                if bad:
                    raise ValueError(f"cannot type {bad[0]!r}")
                steps.append(("type", arg))
            elif op == "wait":
                milliseconds = int(arg)
                if not 0 <= milliseconds <= 600_000:
                    raise ValueError("wait must be between 0 and 600000 milliseconds")
                steps.append(("wait", milliseconds))
            elif op in ("exec", "exec-wait"):
                if not arg:
                    raise ValueError(f"{op} requires a shell command")
                steps.append((op, arg))
            else:
                raise ValueError(f"unknown step '{op}'")
        except ValueError as e:
            raise ValueError(f"line {n}: {e}")
    if not steps:
        raise ValueError("macro is empty")
    return steps


# ---------------------------------------------------------------- playback (needs evdev)
class Player:
    def __init__(self, ui, ecodes):
        self.ui, self.e = ui, ecodes
        self.held = set()

    def _code(self, name):
        return self.e.ecodes["KEY_" + name]

    def _key(self, name, value):
        self.ui.write(self.e.EV_KEY, self._code(name), value)
        self.ui.syn()
        (self.held.add if value else self.held.discard)(name)

    def _tap(self, names):
        for n in names:
            self._key(n, 1)
        time.sleep(0.01)
        for n in reversed(names):
            self._key(n, 0)
        time.sleep(0.01)

    def play(self, steps, stop):
        try:
            for op, arg in steps:
                if stop.is_set():
                    break
                if op == "tap":
                    self._tap(arg)
                elif op == "down":
                    self._key(arg, 1)
                elif op == "up":
                    self._key(arg, 0)
                elif op == "wait":
                    stop.wait(arg / 1000)
                elif op == "type":
                    for ch in arg:
                        name, shift = CHARS.get(ch, (ch.upper(), ch.isupper()))
                        self._tap(["LEFTSHIFT", name] if (shift or ch.isupper()) else [name])
                elif op == "exec":
                    # Fire-and-forget: don't block the input listener or macro playback.
                    subprocess.Popen(
                        ["/bin/bash", "-lc", arg],
                        stdin=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                elif op == "exec-wait":
                    # Wait for completion before continuing to the next macro step.
                    subprocess.run(
                        ["/bin/bash", "-lc", arg],
                        stdin=subprocess.DEVNULL,
                        check=False,
                    )
        finally:
            for n in list(self.held):       # never leave a key stuck
                self._key(n, 0)


def run_daemon(argv):
    try:
        from evdev import InputDevice, UInput, ecodes, list_devices
    except ImportError:
        sys.exit("python-evdev missing: install python-evdev (Arch: sudo pacman -S python-evdev)")
    listen_all = "--all" in argv
    verbose = "--verbose" in argv
    devs = []
    for path in list_devices():
        try:
            device = InputDevice(path)
            if ecodes.EV_KEY in device.capabilities() and (listen_all or device.info.vendor == 0x258A):
                devs.append(device)
            else:
                device.close()
        except (OSError, PermissionError):
            continue
    if not devs:
        sys.exit("no readable keyboard-capable input devices found (vendor 258a). Check /dev/input permissions, or use --all")
    try:
        ui = UInput()
    except OSError as exc:
        for device in devs:
            device.close()
        sys.exit(f"cannot open /dev/uinput ({exc}). Configure input/uinput access for your user; avoid running the whole GUI as root.")

    player = Player(ui, ecodes)
    running = {}            # trigger -> (thread, stop event)
    stamp, macros = 0, {}
    print("listening on:", ", ".join(f"{d.name} ({d.path})" for d in devs), flush=True)
    if listen_all:
        print("privacy note: --all can observe key events on all selected devices; use only on a trusted machine", flush=True)

    def reload_macros():
        nonlocal stamp, macros
        try:
            stat = os.stat(CFG)
            current_stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            current_stamp = None
        if current_stamp is None:
            if stamp != 0:
                stamp, macros = 0, {}
                print("macros: (config file missing; cleared)", flush=True)
            return
        if current_stamp != stamp:
            stamp = current_stamp
            macros = {}
            for name, spec in load().items():
                try:
                    macros[spec["trigger"]] = (name, spec, parse_script(spec["script"]))
                except (ValueError, KeyError, TypeError) as exc:
                    print(f"skipping macro {name}: {exc}", flush=True)
            print("macros:", ", ".join(f"{t}={v[0]}" for t, v in sorted(macros.items())) or "(none)", flush=True)

    def worker(spec, steps, stop):
        count = 0
        while not stop.is_set():
            player.play(steps, stop)
            count += 1
            if not spec.get("hold") and count >= int(spec.get("repeat", 1)):
                break

    by_fd = {device.fd: device for device in devs}
    try:
        while True:
            reload_macros()
            readable, _, _ = select.select(list(by_fd), [], [], 1.0)
            for fd in readable:
                device = by_fd[fd]
                try:
                    events = device.read()
                except OSError as exc:
                    print(f"input device disconnected ({device.path}): {exc}; stopping listener", flush=True)
                    return
                for event in events:
                    if event.type != ecodes.EV_KEY or event.value == 2:
                        continue
                    name = ecodes.KEY.get(event.code)
                    if isinstance(name, list):
                        name = name[0]
                    trigger = (name or "").replace("KEY_", "").lower()
                    if verbose:
                        print(f"event: {trigger or event.code} value={event.value} device={device.path}", flush=True)
                    if trigger not in macros:
                        continue
                    macro_name, spec, steps = macros[trigger]
                    if event.value == 1:
                        if trigger in running and running[trigger][0].is_alive():
                            continue
                        stop = threading.Event()
                        thread = threading.Thread(target=worker, args=(spec, steps, stop), daemon=True)
                        running[trigger] = (thread, stop, bool(spec.get("hold")))
                        thread.start()
                        if verbose:
                            print(f"started macro {macro_name}", flush=True)
                    elif event.value == 0 and trigger in running and running[trigger][2]:
                        running[trigger][1].set()
    finally:
        for _thread, stop, _hold in running.values():
            stop.set()
        for _thread, _stop, _hold in running.values():
            if _thread.is_alive():
                _thread.join(timeout=1.0)
        for key in list(player.held):
            try:
                player._key(key, 0)
            except Exception:
                pass
        try:
            ui.close()
        except Exception:
            pass
        for device in devs:
            try:
                device.close()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        run_daemon(sys.argv[1:])
    except KeyboardInterrupt:
        pass
