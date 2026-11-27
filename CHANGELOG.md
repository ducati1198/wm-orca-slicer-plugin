# Changelog

## 0.29.21
- Fixed: the "unused filament not mapped" fix in 0.29.20 read the wrong part of the exported G-code file. OrcaSlicer writes the `; filament used [mm] = ...` line in the file's **footer** (near the end), not near the top - the 0.29.20 check only scanned the first 300 lines, so it never found the line and silently fell back to showing every filament again. The plugin now reads the last 16 KiB of the file instead, where that line actually is.

## 0.29.20
- Fixed: the **WM Tool Map** window (after slicing) listed, warned about and asked you to map **every filament configured in the Orca project**, even ones your current print doesn't use at all. It now reads which filament slots the slice actually used (from the file's own "filament used" line) and only shows, matches and warns about those - a project with 4 filaments where only 2 are in this print now shows just those 2. An unused slot is never removed from the toolhead map itself (only hidden from the window), so the slot numbers written into the G-code stay correctly aligned even when the used slots aren't the first ones. If the usage line can't be read for any reason, the window falls back to showing everything, same as before.

## 0.29.19
- Fixed: the **WM Tool Map** window's warning text (both the post-slice popup and the live mapping panel in the Device tab) numbered toolheads one higher than the toolhead buttons themselves - a warning could say "toolhead 2" about the exact same toolhead its own button labelled "T1". Warning text now always matches the button labels (T0, T1, T2, T3).
- Fixed: the **Light** button's colour didn't reflect the chamber light's real on/off state on printers using a `WM_CHAMBER_LIGHT` Klipper macro (the vanilla Klipper setup) - only factory printers with the optional live screen refresh helper had this. The light is a Moonraker `[power chamber_light]` device on these printers, so the plugin now reads and sets its real state straight over Moonraker's own `device_power` API, no extra helper needed. The button now turns green while the light is on there too.
- Added an offline self-test suite (`tests/`, run with `tests/run_all.sh`) covering the toolhead-numbering fix, the unused-filament fix, and the chamber-light state sync, so a future change to this logic can be checked without a real printer.

## 0.29.18
- Fixed: the **Screen live-refresh helper** card (and the "Install helper on printer" button) could show up on a **vanilla (upstream) Klipper** printer too, not just factory printers. The card's visibility check only looked for `tmt1.ini` existing - but a vanilla Klipper + Fluidd setup keeps a `tmt1.ini` too (for the same colour/material swatches), so its presence alone never meant "factory screen board". The check now also confirms the printer lacks the vanilla installer's `_ZR_SET_TOOL_INFO` macro, the same signal already used elsewhere in the plugin to tell factory and vanilla printers apart.

## 0.29.11 - 0.29.17
- **One-click installer:** when the live screen refresh helper is not detected on a factory printer (macOS/Linux builds of Orca), the Device tab now shows a **Screen live-refresh helper** card with an **Install helper on printer** button. It connects over SSH (factory default login pre-filled, editable) and installs the helper for you - no terminal needed. See the README's [2.4b, Option A](README.md#24b-factory-printers-live-screen-refresh-optional).
- Fixed: the Light button could crash with a `TypeError` when the helper was unreachable (a helper-status check returned the wrong "not available" value instead of `None`).
- Fixed: the "Screen live-refresh helper" card could stay invisible even when it should show, due to a CSS display bug (an inline style was cleared instead of set to `block`).
- Fixed: clicking "Install helper on printer" could crash OrcaSlicer outright on macOS (a CoreFoundation fork-safety abort). The installer no longer forks the host process at all - it authenticates over SSH through OpenSSH's `SSH_ASKPASS_REQUIRE=force` mechanism, run via a plain `subprocess.run()` call instead of a forked pty session.
- Fixed: the installer's one-time password-entry script is now written inside the plugin's own data folder instead of the system temp directory, avoiding an Orca plugin-sandbox permission denial ("audited operation tempfile.mkstemp").

## 0.29.10
- **Plugin description updated** (shown in Orca's Install/Plugins dialog) to match 0.29.8/0.29.9: the chamber light button is now always shown on factory printers (previously it said the button was hidden without the live-refresh helper); clarified that controlling the light and the instant screen/colour updates both need that optional helper, and that the button turns green while the light is on.
- **Orca Cloud listing description updated** (`docs/ORCA-CLOUD-LISTING.md`) to mention chamber light control.

## 0.29.9
- **Chamber light icon sync (factory printers):** toggling the Light button now pokes the touchscreen's own "is the bulb icon on" flag directly, so its bulb icon flips immediately instead of waiting for its own ~10 s GPIO poll to catch up (best-effort: if it can't, the icon still self-corrects within a few seconds on its own).
- **Light button colour:** the Light button now turns green while the chamber light is on (factory printers, via the live screen refresh helper; printers with a real `WM_CHAMBER_LIGHT` Klipper macro have no tracked state in this version and the button stays plain).
- **Fix (helper, `screen-sync/wm_screen_sync_http.py`):** the background status check (`GET /light?action=status`), used to keep the Light button's colour in sync with the real light over time, was silently 404ing on every call because the route match didn't strip the query string. Fixed to match `do_POST`'s parsing.

## 0.29.8
- **Chamber light on factory printers:** the Light button, previously hidden on any printer without a `WM_CHAMBER_LIGHT` Klipper macro (all factory firmware), now also shows on factory printers and controls the real chamber light via the optional live screen refresh helper (`screen-sync/`). No helper installed or reachable → falls back to the previous "no macro" message, reworded to mention the helper.

## 0.29.7
- **Faster spool colour update on factory printers:** saving a spool colour/material now writes it straight into `tmt1.ini` and refreshes the tab's own spool icon immediately, instead of waiting for the background poll's next `tmt1.ini` read (previously up to ~10 s). The touchscreen itself still updates the same way as 0.29.6 (immediately with the optional helper installed, otherwise on its next app restart).
- Plugin description (shown in Orca's Install/Plugins dialog) now also explains the factory-printer live screen refresh and points to the README section for it.

## 0.29.6
- **Live screen refresh (optional) for factory printers:** after a spool colour/material save, the plugin now tries an optional, separately-installed local helper that makes the touchscreen update immediately instead of waiting for its app to restart. Nothing changes if the helper isn't installed. See `screen-sync/` and the README section "Factory printers: live screen refresh (optional)".

## 0.29.5
- **Spool editor on factory printers:** opening a toolhead now preselects its current material (read from `tmt1.ini`) instead of always showing "Empty / not set".
- **Factory save result:** the message after saving a spool now says the touchscreen shows it once its app restarts.
- **Nozzle temperature on factory printers:** the box is read-only and shows that material's usual default (factory printers do not store a temperature per slot); vanilla Klipper printers keep their own saved temperature, unchanged.

## 0.29.4
- **Vanilla Klipper spool editor:** the material list now matches the printer's own 27-material table (previously only the touchscreen's smaller 16-material list), with the exact saved index read back and reselected, so names like "PLA-CF" or "PETG-CF" round-trip correctly.

## 0.29.3
- **Orca API check panel:** a small panel reports which `orca.host.*` capabilities the running Orca build exposes (used to confirm plugin features on OrcaSlicer 2.5.2).

## 0.29.2
- **Description box in the tool mapping windows:** above the buttons, one line explains what each button does (Apply mapping, Skip mapping, Cancel in the slicing window; Apply and print, Print without map, Cancel in the Print window). The windows are a little taller to fit it.

## 0.29.1
- Timelapse page: the **Open in browser** button is now called **Download** (same action: your browser opens or saves the video).
- The video player now fits the picture, so the black bars on both sides are gone; the picture keeps the same size. The Status page camera does the same and is a little bigger (480 px tall, still 16:9).
- **Tangle indicator** on each toolhead card in Status (from the hub tangle sensors path_0 to path_3): grey while the filament is free, red "TANGLE" when the hub sensor triggers. Hidden on printers without these sensors.

## 0.29.0
- **Timelapse page:** lists the printer's timelapse videos (Moonraker `timelapse` folder, newest first) with their preview pictures, size and date. **Play** opens the video in the tab, **Download** hands it to your browser to save, tick boxes and a red **Delete** remove videos after a "Yes, delete" confirmation.
- Only `.mp4` / `.webm` files inside the timelapse folder can be opened or deleted.

## 0.28.1
- **History page upgrade:** each print shows the part picture, the filament colours as spools, the weight and a coloured result badge (last 25 prints), with a totals bar on top (prints, print time, filament, longest print).
- **Print again** opens the tool mapping window with your remembered filament choice for that project. Entries whose file is gone show "file deleted".
- **Tick boxes and Delete** remove entries from the history list only; the gcode files stay on the printer.

## 0.28.0
- **Delete files:** the Files page has a tick box next to each picture and a red **Delete** button next to **Refresh list**. It shows the files, asks "Yes, delete" or Cancel, and is blocked while a print runs.
- **Unload Menu button** in the Move section (green, next to Light, Home Z and Motors off, now in two columns). It runs the printer's `WM_UNLOAD_MENU` macro, is blocked while printing but allowed when paused, and is hidden on printers that do not have the macro.
- **Printer popup windows open in the tab:** when a macro shows a prompt window (Fluidd style), the same window opens in the WM Device tab with large bold text. Only buttons the printer sent can be pressed.
- The Move dial is bigger and the Z+ / Z- buttons match its height.

## 0.27.0
- **Spool pictures:** the colour dots are now small filament spools (outer rim, six cut-outs, hub) in the Device tab toolhead and ACE cards, on the Files page and in the Print mapping window. The material name is on the card label.
- **Printer address is remembered** on Windows too: the last printer is saved in the plugin's data folder and filled in again after Orca restarts (the page asks for it several times while it loads, and the address list is kept in two folders).
- The permission request lists every saved printer, and a line in `trace.log` says which ones were requested.
- Plugin updates now keep the address list when the Orca storage folder is not readable at load.

## 0.26.1
- Fixed: after installing from **Orca Cloud** the plugin never asked for network permission. The saved printer list is now kept in the plugin's own data folder and in Orca's storage folder, and both are read when the plugin loads. A line in `trace.log` shows which printers were requested.
- README and Orca Cloud page: install from Orca Cloud, and how to add a printer by hand in **File > Plugins > Permissions > Network access** when Orca shows no dialog.

## 0.26.0
- **Files page:** a picture of each part, its filament colours and weight. **Print** now opens the tool mapping window first and then starts the print (or **Print without map**).
- New **Style** button: current look, or two modern dark styles.
- **Home Z** and **Motors off** buttons in the Move section (not while printing).
- **Endless spool** and **Filament in the file** panels side by side; tighter layout.
- Orca notices when the connection to the printer is lost or comes back, and when Klipper reports an error or shutdown.
- The trace log is kept to a small size.

## 0.25.6
- **Camera** now shows the whole picture (like Fluidd) instead of a cropped one.
- New **Refresh camera** button next to Hide camera: reloads the stream and turns the camera back on.
- The toolhead list is re-read now and then, so the filament cards come back if it was read while Klipper was restarting.

## 0.25.0 - 0.25.5
- **Remembered tool map:** the map you choose is saved per project (file name without the time part, plus the number of filaments) and filled in the next time you export it. Saved in the plugin's data folder.
- **Material warning** in the mapping window: a red "Different material" note when a filament does not match the loaded spool. The first press of Apply / Upload / Print stops, the second goes on.
- **Print notification:** Orca shows a notice when a print ends, pauses or fails (works while the WM Device tab is open).
- The mapping window no longer freezes Orca while slicing.

## 0.24.14
- Removed the Extrude / Retract buttons and the Home all button of the Controls panel (the Home circle in the move pad stays).

## 0.24.13
- **Forget** now sticks: a printer you erased is not added back from Orca's printer settings at the next start. Typing it and pressing Connect brings it back.

## 0.24.10 - 0.24.12
- **Forget** button next to Connect (press twice to erase a saved printer address).
- Fixed the permission dialog showing no printers: Orca does not allow `plugin.storage()` while a plugin loads, so the saved printer list is now found from the plugin's own file path.
- Clearer message for a new printer (close Orca, open it, approve, Connect).

## 0.24.8
- Fixed **Orca crashing at startup**: the plugin read Orca's printer settings while loading, which Orca does not allow. It no longer touches them at load.

## 0.24.1 - 0.24.7
- One combined permission dialog per plugin update (printer addresses are named once, at load, and kept in the plugin's data folder). No printer address is written in the code.
- Tool mapping window after slicing: **Apply mapping**, **Skip mapping**, **Sync printer filament**, **Cancel** (a cancelled job becomes a harmless file with no moves).
- Read-only "Filament in the file → toolhead" view in the tab.
- Works with the WM_SET_TOOL_MAP macro (Raspberry Pi setup) and with the factory Klipper (plain SAVE_VARIABLE lines).

## Earlier
- Device tab with live status, toolheads, camera, controls, files, history, messages and the ACE Pro panel.
