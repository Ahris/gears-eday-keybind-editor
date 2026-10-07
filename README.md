# Gears of War: E-Day Keybind Editor

A small tool for viewing and changing your **keyboard and mouse binds** in Gears of War: E-Day by editing the game's "encrypted" .sav settings file directly.
This has ONLY been tested on the Steam version so far, I do not know where the WinSDK/Gamepass version saves its configs, it could be in the same place so it might work.

## Requirements

- **Steam** version of the game
- **Python 3.8 or newer**
  - When installing, leave **"tcl/tk and IDLE"** checked. The tool will need it to open the GUI.

## How to

1. **Turn off Steam Cloud for the game:** right-click it in your Steam library > Properties > General > untick Steam Cloud. Otherwise Steam may put your old settings back.
3. Double-click `gears_eday_keybinds.py`, or run:
   ```
   python gears_eday_keybinds.py
   ```
4. It should find and open your settings file by itself. If not, click **Open...** and go to:
   ```
   %LOCALAPPDATA%\Microsoft\Gears of War E-Day\Saves\<your id>\EnhancedInputUserSettings.sav
   ```
5. Double-click any action to change its keys, then click **Save**.
6. Start the game and check your binds. If they're right, test them in the Boot Camp training mode, then you can turn Steam Cloud back on.

To just print your binds without opening the window:
```
python gears_eday_keybinds.py --dump
```

- Or just download the compiled .exe in the releases tab, whatever works for you.

## Extra

- Change the sprint style (Modern/Legacy) **in the game's menu as well**. The game stores that setting in a second file too called TCSettingsSavepoint.sav
- Every save makes a backup next to the original (`EnhancedInputUserSettings.sav.bak-<date>`). To undo the changes, close the game, delete the edited file and rename the backup back.
- I have no idea if an update will break this in the future, or if they will just fix their game and allow us to double bind things.
- The Xbox app / MS Store / Game Pass version has not been tested
