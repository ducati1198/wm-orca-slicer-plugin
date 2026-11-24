# Changelog

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
