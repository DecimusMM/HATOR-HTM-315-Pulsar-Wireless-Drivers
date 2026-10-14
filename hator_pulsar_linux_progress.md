# HATOR Pulsar Wireless: Linux port, progress notes

Date: 2026-10-13
Target: `HATOR_Pulsar_Wireless_Setup_v1_0_6.exe` (Windows config tool) -> native Arch Linux tool

## Goal
Replace the Windows-only config app with something that runs on Arch (hidraw + `hidapi`, optional GUI, PKGBUILD).
A byte-for-byte "rebuild" of the source is not possible (compiled C++/MFC), but the **HID protocol can be recovered** and re-implemented.

## 1. Installer
- Inno Setup 5.3.3 (unicode). Extracted with `innoextract` (`apt/pacman: innoextract`).
- Useful files in `app/`:
  - `OemDrv.exe` (2.5 MB, 32-bit MFC app, RTTI intact)
  - `Lowerdev.dll` (57 KB, thin HID wrapper)
  - `Cfg.ini` (device + UI config)
  - `text.xml`, `skins/*.png`

## 2. Device facts from `Cfg.ini`
| Key | Value |
|---|---|
| Title | G61 Pulsar Wireless |
| appdir | BY-COMBO3 |
| VID / PID / PID2 | 0x258A / 0x002E / 0x002F |
| Sensor | 0x3335 (PAW3335) |
| DPI stages (7) | 500, 1000, 1500, 2400, 4000, 8000, 16000 |
| Default level | 2 (DEFLEVEL) |
| Stage LED colours | `DC=` list of 7 RGB triples |
| LEDParam | `0,2,4,4,0,0x3F` |
| MacProtocol | 2 |
| GoWithNoDev | 0 (app exits if no device is found) |
| Key table | `K1_1..K10_1` = `type,code,0x00,index` (meaning not decoded yet) |

**Caveat:** the Wine log below shows a device `320F:5055`, not `258A:002E`. See section 5.

## 3. Binary structure (OemDrv.exe, image base 0x400000)
- `fcn.00410e20` (`CHidDrv::virtual_12`): `LoadLibraryW(L"Lowerdev.dll")`, then `GetProcAddress` for the exports below. Fails with "load dll failed." if any is missing.
- Object layout filled by that function:
  - `+0x04` HMODULE
  - `+0x08` OpenHidDevice
  - `+0x0C` SetFeature
  - `+0x10` GetFeature
  - `+0x14` GetInputReport
  - `+0x18` GetProductString
- `Lowerdev.dll` exports (RVA): FindHidDevice 0x1010, OpenHidDevice 0x1170, SetFeature 0x14f0, SetOutputReport 0x1530, GetFeature 0x1510, GetInputReport 0x1550, GetProductString 0x1570, GetProductID 0x1590. Built on `hid.dll` (`HidD_SetFeature`, `HidD_GetFeature`, `HidD_SetOutputReport`, `HidD_GetInputReport`). No obfuscation seen.
- `0x00410c00`, ServiceThread: loops `ReadFile` on the HID handle (overlapped). Report ID is taken from `[this+0x14]`. First 4 data bytes are packed into one DWORD and sent with `PostMessageW(hwnd, 0x7EB, value, 0)`. This is the device-to-host event path (DPI button, battery, etc.).
- Config parsers (not senders): `0x0047b2c0` (DPIH, DPIHW, MDNUM, DEFLEVEL), `0x0047c0a0` (PopByte1), `0x0047e310` (LEDParam).
- Other driver classes (`CDrv816`, `CDrv901`, `CDrv8948`, `CDrvPad`) look like older chip families; `CDrv8948` uses overlapped CreateFile I/O and is probably not ours.
- Useful strings: `OnDeviceBehavior: Buffer= %x %x %x %x`, `Writing Mode %x (PID=%04X)`, `ReadPower: nPower=%d, bUsbCennected=%d`, `SetUsbRateOnReset`, `StMacro_To_HdMacro`.

## 4. Not found yet
- Byte layout of the **write** commands (DPI, polling rate, button remap, LED, macros).
- Report IDs and lengths used with `SetFeature`/`GetFeature`.
- Dead end: searching raw bytes for `call [reg+0xc]` mostly hits ordinary MFC vtable calls. Better: trace at runtime, or follow callers of the CHidDev/CHidDrv wrapper methods.

## 5. Wine attempt (user log)
```
warn:hid:is_hidraw_enabled Ignoring unsupported 320F:5055 hidraw mouse/keyboard
warn:hid:bus_main_thread ignoring hidraw device 320f:5055 with usages 0001:0006
Application could not be started, or no application associated with the specified file.
ShellExecuteEx failed: File not found.
```
Findings:
1. Wine's `winebus` deliberately ignores hidraw interfaces whose top-level usage is Generic Desktop **Mouse (0001:0002)** or **Keyboard (0001:0006)**. This interface reports `0001:0006`, so Wine never creates a HID device for it and `OemDrv.exe` can't open it. Config channels on these mice are often on a keyboard-usage interface. This is Wine behaviour, not an app bug.
2. The device is `320F:5055`, **not** `258A:002E` from `Cfg.ini`. Either the hardware (or its dongle) differs from the config in this installer, or the log is from another device. Needs `lsusb` to confirm.
3. With no matching device and `GoWithNoDev=0`, the app exits. The `ShellExecuteEx ... File not found` message is most likely a follow-up from that exit path (or from `wine` being given a path it couldn't resolve); it is not the root cause.

## 6. Next steps
1. Identify the real hardware:
   ```
   lsusb | grep -iE "320f|258a"
   sudo lsusb -v -d 320f:5055 | less
   sudo pacman -S hid-tools && sudo hid-recorder /dev/hidrawN   # each node
   ```
   Look for an interface with vendor usage page 0xFF00 or feature reports.
2. Capture real traffic (Wine can't see the device): Windows VM (virt-manager) with USB passthrough running the vendor app, and on the host:
   ```
   sudo modprobe usbmon && sudo wireshark   # capture usbmonN, filter on the device
   ```
   Change one setting at a time (DPI stage, polling rate, a button, LED colour) and note timestamps.
3. Map captures to report bytes, then write `hidapi` Python script + GUI + PKGBUILD.
4. Optional: patch Wine's `is_hidraw_enabled` in `dlls/winebus.sys/bus_udev.c` to allow this VID/PID, only as a stopgap.

---

## 7. Update: protocol framing recovered (static analysis, CDrv816 family)

All addresses are in `OemDrv.exe` (image base 0x400000) unless noted. Which driver class this exact mouse uses (CDrv816 vs CDrv901 etc.) is **not yet confirmed**; CDrv816 is the one analysed so far.

### Finding the device (`CDrv816::FindDev`, ~0x411660)
Calls `OpenHidDevice(usagePage=0xFF00, usage=1, L"VID_%04X&PID_%04X", predicate, ..., access=0x40000000)` **twice**, with two predicates:

| Predicate | Condition | Stored at | Role |
|---|---|---|---|
| 0x4115c0 | FeatureReportByteLength == 0x408 (1032) | `this+0x1C` | bulk data channel |
| 0x4115d0 | FeatureReportByteLength == 6 | `this+0x20` | command channel |

Predicate signature (from `Lowerdev.dll` 0x10001280, cdecl): `pred(deviceIdx, InputLen, OutputLen, FeatureLen)`.
`Lowerdev.dll` enumerates the HID class with SetupDi, filters the device path with `StrStrIW("VID_xxxx&PID_xxxx")`, opens it with `CreateFileW`, then checks `HidP_GetCaps` UsagePage/Usage and calls the predicate.

Consequence for Linux: target the hidraw node(s) with **vendor usage page 0xFF00, usage 1**. Use feature-report ioctls (`HIDIOCSFEATURE` / `HIDIOCGFEATURE`) on them. Wine does not hide vendor-usage interfaces, only mouse/keyboard-usage ones.

### Transport helpers (all retry 3x with Sleep(200 ms))
- `0x411840` SetData: report **ID 6** + payload (up to 1031 bytes), `SetFeature(handle=+0x1C, buf, len)`.
- `0x4117c0` GetData: report **ID 6**, `GetFeature` on the same handle.
- Command packets: report **ID 5**, short (observed lengths 7 and 9 bytes), `SetFeature(handle=+0x20, ...)`; a 20 ms sleep follows before reading data back.

### Operations seen so far (semantics inferred, not verified)
| Function | What it does |
|---|---|
| 0x411a90 | READ: sends ID-5 command, waits 20 ms, GetData (ID 6, 1031 B), checks first byte == **0x83**, copies **136 bytes (0x22 dwords)** to the caller. Looks like "read config block". |
| 0x411990 | WRITE: builds an ID-6 payload (header byte 3 + 136-byte block), SetFeature length 0x408. Looks like "write config block". |
| 0x411bc0 | Header `09 00 00 40 ...`, copies **0x318 (792) bytes**, sends via SetData. Candidate: button/macro table. |
| 0x411c70 | Header `04 C8 00 40 00 00 00` + **1024 bytes** copied from caller, sends via SetData. Candidate: bulk/flash block write. |
| 0x411eb0 / 0x411f96 | Header byte 8 (and 5 ...) + 0x400-byte buffers via SetData. |
| 0x4122xx-0x4127xx | Long run of tiny `ret 4` stubs (per-feature getters/setters in the device class). **Next place to read** to map DPI, polling rate, LED, button fields to offsets inside the config block. |

### Still unknown
- Exact byte offsets inside the 136-byte config block (DPI stages, polling rate, LED mode/colour, lift-off, debounce, battery).
- Exact ID-5 command byte meanings (opcode values and arguments).
- Whether this hardware (see section 5, `320F:5055`) matches this driver family at all.

### How to verify quickly on real hardware (no Windows needed)
```
sudo hid-recorder /dev/hidrawN       # find the node with usage page 0xFF00
python3 - <<'PY'
import fcntl, os, struct
# HIDIOCGFEATURE(len) = _IOC(3, 'H', 0x07, len)
def gf(fd, rid, n):
    buf = bytearray(n); buf[0] = rid
    req = (3 << 30) | (n << 16) | (ord('H') << 8) | 0x07
    fcntl.ioctl(fd, req, buf, True); return bytes(buf)
fd = os.open("/dev/hidrawN", os.O_RDWR)
print(gf(fd, 6, 1032).hex())
PY
```
(Read-only probe: reading report 6 does not change settings. Do not send writes until the layout is confirmed; a bad config block can leave the mouse in a odd state until reset.)

---

## 8. Update: hardware confirmed (corrects sections 5 and 7)

User's `lsusb` output:
```
Bus 001 Device 002: ID 258a:002e SINOWEALTH Hator Pulsar
Bus 001 Device 009: ID 320f:5055 Telink USB Gaming Keyboard
```

Corrections:
- The mouse is **258A:002E**, which matches `Cfg.ini` (`VID=0x258A`, `PID=0x002E`). The earlier worry that the installer targets different hardware is resolved: it does match.
- `320F:5055` in the Wine log is a **separate Telink keyboard**, not the mouse. The Wine warnings (`Ignoring unsupported 320F:5055 hidraw mouse/keyboard`, usages `0001:0006`) were about that keyboard and are unrelated to the mouse.
- So the Wine log shows nothing yet about whether Wine can see the mouse's vendor interface (usage page 0xFF00). Whether `OemDrv.exe` exited because it found no usable device is still unverified.
- The caveat in section 7 about the hardware not matching can be dropped. Still unconfirmed: that the mouse uses the `CDrv816` driver class specifically.

Still open:
- Byte offsets inside the 136-byte config block (DPI, polling rate, LED, etc.).
- Meaning of the ID-5 command bytes.
- Next code to read: the getter/setter stubs around 0x4122xx-0x4127xx in `OemDrv.exe`.
- Optional read-only hardware probe (section 7) not run yet.

---

## 9. Update: CORRECTION to section 7. The mouse uses `CDrv8948`, not `CDrv816`

Driver class is selected from three `[OPT]` keys in `Cfg.ini` (`GetPrivateProfileInt`, read at 0x47fe10-0x47fe50):

| Key | Global | Class created (factory 0x4639a0) |
|---|---|---|
| `MsFw`  | `[0x656b68]` | **CDev8948 + CDrv8948** (any non-zero value) |
| `KbFw`  | `[0x656b6c]` | CDev816 + CDrv816 (when == 0x14), i.e. keyboards |
| `PadFw` | `[0x656b70]` | CDevPad + CDrvPad (when == 0x28) |

Your `Cfg.ini` has `MsFw=0x005` and no `KbFw`/`PadFw`, so everything in section 7 (ID 6, 0x408, `09 00 00 40` ...) is the **keyboard** path and does not apply to this mouse.
`MsFw & 0xF` (= 5) selects the report ID of the data channel: 4 -> ID 6, 5 -> **ID 8**, otherwise ID 4. The tool's `0x624dc0/c4/c8` constants (7, 5, 0x207) are set in the CDrv8948 ctor (0x414120).

## 10. Transport (CDrv8948, all verified by reading the code; hardware untested)

`CDrv8948::FindDev` (0x414340 / 0x4143f0) opens **three** vendor interfaces (usage page 0xFF00, usage 1, `VID_%04x&PID_%04x`), distinguished by predicates on the HID caps:

| Handle | Predicate | Meaning |
|---|---|---|
| hCmd (`this+0x50`) | FeatureReportByteLength == 8 | command channel |
| hData (`this+0x54`) | FeatureReportByteLength == 0x208 (520) | bulk data channel |
| hNotify (`this+0x58`) | InputReportByteLength == 8 | device -> host events |

NB: Windows exposes each top-level collection as a separate device; Linux hidraw exposes one node per USB interface, so on Linux the same reports may all be on one node. `pulsar.py list` shows the real layout.

Primitives (all retry 3x, `Sleep(200)` between tries; a 20 ms wait (`this+0x60`; 40 ms if `MsFw&0xF==1`) is used between a command and reading its result):

* **SendCmd** 0x414bd0: `SetFeature(hCmd, 05 c0 c1 c2 c3 c4 00 00)`  (report ID 5, 8 bytes)
* **GetCmd** 0x414cd0: `GetFeature(hCmd, 05 .. 8 bytes)`; returns bytes 1..5 of the reply
* **SendData** 0x414de0: `SetFeature(hData, 520 bytes)` = `[ID=8][518-byte payload][00]`
* **GetData** 0x414ec0: `GetFeature(hData, 520)`; the caller gets report bytes **8..507**

Write framing (0x4152b0, 0x415720): `08 | cmd | 00 | len | 00 00 00 00 | payload`, so the payload starts at report offset 8, the same offset reads return it at.

## 11. Commands

| Cmd bytes (c0..c4) | Function | Notes |
|---|---|---|
| `01 00 00 00 00` | query | 4-byte reply (0x414fc0), called at startup; likely version/ID. Meaning not confirmed |
| `02 00 ..` | get active profile | reply byte 2 (`r[2]`) must be 1..3 (0x415090) |
| `02 N+1 ..` | set active profile | 0x415150 (vtable slot, no direct callers seen) |
| `11` / `21` / `31` | **read general block** of profile 1/2/3 | then `GetData` (0x4151b0) |
| `51` / `52` | read 10 bytes | 0x415580, unknown purpose |
| `80 ..` | status | reply `r[2]==1 && r[3]==1` -> flag (0x415390), meaning unknown |
| `90 ..` | **ReadPower** | reply `r[2]`: 0x10 = USB connected, 0x11 = battery; `r[3]` = percent; `r[4] | r[5]<<8` = millivolts (0x415460) |
| `1A xx ..` / `1B xx ..` | LOD related | 0x4156d0 / SetLOD2 0x415680 |

Data-channel writes (payload via SendData): cmd byte `11/21/31` = general block, length **0x92**; cmd byte `12/22/32` = button matrix, length **0x50 (80)**, last payload byte forced to `0xA5`; macros via 0x415810 (not decoded).
The vendor app's `Apply_24G` (0x417a30) does **read-modify-write**: read profile -> overwrite fields -> `WriteProfile` -> `Sleep(50)`. It never builds the block from scratch, so unknown bytes (e.g. `block[1]`, a serial/version counter that must be non-zero) are preserved. The tool does the same.

## 12. The 146-byte "general" profile block (packers at 0x419530 and 0x4198d0)

| Offset | Content | Confidence |
|---|---|---|
| 0x00 | constant `0x64` | high (written by packer) |
| 0x01 | serial/version, preserved from device, app warns if 0 | medium |
| 0x02 low nibble | polling rate: 1=125, 2=250, 3=500, 4=1000 Hz | high |
| 0x02 bit 7 | "X/Y DPI independent" flag (gates Y array) | medium |
| 0x03 | `(current_stage << 4) \| number_of_active_stages`, stages 1-based | high |
| 0x05..0x14 | 8 x uint16 LE DPI **X** registers | high |
| 0x15..0x24 | 8 x uint16 LE DPI **Y** registers (zero unless independent) | high |
| 0x25..0x3C | 8 x RGB stage-indicator colours (R,G,B order; `RgbSW=0`) | high |
| 0x3D | current LED effect id (table at 0x652260) | medium |
| 0x3E..0x85 | per-effect parameters (speed/brightness nibbles + colour tables), effects 1-10 | partly decoded (see 0x4198d0) |
| 0x8B | value from config `+0x28a4` (LED related) | low |
| 0x90 | constant `0xA5` end marker | high |
| 0x91 | flag bits (bit 7 and bit 2 set from UI options) | low |

**DPI encoding** (built-in sensor table, builder at 0x4770e0): for sensor `0x3335` the register is the *index* in a 100-CPI-step table 100..16000, i.e. `reg = dpi/100 - 1` (500 -> 4, 1000 -> 9, 1500 -> 14, 2400 -> 23, 4000 -> 39, 8000 -> 79, 16000 -> 159). This replaces the dead-end in section 4; the `DPIRANGE/DPISET/DPIHW` ini keys are only a fallback.

Profile selection: in `FindDev`, `this+0x68` is set when the *second* PID in the config (`PID2=0x2F`) matched; in "single mode" the app then uses profile **1 for 258a:002e and 2 for 258a:002f**. Not confirmed which applies to this device; the tool uses the device's active profile (cmd `02`) unless `-p` is given.

## 13. What exists now (files alongside this note)

* `pulsar.py`: stdlib-only CLI (`list`, `info`, `dump`, `set-dpi`, `set-rate`, `set-stage`, `set-color`, `restore`). Read-only unless `--write`; writes are RMW, backed up under `~/.config/pulsar-linux/`, and refused unless markers `b[0]==0x64`, `b[0x90]==0xA5`, `b[1]!=0`, `b[3]!=0` are present.
* `99-hator-pulsar.rules`: udev `uaccess` rule (`sudo cp` to `/etc/udev/rules.d/`, then `sudo udevadm control --reload && sudo udevadm trigger`).
* Verified with a mock device that implements exactly the framing above; **not yet run on real hardware**.

## 14. Next steps

1. Run `./pulsar.py list`, `./pulsar.py info`, `./pulsar.py dump` and paste the output. `dump` is the decisive test: if `b[0]=0x64`, `b[0x90]=0xA5` and the 7 default stages decode to 500/1000/1500/2400/4000/8000/16000 Hz-worth of DPI, the layout is confirmed and writes become reasonable.
2. If `list` shows the node carries no feature ID 5/8, check `sudo hid-recorder /dev/hidrawN` for the interface layout and report back.
3. Then decode: button matrix (`12/22/32`, 80 bytes; `K1_1..K10_1` in Cfg.ini are `type,code,0,index` per button), macros (0x415810), LED effects (rest of 0x4198d0), LOD (`1A/1B`), debounce/sleep if present, and the notify channel (0x410c00 posts 4 data bytes as `WM 0x7EB`).
4. GUI and PKGBUILD only after step 1 confirms the protocol.

---

## 15. Hardware results (2026-10-13, user's mouse, 258a:002e, hidraw1)

**Confirmed on real hardware**

* `list`: one hidraw node (`/dev/hidraw1`) carries every vendor report: feature ID 5 (8 B), feature ID 8 (520 B), input ID 7 (8 B), plus an unexplored `0xFF01` collection with feature ID 6 (8 B). hidraw0 is the plain mouse interface. No Windows-style splitting needed.
* `info`: cmd `01` reply = `05 01 32 36 31 30 ..` (ASCII "2610", probably firmware/version). Cmd `90` = `05 90 10 02 ..`: USB connected, 2 % battery; the millivolt field read 0 (inferred bytes wrong or not reported over USB). Cmd `80` echoed the cmd-01 reply (stale/unknown). Active profile (cmd `02`) = 1.
* `dump -p 1` (profile 1): markers `b[0]=0x64`, `b[0x90]=0xA5` present; stock stages decode to 1500/2400/4000/8000/16000, stage colours match `DC=` in Cfg.ini; polling 500 Hz, 7 stages. **Section 12 layout and the `reg = dpi/100 - 1` encoding are confirmed.**
* Read-modify-write works: `set-dpi 1 500` -> `300` changed exactly one byte (`[0x05]`), read-back matched.
* **Write + profile reselect changes the live DPI.** After `--write --reselect set-dpi 1 3200` and `set-stage 1` the cursor became very fast. So the sequence `write profile block (cmd 0x11)` then `cmd 02 <n>` (select profile) applies settings.
* Caveat: the earlier write without `--reselect` was inconclusive, because the test changed stage 1 while the mouse was on stage 2. It is **not yet proven** that `--reselect` is required; the vendor app's `Apply_24G` sends no explicit apply command (only a 1.5 s `Sleep` at the end). Test: write to the *current* stage without `--reselect`.

**Not working / unexplained**

* `set-color 1 FF8800` (stage-indicator colour, block bytes `0x25..0x3C`) produced **no orange light**. Candidates: (a) block[0x3D] = 0x07 (current LED effect) means the lighting mode doesn't show the DPI-stage colours, (b) the DPI-indicator LED uses a different byte range or effect parameters at `0x3E..0x85`, (c) the colour only displays briefly on a stage change. The stage-colour table in Cfg.ini (`DC=`) is for the *software UI* too, so the physical LED may not use it at all.

**State left on the mouse (restore if wanted)**

* Profile 1: stage 1 = 3200 DPI, stage-1 colour bytes = FF8800, current stage = 1.
* Backups of the pre-write profile: `/root/.config/pulsar-linux/backup-*-p1.bin` (the tool ran under sudo). Restore with `sudo ./pulsar.py --write restore <file>`. The earliest backup holds the original stock-ish values (stage 1 = 200, stage 2 = 400).

## 16. Tool changes

* `pulsar.py` now has `select-profile N` (sends `02 N 00 00 00`) and `--reselect` (after `--write`, sends the profile-select command). Wrapper script must forward arguments: `exec python3 pulsar.py "$@"`.

## 17. Next steps

1. Re-test without `--reselect` on the current stage to see if it is needed.
2. LED: decode the rest of `0x4198d0` (effects 1-10 at `0x3E..0x85`) and try changing `block[0x3D]`; compare against what the physical LED does.
3. Button matrix (cmd `12/22/32`, 80 bytes, last byte `0xA5`), macros (0x415810), LOD (`1A/1B`), debounce (`0x4156d0` -> cmd `1A`).
4. Battery voltage bytes and cmd `80`; event channel (input ID 7); the `0xFF01` collection (feature ID 6).
5. GUI and PKGBUILD.

---

## 18. Update (2026-10-14): LED effects, button matrix, validation by emulation

Method change: instead of transcribing jump tables by hand, the vendor functions were run under Unicorn
(PE image mapped, stubs for lock/log helpers) and their outputs compared with the Python codecs.

### LED (packer 0x4198d0; effect-name strings from text.xml `tc_ms_led1..10`)
`block[0x3D]` = effect id. Each effect owns a fixed slice; ctl byte = `(brightness<<4)|speed` (nibble roles inferred:
Steady/Colorful-Steady write only the high nibble, Tail/Off only the low one).

| id | effect | ctl | colours |
|---|---|---|---|
| 1 | Colorful Streaming | 0x3E (+0x3F direction bit = UI value==3) | - |
| 2 | Steady | 0x40 (high nibble only) | 1 @0x41 |
| 3 | Breathing | 0x44 | count @0x45, list @0x46 (<=7, 21 B zeroed first) |
| 4 | Colorful Tail | 0x5B (low nibble only) | - |
| 5 | Neon | 0x5C | - |
| 6 | Colorful Steady | 0x5D (high nibble only) | list @0x5E (30 B zeroed first) |
| 7 | Flicker | 0x7C | 2 @0x7D |
| 8 | Stars twinkle | 0x83 | - |
| 9 | Wave | 0x84 | - |
| 10 | LED OFF | 0x85 (low nibble only) | - |

Other nibble is preserved on write. Tail of block: `0x8B` = index, `0x8C` = param, `0x8D..0x8F` = RGB from a
12-byte-stride list at cfg+0x28A8 (an indicator-type setting, undecoded).
Explains section 15: mouse was on effect 7 (Flicker), which ignores the stage colours at 0x25..0x3C.
Validation: 300 randomised trials, `patch_led` == vendor packer on bytes 0x3D..0x85. Colour byte order assumed RGB.

### Button matrix (encoder = CDev8948 vtable+0x10 = 0x416580; builder 0x4176f0; writer 0x415720)
20 slots x LE dword. Slot = 4th field of Cfg.ini `K<n>_1`; unused slots init to 0x150.
Cfg `K` entries: entry+0 = type<<24|code, entry+0x14 = slot.

| dword bytes | meaning |
|---|---|
| `11 m 00 00` | mouse button, m = HID mask (1 L, 2 R, 4 M, 8 back, 0x10 fwd) |
| `12 01` / `12 ff` | scroll up / down |
| `31 01 32 N` | left click x N (double=2, triple=3) |
| `21 mods k1 k2` | keyboard; HID usages (vendor VK->HID helper 0x489090), HID modifier bitmask |
| `22 b1 b2 b3` | consumer bitmask: play/pause 08.., next 01.., prev 02.., stop 04.., mute 10.., vol+ 40.., vol- 80.. (byte1); media-player/explorer/mail/calc (byte2: 01/02/10/20); search/home/back/fwd/stop/refresh/fav (byte3: 01..40) |
| `50 01 00 00` | unassigned/disabled |
| `50 xx`, `41 xx`, `60 xx` | special functions (DPI etc.). Names NOT verified. Defaults: slot6 `50 07`, slot7 `41 00`, slot8 `50 04` |

Macro entries (type 5 / 0xC) patch byte 1 with the macro index (|0x80 for 0xC); not implemented.
Write frame (captured from vendor 0x415720): report `08 | (n<<4)|2 | 00 | 50 | 00 00 00 00 | 80 B | A5` (A5 at payload[80]).
The vendor app NEVER reads the matrix back. Readback opcode (n<<4)|2 is a guess (`dump-buttons --experimental`).
Validation: all 78 key names, modifiers, combos, mouse/scroll/click codes identical to vendor encoder output.

### Not applicable to this model
LOD cmds `1A`/`1B` (0x4156d0/0x415680) are gated by config flags; `ShowLOD=0` here, so never sent.

### pulsar.py additions
`leds`, `set-led`, `buttons`, `set-button`, `reset-buttons`, `dump-buttons --experimental`. All writes dry-run by default;
button writes refuse if no slot is left-click; last written matrix kept in `~/.config/pulsar-linux/buttons-pN.json`.
**None of the new commands have run on real hardware yet.**

### Next steps
1. `./pulsar.py leds`; then `--write set-led steady --color FF8800 --brightness 8` (check LED, then try other effects).
2. One harmless button test: `--write set-button 6 key:ctrl+shift+esc`.
3. Identify the 0x50/0x41/0x60 special functions by assigning raw values and observing.
4. Decode block 0x8B..0x8F, macros (0x415810), event channel (input ID 7), 0xFF01 collection.
5. GUI + PKGBUILD.

---

## 19. Hardware results (2026-10-14, user's mouse 258a:002e, profile 1, run under sudo)

### LED: WORKS (confirmed physically)
* `leds` before: effect 7 (flicker), speed 2, brightness 4, colours `00FF00`, `FF00FF`, ctl byte `0x42`.
  This matches the layout in section 18 (ctl `0x7C`, 2 colours at `0x7D`) as far as the decode is concerned.
* `--write --reselect set-led steady --color FF8800 --brightness 8` changed 4 bytes:
  `[0x3D] 07->02`, `[0x40] 40->80`, `[0x42] 00->88`, `[0x43] FF->00` (`[0x41]` was already `FF`).
  Read-back matched. **The mouse LED changed colour as expected** (steady orange).
* Confirms: effect id at `0x3D`, Steady brightness in the HIGH nibble of `0x40`, colour stored at `0x41..0x43` in R,G,B order.
* Still unconfirmed: whether `--reselect` is required for LED changes (it was used); brightness scale (8 of 0..15 used,
  visible effect of other values not compared); speed nibble role; the other effects.
* Side finding: the dump shows stage 8 = 100 DPI / `#000000`, 7 active stages, current stage 2, stage 1 = 3200 DPI, stage 2 = 400 DPI
  (left over from earlier tests, section 15).

### Buttons: write did NOT take effect
* `set-button 6 key:w` (matrix slot 6 = `21 00 1A 00`) was written **without** `--reselect`; base matrix was the Cfg.ini default
  (no state file existed). The tool reported "written" (there is no read-back for buttons).
* User result: **button 6 did not act as `w`** (no effect observed).
* `reset-buttons` was then written (default matrix: slot 6 = `50 07 00 00`). State file `buttons-p1.json` (under /root because of sudo)
  holds the default matrix. The mouse should therefore be back to the default button layout; not independently verified.
* Not yet determined why. Untested candidates, none verified:
  1. matrix write needs `--reselect` (profile re-select) to apply, as the DPI write seemed to;
  2. physical button 6 is not matrix slot 6 on this model (the slot field in Cfg.ini was assumed to equal the physical position);
  3. the matrix belongs to a different profile number (PID2/single-mode note in section 12) than profile 1;
  4. the device ignores or rejects the write (framing/length/marker difference), or only accepts it in a different mode (wired vs dongle);
  5. the key assignment was applied but the test method (which button was pressed) did not hit slot 6.
* No tests beyond these were run, per user request.

### Current state left on the mouse
* LED: effect 2 (steady), colour `FF8800`, brightness 8 (profile 1). Earlier backup of the previous block:
  `/root/.config/pulsar-linux/backup-20261014-125747-p1.bin` (restore with `sudo ./pulsar.py --write restore <file>`).
* Buttons: default matrix last written.
* DPI stages as in section 15.

### Next steps (not started)
1. Re-test the button write with `--reselect`, and with other slots, to separate candidates 1 and 2.
2. Try `dump-buttons --experimental` (read-only) to see whether the device returns the matrix.
3. Everything else in section 18's list.

---

## 20. Update (2026-10-14, later): static re-audit of the button write, profile rule, event channel, special functions

No hardware access this session; everything below is from disassembly/emulation of `OemDrv.exe`, plus unit tests of the new code against patched I/O.

### 20.1 The button write matches the vendor exactly: framing is not the problem
`Apply_24G` (0x417a30) is the **only** mouse apply routine (the other `Apply*` strings are PAD/thread wrappers). Its sequence, by flag bit in `[ebp+0x10]`:

| Flag | Step |
|---|---|
| 0x02 | read general block (0x4151b0) -> pack (0x419530 + LED packer 0x4198d0) -> write (0x4152b0) -> `Sleep(50)` |
| 0x01 | for each macro: 0x415810, `Sleep(50)`; then **matrix** 0x415720 (`ecx` = profile number, 0x50 bytes) -> `Sleep(50)` |
| 0x20 / 0x40 | LOD (0x415680) / debounce-style (0x4156d0), both gated by config (`ShowLOD=0` here) |
| always, last | `Sleep(0x5DC)` = 1500 ms, then done |

* There is **no commit / apply / profile-reselect command** anywhere in this sequence and the matrix is never read back. So "needs `--reselect`" (section 19 candidate 1) is not something the vendor app does.
* 0x415720 re-derived by hand: buffer = `[cmd][00][len=0x50][00 00 00 00][80 B payload][A5]` behind report ID 8; cmd = `0x12/0x22/0x32` for profile 1/2/3; `0xA5` at report offset 88. `Pulsar.write_buttons` produces byte-identical frames (checked again this session with patched I/O). Candidate 4 (framing) is therefore unlikely.

### 20.2 Profile rule: follows the USB PID (corrects the "active profile" assumption)
* `CDrv8948::FindDev` (0x414340) sets `[this+0x68]` when the **second** PID matched; vtable slot `+0x2c` (0x4143d0) returns `[this+0x68]` if the command handle is open.
* `Apply_24G` (non-"SingleMode"): if that is set the profile number must be **2**, else **1** (log codes 1002/1001 otherwise).
* So **258a:002e -> profile 1, 258a:002f (dongle) -> profile 2.** Profile 1 on the wired mouse (what was used so far) is correct; candidate 3 (wrong profile) is unlikely for USB.
* The event report (20.3) also switches the app's "mode" index between 0 (`0x10`) and 1 (`0x11`) unless SingleMode, consistent with the above.
* `pulsar.py` now selects the profile **from the PID** like the vendor (still overridable with `-p`) and prints a note when the device's active profile differs. Before this it trusted the cmd-02 active profile, which could pick the wrong one on the dongle.

### 20.3 Event channel decoded (input report ID 7, 8 bytes)
Service thread 0x410c00 -> `ReadFile` on the input report; bytes 1..4 (after the ID) are packed into one WM 0x7EB word; handler 0x46acc0 (message-map entry at 0x5efeb8):

| Byte | Meaning | Confidence |
|---|---|---|
| 1 | `0x10` = USB/wired, `0x11` = battery/wireless (same codes as the ReadPower reply). Any other value is ignored by the app | high |
| 2 | polling-rate code 1..4 = 125/250/500/1000 Hz | high |
| 3 low nibble | current DPI stage, 1-based (checked against the stage count) | high |
| 3 high nibble, 4 | only logged, purpose unknown | - |

New command `pulsar.py watch [--seconds N]` (read-only) opens every hidraw node of the device, prints each input report with a timestamp and decodes ID-7 reports. Unit-tested with pipes only; **not yet run on hardware**.

### 20.4 Which physical button is slot N (vendor artwork `skins/mouse/mouse_nr.png`)
1 left, 2 right, 3 wheel click, 4 and 5 the two side buttons, **6 = the DPI button behind the wheel**. Cfg `K<n>_1` field 4 is the matrix slot: K4 (code 0x14 = forward) -> slot 5, K5 (0x15 = back) -> slot 4, i.e. the UI numbers 4/5 and the matrix slots 4/5 are swapped. So the section-19 test (`set-button 6 key:w`) did target the DPI button's slot. That button's default is `50 07`, a firmware "special" function, so it may simply not be remappable to a keyboard key, or may behave differently from ordinary buttons. This is the leading untested explanation.

### 20.5 Vendor encoder (0x416580) emulated over every type-8 code (UI "function" codes 0xA2..0xD1)
Harness: `emu_enc.py` (Unicorn; reproduces the known type-1 defaults, sanity check passed).

| Code | Dword | Notes |
|---|---|---|
| 0xA2 | `50 01 00 00` | Disable (default of slots 9, 10) |
| 0xA7 | `50 02 00 00` | unnamed |
| 0xA8 / 0xA9 / 0xAA | `41 01` / `41 02` / `41 00` | order matches the UI menu DPI+, DPI-, DPI Loop: **guess, low confidence** (K7 default = 0xAA) |
| 0xAB | `50 06 00 00` | unnamed |
| 0xAC | `50 04 00 00` | default of slot 8; 0xD0/0xD1 = `50 04 01` / `50 04 02` (variants) |
| 0xAE | `50 07 00 00` | default of slot 6 (DPI button) |
| 0xAF | `50 09 00 00` | unnamed |
| 0xC0 | `60 01 00 00` | possibly Fire key (tc_fire) |
| 0xC2 | `50 08 00 00` | unnamed |
| 0xA4 | `32 00 00 00` | odd; unnamed |
| others in range | `ff ff ff ff` or no result | not valid on this encoder |

Type 1 (mouse) confirmed again: 0x11 `11 01` L, 0x13 `11 02` R, 0x12 `11 04` M, 0x15 `11 08` back, 0x14 `11 10` fwd, 0x16/0x17 double/triple click, 0x18/0x19 scroll up/down. Names for the `50/41/60` dwords are **not** verified; the UI code->label table was not found. Test with `raw:` and observe.

### 20.6 Updated candidate list for the failed button write (section 19)
1. DPI button (slot 6) is firmware-special and not remappable this way. **Most likely; cheapest to test (E3).**
2. Keyboard output has no keyboard interface to go through, or goes through a node nobody has looked at (E1/E2).
3. Applied only after something we don't do (reselect, mode change). Vendor does not, so unlikely.
4. Wired vs dongle difference. Wired = 002E = profile 1 (confirmed above), so unlikely.
Framing/slot-order/profile number: ruled out as far as static analysis can.

### 20.7 Next steps (hardware)
Using the udev rule no `sudo` is needed (and avoids backups landing under `/root`).
* **E1** `./pulsar.py list`: paste the full output (is there a keyboard collection on hidraw0?).
* **E2** `./pulsar.py watch --seconds 40`: press each of the 6 buttons, click and turn the wheel. Note which node/report each produces; press the DPI button and see if an ID-7 event with a new stage appears (also validates 20.3).
* **E3** test an ordinary button instead of the DPI button: `./pulsar.py --write set-button 5 key:w` (forward button; left click stays), run `watch` and press it, then `./pulsar.py --write reset-buttons`.
* **E4** if E3 works and slot 6 doesn't: slot 6 is special, list it as non-remappable. If E3 also fails, retry with `--reselect`, then `dump-buttons --experimental`.
* Then as before: LED effects other than steady, macros (0x415810), 0x8B..0x8F, cmd 0x80, GUI + PKGBUILD.

---

## 21. Hardware results (2026-10-14, later): buttons WORK; DPI+ identified; event and mouse reports decoded

**Hardware-confirmed**
* `list`: hidraw0 = plain mouse (usage 0x0001:0x2, 7-byte input, **no report ID**). hidraw1 = keyboard (ID 1, 8 B), consumer (ID 2, 4 B), vendor: feature 8 (520 B), input 7 (8 B), feature 5 (8 B), and `0xFF01` feature 6 (8 B). So keyboard/media output from button assignments has a proper interface (hidraw1).
* `set-button 5 key:w` (forward button) **works**. Slot numbering, the 4/5 swap, the matrix frame, profile 1 on USB and "no reselect needed" are all confirmed. The failed slot-6 test (section 19) was therefore about slot 6, not the write path; slot 6 (the DPI button, default `50 07`) is still untested/special.
* **`41 01 00 00` = DPI+** (next stage, saturates at the last active stage, no wrap). Forward button presses produced no button mask on hidraw0 (firmware consumes it) and ID-7 events with stage 4->5->6->7->7->7.

**Mouse report (hidraw0, 7 bytes, no ID):** `[buttons][X int16 LE][Y int16 LE][wheel int8][pad]`; buttons: 1 left, 2 right, 4 middle, 8 back, 0x10 forward.

**Vendor event (hidraw1, ID 7, 8 bytes), corrects section 20.3:** `07 | mode | rate | stage | dpi_reg | 00 00 00`
* mode `0x10` USB / `0x11` battery; rate code 1..4; **stage = byte 3 (plain, 1-based)**; **byte 4 = DPI register of that stage**, `dpi = (reg+1)*100` (seen: 0x17=2400, 0x27=4000, 0x4f=8000, 0x9f=16000). The "high nibble / unknown" reading in 20.3 was wrong. Sent on every stage change, including repeated presses at the last stage.

**Tool changes:** `decode_event` fixed (requires 8-byte report); `watch` now decodes mouse reports, hides pure motion (`--motion` shows it); `dpi-up` added as a named button function. Unit-tested with packet sockets; real-hardware verification is the capture above.

**State left on the mouse:** profile 1, slot 5 = `41 01` (DPI+). Restore: `sudo ./Fire_pulsar.sh --write reset-buttons`.

**Next:** test `raw:41020000` (guess DPI-), `raw:41000000`, `raw:50020000`, `50060000`, `50070000`, `50090000`, `60010000` on slot 5 with `watch`, name what changes. Slot 6 with `key:w` again is not needed. Then macros, LED effects, GUI, PKGBUILD.

---

## 22. Hardware results (2026-10-14, later): DPI special functions; extra physical keys

**Slot 5 (forward button) test series, profile 1, wired 258a:002e, observed with `watch`:**

| Dword | Result | Evidence |
|---|---|---|
| `41 01 00 00` | **DPI+**: next stage, stops at the last active stage (no wrap) | stage 4,5,6,7,7,7 |
| `41 02 00 00` | **DPI loop**: next stage, wraps from the last active stage to stage 1 | 3,4,5,6,7,**1**,2,3 |
| `41 00 00 00` | not yet tested here. Default of slot 7. Guess: DPI- (from the vendor menu order DPI+, DPI-, DPI loop vs codes 0xA8/0xA9/0xAA); the guess for `41 02` was wrong, so treat this one as unverified | - |

* Earlier guess "`41 02` = DPI-" was **wrong**; "`41 01` = DPI+" was right. Named in `pulsar.py`: `dpi-up` = `41 01`, `dpi-loop` = `41 02`.
* Each press emits an ID-7 event (`07 mode rate stage dpi_reg`); the DPI is `(dpi_reg+1)*100`. Looping showed the profile's stage table exactly: 1=3200, 2=400, 3=1500, 4=2400, 5=4000, 6=8000, 7=16000.
* Stages 1 and 2 (3200, 400) are leftovers of the earlier tests (section 15). With that table the loop jumps 400 -> 1500 -> 2400 -> 4000 -> 8000 -> 16000 -> 3200, which the user felt as "the DPI skyrockets". Not a tool bug. Reset with `set-dpi 1 500`, `set-dpi 2 1000`.
* Left, right and middle clicks were unaffected during the test.
* Left on the mouse (unless the user reset it): slot 5 = `41 02`. Restore: `sudo ./Fire_pulsar.sh --write reset-buttons`.

**Extra physical keys reported by the user (not yet investigated):**
1. A key **right beneath the mouse wheel**. Most likely the DPI button, which the vendor artwork (`skins/mouse/mouse_nr.png`) shows as position 6 directly behind/below the wheel (slot 6, default `50 07`). Not confirmed that this is the same key.
2. A **report-rate (polling-rate) key**. No matching button position exists in the vendor artwork or in `Cfg.ini`'s key table (`K1..K10`), so it is probably handled entirely by firmware and is not remappable. The ID-7 event carries the rate code (byte 2), so pressing it should show up in `watch` as a rate change.

**To investigate (read-only, no writes needed):** run `./pulsar.py watch --seconds 40`, press each of those two keys several times and note (a) whether anything appears on hidraw0 (button mask / wheel), (b) whether an ID-7 event appears and which field changes (stage for the DPI key, rate code for the report-rate key). Also check the section-19 slot-6 question again: with `watch` running, press the key beneath the wheel on the default matrix and see whether it emits `stage` events.

---

## 23. Session (2026-10-14, later): GUI, packaging, udev rule fix. No hardware access this session

**Done (verified only with a fake device, see below)**
* `pulsar.py`: new `patch_nstages` + CLI `set-stages N` (active stage count, current stage clamped); `save_backup()` helper (CLI behaviour unchanged).
* `pulsar_gui.py` (new): tkinter GUI with tabs DPI/polling (rate, stage count, per-stage DPI + indicator colour, current stage),
  Lighting (effect, speed, brightness, colours, direction) and Buttons (slots 1..10, editable combobox using the same `parse_button` syntax).
  Logic lives in `Session` (no GUI imports): load -> compare UI values with the loaded snapshot -> patch only changed fields on a
  fresh read -> backup -> write -> read back. Button matrix is written only if it differs from the remembered one.
  `test_session.py` (fake device implementing `read_profile_raw/write_profile/write_buttons`): all checks pass, including
  "unchanged settings write nothing" for every LED effect 1..10 and the steady/orange change giving the same bytes as section 19.
* **The tkinter widget layer itself has NOT been run** (no tkinter/display in the sandbox); it only passes `py_compile`. Expect to fix small
  layout/widget bugs on first launch. Needs `sudo pacman -S tk`.
* `PKGBUILD` + `hator-pulsar.install` + `hator-pulsar.desktop`: installs to `/usr/lib/hator-pulsar`, commands `hator-pulsar` and `hator-pulsar-gui`.
  `package()` was dry-run by hand (makepkg not available here); `sha256sums` are `SKIP` (run `updpkgsums`).
* **udev rule renamed `99-` -> `70-hator-pulsar.rules`.** The `uaccess` tag must be set before `73-seat-late.rules` runs; a 99- file sets it
  too late, so no ACL is applied (that would explain needing sudo). Remove the old copy: `sudo rm /etc/udev/rules.d/99-hator-pulsar.rules`,
  install the new one, `sudo udevadm control --reload && sudo udevadm trigger`, replug. (From knowledge of systemd/udev ordering, not tested here.)

**GUI limits to remember**
* The matrix cannot be read from the mouse; the Buttons tab shows the last matrix this tool wrote (or the Cfg.ini default).
* Slots 7..10 have unknown physical meaning; slot 6 (DPI button) may not be remappable. Stage colours are ignored by some LED effects (section 15).

**Still open (needs the mouse)**
1. `./pulsar.py watch --seconds 40`: press the key under the wheel and the report-rate key; note which reports appear (section 22).
2. `set-button 5 raw:41000000` + watch: is it DPI-? (still a guess.) Then `raw:50020000`, `50060000`, `50070000`, `50090000`, `60010000`.
3. Run the GUI (`./Fire_pulsar_gui.sh`), try one DPI change and one LED change, report any traceback.
4. LED effects other than steady, macros (0x415810), block bytes 0x8B..0x8F, cmd 0x80.

---

## 24. User findings (2026-10-14, later): physical slot map corrected

Reported by the user from hardware tests (override of each slot works):

| Slot | Physical button | Default dword | Notes |
|---|---|---|---|
| 1 | left | `11 01` | confirmed |
| 2 | right | `11 02` | confirmed |
| 3 | wheel click | `11 04` | |
| 4 | side (back) | `11 08` | UI 4/5 swapped vs slot (section 20.4) |
| 5 | side (forward) | `11 10` | confirmed remappable (section 21) |
| 6 | **none known** | `50 07` | earlier assumed DPI button: **wrong**. Function unknown |
| 7 | **DPI button** (under wheel) | `41 00` | override works; stage indicator colour does not change after override (irrelevant) |
| 8 | **report-rate button** | `50 04` | |
| 9 | unknown | `50 01` (disabled) | purpose unknown |
| 10 | unknown | `50 01` (disabled) | purpose unknown |

This corrects sections 19/20.4/22: the failed `key:w` test on slot 6 simply hit a slot with no known physical button.
`pulsar.py` now labels the factory-default dwords (`SPECIAL_HINTS`); GUI slot labels updated.

Open: `41 00` meaning (default of the DPI button, untested on slot 5); `50 04` = rate cycle (guess);
`50 02/06/07/08/09`, `60 01`, `50 04 01/02` still unnamed.

### Next tests (slot 5 as test bench, `watch` running, `reset-buttons` afterwards)
1. `raw:41000000`, then `raw:50040000` (watch ID-7 byte 2 = rate code), then `raw:50040100`, `raw:50040200`.
2. `raw:50020000`, `50060000`, `50070000`, `50080000`, `50090000`, `60010000`: note any ID-7 event or mouse-report change.
3. Slots 9/10: set `key:w` on each, press/tilt every part of the mouse with `watch`; if nothing types `w`, no physical button.
4. Slot 6: treat as unmapped (the `key:w` test already failed).

**Update:** user confirmed slots 9 and 10 have no physical button; removed from the GUI (matrix keeps them at `50 01`, disabled). Slot 6 stays listed (no known button).

**Correction (user):** slot 6 IS a physical button, the one under the mouse wheel (name unknown). The statement above that slot 6 has
no known button / that the earlier `key:w` test hit a slot with no button was wrong. The section-19 failure (`set-button 6 key:w`
had no effect) is therefore still unexplained; default `50 07` is a firmware function. Slot 7 = DPI button, slot 8 = report-rate button.
Next: `set-button 6 key:w` again with `watch` running (does the press show on any node?), then `raw:41010000` / `raw:41020000` on slot 6.


lightning report(brightnes setting does not work at all so it needs to be removed):
1. Works, no customisable colour, speed change works but really strange(0 - max speed, 1 - slow, 2 - fster than 1, 3 - faser than 2, 4 - max speed, and the pattern repeates)
2. Fully works
3. Works, customisable colour, speed change works in range of 1-12(13-15 seems to set speed to default, 0 sets max speed)
4. Works, no customisable colour, speed change in the same way as 1 animation
5. Works, no customisable colour, speed change in the same way as 1 animation
6. Fully works, no colour or speed customisation
7. Works, customisable colour, speed change works in range of 0-3(0 - 3 normal progression of speed, then pattern continues, last positions just put max speed)
8. Works, no customisable colour, speed change works in range of 1-4(0 - max speed, 1-4 normal progretion, and the pattern repeates)
9. Works, no customisable colour, speed change works in range of 0-5(everything else just max speed)
