# Changelog

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
