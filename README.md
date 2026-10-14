# Pulsar Control for Linux

A small Linux desktop application and CLI for the HATOR Pulsar Wireless mouse (USB IDs `258a:002e` and `258a:002f`). It uses Python's standard library for HID access and Tkinter for the GUI. The optional host-side macro listener requires `python-evdev`.

> **Hardware note:** this is a reverse-engineered, community tool, not HATOR software. DPI/polling writes and the listed button encodings have supporting observations; several LED effects and parts of the button protocol are not vendor-certified. Test on your own device before relying on a setting.

## Highlights

- Dark, keyboard-friendly desktop interface with DPI/polling, lighting, button mapping, and macro tabs.
- Profile backups created before profile writes, followed by a read-back check.
- Preflight validation before writes, safer config/backup files, and clear warnings where the hardware cannot report its state.
- CLI dry-run behavior remains the default for write commands; writes require `--write`.
- Macro listener starts automatically with the GUI and is stopped when the GUI closes; the GUI can restart it after a permissions/dependency fix. Key events are not logged by default.

## Requirements

- Linux with `hidraw` support and a compatible HATOR Pulsar device/receiver.
- Python 3.
- Tkinter (`tk` on Arch Linux).
- Optional macros: `python-evdev` and user permissions to read suitable `/dev/input/event*` devices and write `/dev/uinput`.

On Arch Linux:

```sh
sudo pacman -S python tk
# Required for host-side macros (the GUI otherwise reports that macros are unavailable):
sudo pacman -S python-evdev
```

Do not run the whole GUI as root as a permissions shortcut. Install the udev rule below, reconnect the mouse, or log out/in if the access ACL does not update.

## Run from a checkout

```sh
./hator-pulsar-gui
```

CLI examples:

```sh
./hator-pulsar list
./hator-pulsar info
./hator-pulsar dump
./hator-pulsar set-rate 1000       # dry run only
./hator-pulsar --write set-rate 1000
```

The command-line option order follows argparse: global options such as `--write` go before the subcommand.

## Arch package

The `PKGBUILD` builds a local package. From the project directory:

```sh
makepkg -si
```

Before a public release, replace the placeholder project URL in `PKGBUILD`, select a license that the maintainer has the right to grant, replace `LICENSE` with that license's full text, and update the package's `license` field. No license grant or upstream URL was present in the supplied source files, so neither is silently invented here.

The package installs these commands:

- `hator-pulsar` — CLI
- `hator-pulsar-gui` — desktop UI
- `hator-pulsar-macros` — optional macro listener

It also installs a desktop entry and `/usr/lib/udev/rules.d/70-hator-pulsar.rules`.

## Safety and limitations

### Profile settings

Before writing a profile block, the app stores the original HID report in `~/.config/pulsar-linux/backup-*.bin`. It validates the expected profile markers and reads the profile back after writing. A mismatch stops the second (button matrix) write, but cannot automatically roll back a firmware write that already happened.

### Button mappings

The mouse does not provide a verified read-back operation for its complete button matrix. The tool uses the last matrix it wrote, or vendor configuration defaults when no local copy exists. Applying a changed map writes all 20 slots; the GUI displays the eight physical buttons exposed by the current hardware notes. A local backup is made before replacing the saved matrix, but a locally remembered/default map is not proof of the map actually stored in the mouse. The app requires at least one left-click assignment.

### Lighting

The controls intentionally expose speed only for effects with observed working ranges and colors only for Breathing and Flicker, based on the supplied hardware notes. Do not assume undocumented firmware fields behave like the vendor app.

### Macros

Macros are host-side actions, not mouse firmware features. Map a button to `macro:NAME` and apply the button map. The GUI starts the listener automatically when it opens and stops it when it closes; use **Start / restart** in the Macros tab after fixing a dependency or permission issue. It needs `/dev/input` read access and `/dev/uinput` write access. The exact permissions depend on the distribution/session; avoid broadly granting input-injection access to untrusted users.

Macro `exec` and `exec-wait` statements execute shell commands as your logged-in user. Only run macro definitions you trust. The `--all` listener option can observe key events from every selected keyboard-capable device and should be used cautiously. Event names are not logged unless `--verbose` is passed.

Macro scripts support one step per line:

```text
key ctrl+shift+esc
down shift
up shift
type Hello
wait 100
```

Blank lines and whole-line comments starting with `#` are ignored. `wait` is limited to 600,000 ms per step and scripts to 1,000 lines / 64,000 characters.

## User data and backups

All local state is kept under `~/.config/pulsar-linux/`:

- `backup-*.bin` — raw profile reports before writes
- `buttons-pN.json` — last locally remembered button matrix for profile N
- `buttons-backup-*.json` — previous locally remembered map before a changed map is written
- `macros.json` — macro definitions
- `macro-listener.log` — listener diagnostics from automatic GUI startup or a manual restart

The tool does not upload configuration or mouse data.

## Tests

Tests do not open a device or send HID reports:

```sh
python -m unittest discover -s tests -v
python -m py_compile pulsar.py pulsar_gui.py pulsar_macros.py
```

`packaging/emu_enc.py` is a developer-only reverse-engineering helper; it requires the original vendor executable plus `pefile` and Unicorn and is not installed by the package.
