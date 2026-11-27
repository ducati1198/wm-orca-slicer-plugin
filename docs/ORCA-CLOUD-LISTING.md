# Orca Cloud listing: ready-to-paste fields

Steps (from the Orca Cloud help guide): sign in to Orca Cloud with the same account as OrcaSlicer, open **Plugins**, tab **Shared**, press **+**.

| Field | Value |
|---|---|
| Plugin files | `wm_device_any.py` (a copy of `wm_device.py`; the file name needs the OS suffix `any` for a universal .py) |
| Name | WM Device |
| Description | Device tab and tool mapping step for the WonderMaker ZR Ultra / Ultra-S (Moonraker): live status, controls (including the chamber light, with a colour-matched button on printers where the optional live-refresh helper is installed), ACE Pro and endless spool, and a window after slicing to choose which toolhead prints which filament. On a factory printer, an "Install helper on printer" button (macOS/Linux builds of Orca) installs the optional live-refresh helper over SSH with one click, after you confirm a warning popup - no terminal needed. Works with the factory Klipper and the factory app; nothing is installed on the printer unless you choose to add the optional helper. |
| Version | the `version` line at the top of `wm_device.py` |
| Min. OrcaSlicer version | 2.5.2 (the version it was tested on) |
| Tags | printer, moonraker, klipper, wondermaker, multicolor, device |
| Changelog | the newest entry of `CHANGELOG.md` |
| Icon | optional PNG (up to 2 MB) |
| Public plugin | on, only when you want it in Plugin Hub |

"Create plugin" starts an automated review; it does not publish at once. Later versions: **Edit plugin**, bump the version.
