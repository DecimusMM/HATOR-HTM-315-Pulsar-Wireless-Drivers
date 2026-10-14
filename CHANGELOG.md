# Changelog

## 0.3.1

- Automatically start the host-side macro listener after the GUI successfully connects to the mouse.
- Stop the listener when the GUI closes, and retain a Start / restart control for recovery.
- Show a friendly status when the optional `python-evdev` dependency is missing.
- Update the macro help text and installation notes to remove the manual-start requirement.


## 0.3.0

- Reworked the Tkinter UI with a consistent dark theme, clearer groups, staged-change status, and connection/profile information.
- Added a listener start/stop control to the GUI and safer listener diagnostics.
- Added preflight validation before writes, a confirmation before writing the full button matrix, and separate local backups for button maps.
- Made JSON config updates atomic and restrictive by default; profile backups no longer rely on second-level timestamps.
- Corrected the udev product-ID match and completed the Arch package contents and launchers.
- Added hardware-free unit tests and CI checks.
