#!/usr/bin/env python3
# Gears of War: E-Day keybind editor
# by Ahri - twitter.com/Ahrisss
#
# edits EnhancedInputUserSettings.sav (keyboard + mouse binds)
#
#   python gears_eday_keybinds.py              opens the editor
#   python gears_eday_keybinds.py FILE.sav     opens it with that file
#   python gears_eday_keybinds.py --dump FILE  just prints the binds
#
# no extra packages needed, just python 3.8+
# close the game + turn off steam cloud before saving or it'll get overwritten

import copy
import datetime
import glob
import os
import shutil
import struct
import sys

# ---- save-file format ----

PROFILE_CLASS = "/Script/TCSettings.TCPlayerMappableKeyProfile"
CURRENT_PROFILE_PROP = "CurrentProfileIdentifierString"
TEMP_PROFILE = "TCSettingsTemporaryProfile"
INTERNAL_PROFILES = ("InputUserSettings.Profiles.Default", TEMP_PROFILE)
# kbm only has modern/legacy (sprint style), the rest are controller schemes
SELECTABLE_SCHEMES = ("MODERN", "LEGACY")
CONTROLLER_ONLY_SCHEMES = ("DEFAULT", "MODERNALT", "LEGACYALT")

PRIMARY, SECONDARY = 0, 1
SLOT_NAMES = {PRIMARY: "primary", SECONDARY: "secondary"}
# game writes primary/secondary binds slightly differently, copying that
SLOT_DEVICE = {PRIMARY: "DefaultKeyboardAndMouse", SECONDARY: "WindowsApplication"}
BIND_GROUP = "KBM"


def slot_flags(slot):
    return (1, 3, slot)


class SaveFormatError(Exception):
    pass


class Reader:
    def __init__(self, data, pos=0):
        self.d = data
        self.p = pos

    def u8(self):
        if self.p + 1 > len(self.d):
            raise SaveFormatError("Unexpected end of file")
        v = self.d[self.p]
        self.p += 1
        return v

    def u32(self):
        if self.p + 4 > len(self.d):
            raise SaveFormatError("Unexpected end of file")
        v = struct.unpack_from("<I", self.d, self.p)[0]
        self.p += 4
        return v

    def fstring(self):
        if self.p + 4 > len(self.d):
            raise SaveFormatError("Unexpected end of file")
        n = struct.unpack_from("<i", self.d, self.p)[0]
        self.p += 4
        if n == 0:
            return ""
        if n > 0:
            raw = self.d[self.p:self.p + n]
            self.p += n
            if len(raw) != n or raw[-1:] != b"\0":
                raise SaveFormatError("Bad string at offset %d" % (self.p - n))
            return raw[:-1].decode("latin-1")
        n = -n * 2  # utf16
        raw = self.d[self.p:self.p + n]
        self.p += n
        return raw[:-2].decode("utf-16-le")


def fstring_bytes(s):
    if s == "":
        return struct.pack("<i", 0)
    try:
        b = s.encode("ascii") + b"\0"
        return struct.pack("<i", len(b)) + b
    except UnicodeEncodeError:
        b = s.encode("utf-16-le") + b"\0\0"
        return struct.pack("<i", -(len(b) // 2)) + b


class Binding:
    __slots__ = ("action", "key", "device", "group", "flags")

    def __init__(self, action, key, device, group, flags):
        self.action, self.key, self.device, self.group = action, key, device, group
        self.flags = tuple(flags)

    @classmethod
    def new(cls, action, key, slot):
        return cls(action, key, SLOT_DEVICE[slot], BIND_GROUP, slot_flags(slot))

    @property
    def slot(self):
        return self.flags[2]

    def to_bytes(self):
        return (fstring_bytes(self.action) + fstring_bytes(self.key) +
                fstring_bytes(self.device) + fstring_bytes(self.group) +
                struct.pack("<BIB", *self.flags))


class Profile:
    def __init__(self, cls, obj, name, bindings):
        self.cls, self.obj, self.name, self.bindings = cls, obj, name, bindings

    def find(self, action, slot=PRIMARY):
        for b in self.bindings:
            if b.action == action and b.slot == slot:
                return b
        return None

    def set(self, action, slot, key):
        b = self.find(action, slot)
        if b:
            b.key, b.device, b.flags = key, SLOT_DEVICE[slot], slot_flags(slot)
        else:
            self.bindings.append(Binding.new(action, key, slot))

    def clear(self, action, slot=None):
        self.bindings = [b for b in self.bindings
                         if not (b.action == action and (slot is None or b.slot == slot))]

    def to_bytes(self):
        out = fstring_bytes(self.cls) + fstring_bytes(self.obj)
        out += struct.pack("<I", len(self.bindings))
        for b in self.bindings:
            out += b.to_bytes()
        # name comes after the binds for some reason
        out += fstring_bytes(self.name)
        return out


class KeybindSave:

    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self.original = f.read()
        self._parse(self.original)
        if self.to_bytes() != self.original:
            raise SaveFormatError("This file doesn't round-trip cleanly, so editing it isn't safe.")

    def _parse(self, d):
        if d[:4] != b"GVAS":
            raise SaveFormatError("Not an Unreal save file (missing GVAS header).")

        # current scheme
        marker = fstring_bytes(CURRENT_PROFILE_PROP) + fstring_bytes("StrProperty")
        i = d.find(marker)
        if i < 0:
            raise SaveFormatError("Couldn't find the active control scheme setting.")
        tag_end = i + len(marker) + 4
        r = Reader(d, tag_end)
        size = r.u32()
        self._cp_flag = r.u8()
        value_start = r.p
        self.current_profile = r.fstring()
        if r.p - value_start != size:
            raise SaveFormatError("Unexpected layout of the control scheme setting.")
        self._prefix = d[:tag_end]
        value_end = r.p

        # profiles
        cls_marker = fstring_bytes(PROFILE_CLASS)
        j = d.find(cls_marker, value_end)
        if j < 4:
            raise SaveFormatError("Couldn't find any control scheme profiles.")
        count_pos = j - 4
        self._mid = d[value_end:count_pos]
        r = Reader(d, count_pos)
        count = r.u32()
        if not 0 < count < 1000:
            raise SaveFormatError("Unexpected profile count.")
        self.profiles = []
        for _ in range(count):
            cls = r.fstring()
            obj = r.fstring()
            n = r.u32()
            if n > 10000:
                raise SaveFormatError("Unexpected binding count.")
            binds = []
            for _ in range(n):
                action, key, device, group = r.fstring(), r.fstring(), r.fstring(), r.fstring()
                flags = (r.u8(), r.u32(), r.u8())
                binds.append(Binding(action, key, device, group, flags))
            name = r.fstring()
            if cls != PROFILE_CLASS:
                raise SaveFormatError("Unexpected profile type: %r" % cls)
            self.profiles.append(Profile(cls, obj, name, binds))
        self._suffix = d[r.p:]

    def to_bytes(self):
        value = fstring_bytes(self.current_profile)
        out = self._prefix + struct.pack("<I", len(value)) + bytes([self._cp_flag]) + value
        out += self._mid + struct.pack("<I", len(self.profiles))
        for p in self.profiles:
            out += p.to_bytes()
        return out + self._suffix

    def profile(self, name):
        for p in self.profiles:
            if p.name == name:
                return p
        return None

    def scheme_names(self, show_all=False):
        names = [p.name for p in self.profiles if p.name not in INTERNAL_PROFILES]
        if not show_all:
            names = [n for n in SELECTABLE_SCHEMES if n in names]
        if self.current_profile not in names:
            names.append(self.current_profile)
        return names

    def editable_profiles(self, show_all=False):
        names = [p.name for p in self.profiles]
        if show_all:
            return names
        shown = [n for n in SELECTABLE_SCHEMES if n in names]
        if self.current_profile in names and self.current_profile not in shown:
            shown.append(self.current_profile)
        return shown or names

    def sync_temp_profile(self):
        # game keeps a copy of the active scheme here, keep it in sync
        src, tmp = self.profile(self.current_profile), self.profile(TEMP_PROFILE)
        if src and tmp:
            tmp.bindings = copy.deepcopy(src.bindings)

    def save(self, path=None, backup=True):
        path = path or self.path
        data = self.to_bytes()
        # sanity check before writing anything
        check = KeybindSave.__new__(KeybindSave)
        check._parse(data)
        if check.to_bytes() != data:
            raise SaveFormatError("Verification failed; nothing was written.")
        backup_path = None
        if backup and os.path.exists(path):
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_path = "%s.bak-%s" % (path, stamp)
            shutil.copy2(path, backup_path)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        self.path, self.original = path, data
        return backup_path


# ---- actions ----
# (section, name in game menu, internal IA_ name, default keys)
# no IA_ name yet = can't edit it, only shows the default
# defaults are {scheme: (primary, secondary)}, "*" means both sprint styles
# to find a missing IA_ name: rebind it in game then run --dump

_ = "None"
ACTIONS = [
    ("Movement", "Forward",                        None,               {"*": ("W", _)}),
    ("Movement", "Backward",                       None,               {"*": ("S", _)}),
    ("Movement", "Left",                           None,               {"*": ("A", _)}),
    ("Movement", "Right",                          None,               {"*": ("D", _)}),
    ("Movement", "Sprint",                         "IA_RoadieRun",     {"LEGACY": ("LeftShift", _)}),
    ("Movement", "Slide (while sprinting)",        "IA_Slide",         {}),
    ("Movement", "Evade",                          "IA_Evade",         {"LEGACY": ("SpaceBar", _)}),
    ("Movement", "Take Cover",                     "IA_Cover",         {"LEGACY": ("SpaceBar", _)}),
    ("Movement", "Jump",                           "IA_Jump",          {}),
    ("Movement", "Mantle / Climb",                 "IA_Mantle",        {}),
    ("Movement", "Cover / Crouch",                 "IA_CoverCrouch",   {}),
    ("Action",   "Shoot Weapon / Throw Equipment", None,               {"*": ("LeftMouseButton", _)}),
    ("Action",   "Aim",                            None,               {"*": ("RightMouseButton", _)}),
    ("Action",   "Reload",                         None,               {"*": ("R", _)}),
    ("Action",   "Melee",                          None,               {"*": ("F", _)}),
    ("Action",   "Pause",                          None,               {"*": ("Escape", _)}),
    ("Action",   "Map / Scoreboard",               None,               {"*": ("Tab", "RightAlt")}),
    ("Action",   "Heat Vent",                      None,               {"*": ("R", _)}),
    ("Combat",   "Scope Toggle (while aiming)",    None,               {"*": ("MiddleMouseButton", _)}),
    ("Combat",   "Struggle",                       None,               {"*": ("F", _)}),
    ("Combat",   "Mark Target (while aiming)",     "IA_Ping",          {"*": ("Q", _)}),
    ("Combat",   "Inventory 1",                    None,               {"*": ("One", _)}),
    ("Combat",   "Inventory 2",                    None,               {"*": ("Two", _)}),
    ("Combat",   "Inventory 3",                    None,               {"*": ("Three", _)}),
    ("Combat",   "Inventory 4",                    None,               {"*": ("Four", "G")}),
    ("Combat",   "Cycle Inventory Equipment",      None,               {"*": ("Four", "G")}),
    ("Combat",   "Tactical Gear",                  "IA_SpecialGear",   {"*": ("C", _)}),
    ("Combat",   "Tac-Com / Objectives",           None,               {"*": ("LeftAlt", _)}),
    ("Camera",   "Swap Shoulder Camera",           None,               {"*": ("LeftShift", _)}),
    ("Interact", "Interaction Requested",          None,               {"*": ("E", _)}),
    ("Turret",   "Exit",                           None,               {"*": ("E", _)}),
    ("Turret",   "Detach",                         None,               {"*": ("F", _)}),
    ("Other",    "Voice chat push-to-talk",        "IA_UI_VOIP_PushToTalk", {}),
    ("Other",    "Look acceleration",              "IA_LookAxis_Acceleration", {}),
]
del _

ACTION_LABELS = {a: label for _s, label, a, _d in ACTIONS if a}


def default_key(action_row, scheme, slot):
    defaults = action_row[3]
    pair = defaults.get(scheme) or defaults.get("*")
    if pair:
        return pair[slot]
    # pretty much every secondary is empty by default
    return "None" if slot == SECONDARY else None


def action_label(a):
    return ACTION_LABELS.get(a, a)


def all_rows(profile):
    rows = list(ACTIONS)
    known = {r[2] for r in ACTIONS if r[2]}
    for b in profile.bindings if profile else []:
        if b.action not in known:
            known.add(b.action)
            rows.append(("Other", b.action, b.action, {}))
    return rows


def effective(profile, row, scheme, slot):
    # returns (key, changed)
    action = row[2]
    b = profile.find(action, slot) if (profile and action) else None
    if b:
        return b.key, True
    return default_key(row, scheme, slot), False


# ---- key names ----

KEYS = [("None", "(no binding)"),
        ("LeftMouseButton", "Left mouse"), ("RightMouseButton", "Right mouse"),
        ("MiddleMouseButton", "Middle mouse"), ("ThumbMouseButton", "Mouse 4 (back)"),
        ("ThumbMouseButton2", "Mouse 5 (forward)"),
        ("MouseScrollUp", "Mouse wheel up"), ("MouseScrollDown", "Mouse wheel down"),
        ("SpaceBar", "Space"), ("LeftShift", "Left Shift"), ("RightShift", "Right Shift"),
        ("LeftControl", "Left Ctrl"), ("RightControl", "Right Ctrl"),
        ("LeftAlt", "Left Alt"), ("RightAlt", "Right Alt"), ("Tab", "Tab"),
        ("CapsLock", "Caps Lock"), ("Enter", "Enter"), ("BackSpace", "Backspace"),
        ("Escape", "Esc")]
KEYS += [(c, c) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
_DIGITS = ["Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"]
KEYS += [(_DIGITS[i], str(i)) for i in range(1, 10)] + [("Zero", "0")]
KEYS += [("F%d" % i, "F%d" % i) for i in range(1, 13)]
KEYS += [("Tilde", "` (backtick)"), ("Hyphen", "-"), ("Equals", "="),
         ("LeftBracket", "["), ("RightBracket", "]"), ("Backslash", "\\"),
         ("Semicolon", ";"), ("Apostrophe", "'"), ("Comma", ","), ("Period", "."),
         ("Slash", "/"), ("Insert", "Insert"), ("Delete", "Delete"), ("Home", "Home"),
         ("End", "End"), ("PageUp", "Page Up"), ("PageDown", "Page Down"),
         ("Up", "Arrow Up"), ("Down", "Arrow Down"), ("Left", "Arrow Left"),
         ("Right", "Arrow Right")]
KEYS += [("NumPad" + _DIGITS[i], "Numpad %d" % i) for i in range(10)]
KEYS += [("Multiply", "Numpad *"), ("Add", "Numpad +"), ("Subtract", "Numpad -"),
         ("Decimal", "Numpad ."), ("Divide", "Numpad /")]
KEY_LABEL = dict(KEYS)
LABEL_KEY = {v: k for k, v in KEYS}

TK_KEYSYMS = {
    "space": "SpaceBar", "Shift_L": "LeftShift", "Shift_R": "RightShift",
    "Control_L": "LeftControl", "Control_R": "RightControl", "Alt_L": "LeftAlt",
    "Alt_R": "RightAlt", "Tab": "Tab", "Caps_Lock": "CapsLock", "Return": "Enter",
    "BackSpace": "BackSpace", "Delete": "Delete", "Insert": "Insert", "Home": "Home",
    "End": "End", "Prior": "PageUp", "Next": "PageDown", "Up": "Up", "Down": "Down",
    "Left": "Left", "Right": "Right", "grave": "Tilde", "minus": "Hyphen",
    "equal": "Equals", "bracketleft": "LeftBracket", "bracketright": "RightBracket",
    "backslash": "Backslash", "semicolon": "Semicolon", "apostrophe": "Apostrophe",
    "comma": "Comma", "period": "Period", "slash": "Slash",
    "KP_Multiply": "Multiply", "KP_Add": "Add", "KP_Subtract": "Subtract",
    "KP_Decimal": "Decimal", "KP_Divide": "Divide", "KP_Enter": "Enter",
}


def key_label(k):
    if k is None:
        return "?"
    return KEY_LABEL.get(k, k)


def tk_event_to_key(ev):
    ks = ev.keysym
    # numpad with numlock on
    if sys.platform == "win32" and 96 <= ev.keycode <= 105:
        return "NumPad" + _DIGITS[ev.keycode - 96]
    if ks.startswith("KP_") and ks[3:].isdigit():
        return "NumPad" + _DIGITS[int(ks[3:])]
    if len(ks) == 1 and ks.isalpha():
        return ks.upper()
    if len(ks) == 1 and ks.isdigit():
        return _DIGITS[int(ks)]
    if ks.startswith("F") and ks[1:].isdigit():
        return ks
    return TK_KEYSYMS.get(ks)


def mouse_event_to_key(num):
    if num == 1:
        return "LeftMouseButton"
    if num == 2:
        return "MiddleMouseButton"
    if num == 3:
        return "RightMouseButton"
    if sys.platform == "win32":
        return {4: "ThumbMouseButton", 5: "ThumbMouseButton2"}.get(num)
    return {8: "ThumbMouseButton", 9: "ThumbMouseButton2"}.get(num)


def find_default_saves():
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return []
    pattern = os.path.join(base, "Microsoft", "Gears of War E-Day", "Saves", "*",
                           "EnhancedInputUserSettings.sav")
    return sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)


# ---- text dump ----

def dump(path):
    s = KeybindSave(path)
    print("File:", path)
    print("Keyboard sprint style:", s.current_profile)
    for name in s.scheme_names():
        p = s.profile(name)
        if not p:
            continue
        tag = "  <- active" if name == s.current_profile else ""
        print("\n[%s]%s" % (name, tag))
        print("    %-32s %-20s %-20s %s" % ("Action", "Primary", "Secondary", "Internal name"))
        section = None
        for row in all_rows(p):
            if row[0] != section:
                section = row[0]
                print("  %s" % section.upper())
            cells = []
            for slot in (PRIMARY, SECONDARY):
                k, changed = effective(p, row, name, slot)
                cells.append(key_label(k) + (" *" if changed else ""))
            print("    %-32s %-20s %-20s %s" % (row[1], cells[0], cells[1], row[2] or "(not known yet)"))
    print("\n* = changed from the default.  ? = default not known yet.")


# ---- gui ----

# about window logo (png, already 50% transparent)
LOGO_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAHgAAAB4CAYAAAA5ZDbSAABgF0lEQVR42u39f5Bk13XfCX7ufTdvvnr16lV2dnZ1daHQaBSb"
    "zRYEQvwlmII4FKngaGTvWJbtJUczYVMeeVbyrjQ7nqFlxzq8W2g7QmOHPLZjTGtGHFn+MWt7VnRIWo0sjy3aAmgtTVEgSEEg"
    "3Gw1G41ioVCozs7Oynr16uXN++7dP+7NH9UAJVIiKXp2C5FRheqszJfv3PPre77nHMG/H19iGwTANXCL/7C9vS3Hz/yr1fHk"
    "+Gzj5LpvyBrceSFY9l40AtfziNQ7PDiQCiUVrVSLtKVrnaZ93U6TJFHHQrVf1Sqpbtx8ef+l69fv/uJLLx0KIV7zfgDXrl3z"
    "gP+Gv3Hf6EK9Fm7i7Eb+4Nvf3hKt0dpEti+qRF7E+Z4XdIT3SwghPUgpfOPBexACYb1zs793zs2+C4loqbZqa+310rJopTpJ"
    "25mTQrmT6uTECzk8e/ZMv9Pt7igpd8b944Mf+shHJtPX8t6LJ598UnwjC1t8Awt2pjn/5eOXiyOpLifIqwguCDjjJVp64RCi"
    "8d43SNHgQEi8WPhc3nuxKFgk8ZUdDmi89+CQTpIo6ROphNaapSxPEpUm1vqktZTJc2fOmpWV7B7Cv+JE63pdNzf/q7/9t0eL"
    "mv3ktWtefIMJ+htGwEHbpj/C+x95JM/S8ZtIxFUQl2SiVqQEiZgIKa0AL5E0OCGEWBDiTIKnvtxrfheee0rwyPDNORyORCR+"
    "Kc28Q9E4LYqVVbW23mutrq4glT7ycNsgrrvRyed/5Cd+opx+jnhj/f9fwHPBzm7Gn3z71kUQbxWIq16IbgKORJpEiGb6fO+9"
    "kFLOXmPx59cIbkFgp8Qrp0fhvgMxlbcM4nYOtNakaUbjhfdon2YpZ4pOsrzS0a20LZXUA9uY6zduvviZ//4Xf3HnS322/58S"
    "8H0aK/70Ox95g20m72pc80bhfZJIZYQUFsA1PqpWEKZSCinlTDTOuVO+9X6ZSTn/XzcT+P3aPP9RLh4VN9durTVSqamFRyYt"
    "p9OMNM2VR+i7d/tNIvVv9dbWf/WvfvSjX5h+NhYs09f7K/l98rHyvfED//C73/zmdzy49oHGN/9B0zTnvGAsvJgghHfOSds0"
    "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"
    "w2POny2ozYRbt19lOXEsJQlSKU48rGWaW68e8dGPv8BSIpkYS6IE5XHNUTWm3dKIRIQcN5rrREpwTXARPrgamSQhbmiaGRbj"
    "cNNUHGNOEDKhd67HwfiE5PCE5XSJY2sYO+E38s6V9114+HPX7uxWPtCLf88aLJ9++mn/h7/tXe+fODZffWWn9uO7MkkS8qUV"
    "PA4hHInzJInn4pU30NJtaAzCwb17hyQx7ZEI2lqy1A5aY23DSp6SJIKJMbPS3fT7NM2dcqnkfamvFKE4LxOJEmJ28ybWYm0z"
    "Q7SklzTeYUyDMRY3aRgOS/b27vLiS/ss50u8422XaZxnd6/P1toKD6ytUqzmSN1ivUg5aRzPvDhkqa3pnslYO5MhnGN5SbOc"
    "RbTKA8KFmrP3KNUiabUCVu4aPIIkgUSpYL6di4ZJICLg0viG8bgmbaWcOX+W3cGI3DiWdFsA1gjyVkud+fidl3+DL0OLky/H"
    "NP/khz70rcb595Yn1fHRqy8mrcTS0hlat2i8oS3Dybvw8CU6Z3uIZsK4PmE5z6mrMfVRRaLmAX6aapa0pDaWZuLI0hZpqjmp"
    "Teg4ECImTcG0Sukj4MHs+/T0T4sVIf1pYv3WI+eQN2PTcFTWICWb51bBw+jeiHE95tz5MzzyTQ9yeHhENSx5y+ULbG32SLOU"
    "9lKbjdUlTiaOZ24NaGxDnibUxnJmNcMag0hkiBV8yL2dnyJrDiE8SiQkLR1MeETfEklQAg+OJuAECDweIROaZkI9HpMvL5Od"
    "W2Vn/y4doZkg5KsnpenkK5vv6T04/Mt3dl/+nUz1bydg8dTTT3NuezvnePyfeTx377zqXX1HqFbCUnsJ7xtaStKcVBTdVbbe"
    "9Aa8c4zrGiEUUguSRHJvMEQn0VyJEFQsZSkq8fjGYZqGRHiKfBkvPONJE4Qbg6PXu35/HzTlRNR2B4kMZLjxeIIxFvCs5Blv"
    "uNBDS8Hd/iFZu83G5jnWNrrY8ZgHiiXe+qYNumdXEFLSUgm9vMWgMnxu94iqGqOkoJ0ITmqDarWQMbBMZIumsSQyiVZDhAJL"
    "vMQEQbulSWTCxBqapkG1JK0pmuc9Dk/ip5ZOMmkm1Kams1KgzuTsvnLAhbTANI5748qdWy0e+uNvvPSZv/DSS+a3S53kb6O9"
    "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"
)

PREFS_PATH = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                          "gears_eday_keybinds.json")

PALETTES = {
    "light": {"bg": None, "fg": "#1a1a1a", "muted": "#666666", "field": "#ffffff",
              "select": "#cfe0f7", "select_fg": "#1a1a1a", "border": "#b5b5b5",
              "changed": "#b3261e", "locked": "#8a8a8a", "link": "#1a6fd6",
              "cap_idle": "#f3f3f3", "cap_armed": "#fff4cc", "cap_done": "#e6f4e6",
              "cap_fg": "#1a1a1a"},
    "dark": {"bg": "#1e1f22", "fg": "#e6e6e6", "muted": "#9aa0a8", "field": "#2b2d31",
             "select": "#3d5a80", "select_fg": "#ffffff", "border": "#3a3c42",
             "changed": "#ff7b72", "locked": "#7d828a", "link": "#6cb6ff",
             "cap_idle": "#2b2d31", "cap_armed": "#4a4128", "cap_done": "#24402c",
             "cap_fg": "#e6e6e6"},
}


def load_prefs():
    try:
        import json
        with open(PREFS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_prefs(prefs):
    try:
        import json
        with open(PREFS_PATH, "w", encoding="utf-8") as f:
            json.dump(prefs, f)
    except Exception:
        pass

def run_gui(initial=None):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    root = tk.Tk()
    root.title("Gears of War: E-Day keybind editor")
    root.geometry("980x640")
    root.minsize(820, 480)

    style = ttk.Style(root)
    light_theme = style.theme_use()
    prefs = load_prefs()
    dark_var = tk.BooleanVar(value=bool(prefs.get("dark_mode")))
    pal = {}
    tree_ref = []

    def apply_theme():
        dark = dark_var.get()
        pal.clear()
        pal.update(PALETTES["dark" if dark else "light"])
        if dark:
            style.theme_use("clam")
            bg, fg, field = pal["bg"], pal["fg"], pal["field"]
            style.configure(".", background=bg, foreground=fg, fieldbackground=field,
                            bordercolor=pal["border"], lightcolor=bg, darkcolor=bg,
                            troughcolor=field, selectbackground=pal["select"],
                            selectforeground=pal["select_fg"], insertcolor=fg)
            style.map(".", background=[("disabled", bg)], foreground=[("disabled", pal["muted"])])
            style.configure("TButton", background=field, bordercolor=pal["border"])
            style.map("TButton", background=[("pressed", pal["select"]), ("active", "#35373c")])
            for w in ("TCheckbutton", "TRadiobutton"):
                style.configure(w, background=bg, indicatorbackground=field,
                                indicatorforeground=fg)
                style.map(w, background=[("active", bg)],
                          indicatorbackground=[("selected", pal["select"])])
            style.configure("TCombobox", fieldbackground=field, background=field,
                            arrowcolor=fg, foreground=fg)
            style.map("TCombobox", fieldbackground=[("readonly", field)],
                      foreground=[("readonly", fg)], background=[("readonly", field)],
                      selectbackground=[("readonly", field)],
                      selectforeground=[("readonly", fg)])
            style.configure("TEntry", fieldbackground=field, foreground=fg)
            style.configure("TLabelframe", background=bg, bordercolor=pal["border"])
            style.configure("TLabelframe.Label", background=bg, foreground=fg)
            style.configure("Treeview", background=field, fieldbackground=field, foreground=fg,
                            bordercolor=pal["border"])
            style.map("Treeview", background=[("selected", pal["select"])],
                      foreground=[("selected", pal["select_fg"])])
            style.configure("Treeview.Heading", background="#2f3136", foreground=fg,
                            bordercolor=pal["border"])
            style.map("Treeview.Heading", background=[("active", "#35373c")])
            style.configure("TScrollbar", background=field, arrowcolor=fg,
                            bordercolor=pal["border"], troughcolor=bg)
        else:
            style.theme_use(light_theme)
            pal["bg"] = style.lookup("TFrame", "background") or "#f0f0f0"
            pal["field"] = style.lookup("TEntry", "fieldbackground") or "#ffffff"
        root.configure(background=pal["bg"])
        style.configure("Muted.TLabel", foreground=pal["muted"])
        for opt, key in (("background", "field"), ("foreground", "fg"),
                         ("selectBackground", "select"), ("selectForeground", "select_fg")):
            root.option_add("*TCombobox*Listbox." + opt, pal[key])
        if tree_ref:
            tree_ref[0].tag_configure("changed", foreground=pal["changed"])
            tree_ref[0].tag_configure("locked", foreground=pal["locked"])

    def toggle_dark():
        apply_theme()
        prefs["dark_mode"] = dark_var.get()
        save_prefs(prefs)

    apply_theme()

    state = {"save": None, "dirty": False, "rows": {}}
    path_var = tk.StringVar(value="No file loaded")
    active_var = tk.StringVar()
    edit_var = tk.StringVar()
    mirror_var = tk.BooleanVar(value=True)
    show_all_var = tk.BooleanVar(value=False)
    changed_only_var = tk.BooleanVar(value=False)
    status_var = tk.StringVar(value="Open your EnhancedInputUserSettings.sav to begin.")

    # about window
    def show_about():
        import webbrowser
        win = tk.Toplevel(root)
        win.title("About")
        win.transient(root)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=(28, 20))
        frm.pack()
        win.configure(background=pal["bg"])
        try:
            logo = tk.PhotoImage(data=LOGO_PNG_B64)
            win._logo = logo  # tk drops the image otherwise
            ttk.Label(frm, image=logo).pack()
            credit = ttk.Frame(frm)
            credit.pack(pady=(3, 10))
            ttk.Label(credit, text="Art by", style="Muted.TLabel",
                      font=("TkDefaultFont", 8)).pack(side="left")
            art = tk.Label(credit, text="https://x.com/OlchaSArt", fg=pal["muted"], bg=pal["bg"],
                           cursor="hand2", font=("TkDefaultFont", 8, "underline"))
            art.pack(side="left", padx=(3, 0))
            art.bind("<Button-1>", lambda e: webbrowser.open("https://x.com/OlchaSArt"))
        except tk.TclError:
            pass
        ttk.Label(frm, text="Gears of War: E-Day keybind editor",
                  font=("TkDefaultFont", 11, "bold")).pack()
        ttk.Label(frm, text="Created by Ahri").pack(pady=(10, 4))
        for text, url in (("twitter.com/Ahrisss", "https://twitter.com/Ahrisss"),
                          ("github.com/Ahris/gears-eday-keybind-editor",
                           "https://github.com/Ahris/gears-eday-keybind-editor/")):
            link = tk.Label(frm, text=text, fg=pal["link"], bg=pal["bg"], cursor="hand2",
                            font=("TkDefaultFont", 10, "underline"))
            link.pack(pady=1)
            link.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))
        ttk.Button(frm, text="Close", command=win.destroy).pack(pady=(14, 0))
        win.bind("<Escape>", lambda e: win.destroy())
        win.update_idletasks()
        x = root.winfo_rootx() + (root.winfo_width() - win.winfo_width()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - win.winfo_height()) // 3
        win.geometry("+%d+%d" % (max(x, 0), max(y, 0)))
        win.grab_set()

    menubar = tk.Menu(root, tearoff=0)
    menubar.add_command(label="About", command=show_about)
    root.config(menu=menubar)

    # layout
    top = ttk.Frame(root, padding=8)
    top.pack(fill="x")
    ttk.Button(top, text="Open...", command=lambda: open_file()).pack(side="left")
    ttk.Button(top, text="Reload", command=lambda: reload_file()).pack(side="left", padx=4)
    ttk.Checkbutton(top, text="Dark mode", variable=dark_var,
                    command=toggle_dark).pack(side="right")
    ttk.Label(top, textvariable=path_var, style="Muted.TLabel").pack(side="left", padx=8)

    schemes = ttk.LabelFrame(root, text="Control scheme", padding=8)
    schemes.pack(fill="x", padx=8)
    ttk.Label(schemes, text="Keyboard sprint style:").grid(row=0, column=0, sticky="w")
    active_box = ttk.Combobox(schemes, textvariable=active_var, state="readonly", width=24)
    active_box.grid(row=0, column=1, sticky="w", padx=6)
    ttk.Label(schemes, text="Editing binds for:").grid(row=0, column=2, sticky="w", padx=(20, 0))
    edit_box = ttk.Combobox(schemes, textvariable=edit_var, state="readonly", width=30)
    edit_box.grid(row=0, column=3, sticky="w", padx=6)
    ttk.Checkbutton(schemes, variable=mirror_var,
                    text="Also copy the active scheme into the game's temporary profile when "
                         "saving (recommended)").grid(row=1, column=0, columnspan=4, sticky="w",
                                                      pady=(6, 0))
    ttk.Checkbutton(schemes, variable=show_all_var,
                    text="Also show controller schemes (DEFAULT, MODERNALT, LEGACYALT) and "
                         "internal profiles",
                    command=lambda: fill_scheme_lists()).grid(row=2, column=0, columnspan=4,
                                                              sticky="w", pady=(2, 0))

    mid = ttk.Frame(root, padding=8)
    mid.pack(fill="both", expand=True)
    tree = ttk.Treeview(mid, columns=("primary", "secondary", "id"), show="tree headings",
                        selectmode="browse")
    tree.heading("#0", text="Action")
    tree.heading("primary", text="Primary")
    tree.heading("secondary", text="Secondary")
    tree.heading("id", text="Internal name")
    tree.column("#0", width=260)
    tree.column("primary", width=150)
    tree.column("secondary", width=150)
    tree.column("id", width=190)
    tree.tag_configure("section", font=("TkDefaultFont", 9, "bold"))
    tree.tag_configure("changed", foreground=pal["changed"])
    tree.tag_configure("locked", foreground=pal["locked"])
    tree_ref.append(tree)
    sb = ttk.Scrollbar(mid, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="left", fill="y")

    btns = ttk.Frame(mid, padding=(8, 0))
    btns.pack(side="left", fill="y")
    for text, cmd in (("Change keys...", lambda: edit_row(selected_row())),
                      ("Reset to default", lambda: reset_row()),
                      ("Reset all...", lambda: reset_all()),
                      ("Add other action...", lambda: edit_row(None))):
        ttk.Button(btns, text=text, command=cmd, width=20).pack(pady=2, fill="x")
    ttk.Checkbutton(btns, text="Only show changed", variable=changed_only_var,
                    command=lambda: refresh_tree()).pack(anchor="w", pady=(10, 0))
    ttk.Label(btns, text="Red = changed.\nGrey = view only (internal\nname not known yet).\n"
                         "? = default not known yet.\n\nDouble-click a row to\nchange its keys.",
              style="Muted.TLabel", justify="left").pack(anchor="w", pady=(10, 0))

    bottom = ttk.Frame(root, padding=8)
    bottom.pack(fill="x")
    ttk.Button(bottom, text="Save", command=lambda: save()).pack(side="right")
    ttk.Button(bottom, text="Save as...", command=lambda: save(as_new=True)).pack(side="right", padx=4)
    ttk.Label(bottom, textvariable=status_var).pack(side="left")

    tree.bind("<Double-1>", lambda e: edit_row(selected_row()))

    def current_profile():
        s = state["save"]
        return s.profile(edit_var.get()) if s else None

    def selected_row():
        sel = tree.selection()
        return state["rows"].get(sel[0]) if sel else None

    def mark_dirty(msg):
        state["dirty"] = True
        status_var.set(msg + "  (unsaved)")

    def refresh_tree():
        sel = tree.selection()
        tree.delete(*tree.get_children())
        state["rows"] = {}
        p = current_profile()
        if not p:
            return
        scheme = p.name
        sections = {}
        for i, row in enumerate(all_rows(p)):
            keys, changed_any = [], False
            for slot in (PRIMARY, SECONDARY):
                k, changed = effective(p, row, scheme, slot)
                changed_any |= changed
                keys.append(key_label(k))
            if changed_only_var.get() and not changed_any:
                continue
            sec = row[0]
            if sec not in sections:
                sections[sec] = tree.insert("", "end", text=sec.upper(), open=True,
                                            tags=("section",))
            tag = "changed" if changed_any else ("locked" if not row[2] else "")
            iid = "r%d" % i
            tree.insert(sections[sec], "end", iid=iid, text=row[1],
                        values=(keys[0], keys[1], row[2] or "not known yet"), tags=(tag,))
            state["rows"][iid] = row
        if sel and tree.exists(sel[0]):
            tree.selection_set(sel[0])
            tree.see(sel[0])

    def fill_scheme_lists():
        s = state["save"]
        if not s:
            return
        show_all = show_all_var.get()
        active_box["values"] = s.scheme_names(show_all)
        edit_names = s.editable_profiles(show_all)
        edit_box["values"] = edit_names
        if edit_var.get() not in edit_names:
            edit_var.set(s.current_profile if s.current_profile in edit_names else edit_names[0])
        refresh_tree()

    def load(path):
        try:
            s = KeybindSave(path)
        except (OSError, SaveFormatError) as e:
            messagebox.showerror("Couldn't open file", str(e))
            return
        state["save"], state["dirty"] = s, False
        path_var.set(path)
        active_var.set(s.current_profile)
        edit_var.set(s.current_profile if s.profile(s.current_profile) else s.profiles[0].name)
        fill_scheme_lists()
        status_var.set("Loaded. Keyboard sprint style: %s" % s.current_profile)

    def confirm_discard():
        return not state["dirty"] or messagebox.askyesno(
            "Unsaved changes", "Discard your unsaved changes?")

    def open_file():
        if not confirm_discard():
            return
        found = find_default_saves()
        initdir = os.path.dirname(found[0]) if found else None
        path = filedialog.askopenfilename(
            title="Open EnhancedInputUserSettings.sav", initialdir=initdir,
            filetypes=[("Unreal save", "*.sav"), ("All files", "*.*")])
        if path:
            load(path)

    def reload_file():
        if state["save"] and confirm_discard():
            load(state["save"].path)

    def on_active_change(_e=None):
        s = state["save"]
        if s and active_var.get() != s.current_profile:
            s.current_profile = active_var.get()
            edit_var.set(s.current_profile)
            refresh_tree()
            mark_dirty("Sprint style set to %s. Pick the same one in the game's menu too, "
                       "as the game also stores it in TCSettingsSavepoint.sav." % s.current_profile)

    active_box.bind("<<ComboboxSelected>>", on_active_change)
    edit_box.bind("<<ComboboxSelected>>", lambda e: refresh_tree())

    def reset_row():
        row, p = selected_row(), current_profile()
        if not (row and p):
            return
        if not row[2]:
            messagebox.showinfo("Not editable yet",
                                "This action's internal name isn't known yet, so the tool "
                                "can't change it. Change it in the game's menu instead.")
            return
        p.clear(row[2])
        refresh_tree()
        mark_dirty("%s reset to the game's default." % row[1])

    def reset_all():
        p = current_profile()
        if p and messagebox.askyesno(
                "Reset all", "Remove every change in %s so all actions use the game's "
                             "default keys?" % p.name):
            p.bindings = []
            refresh_tree()
            mark_dirty("All binds in %s reset to default." % p.name)

    def edit_row(row):
        p, s = current_profile(), state["save"]
        if not p:
            return
        if row is not None and not row[2]:
            messagebox.showinfo(
                "Not editable yet",
                "The internal name for \"%s\" isn't known yet, so the tool can't change it.\n\n"
                "Change it once in the game's menu, then open the file here: it will show up "
                "with its internal name." % row[1])
            return
        scheme = p.name

        dlg = tk.Toplevel(root)
        dlg.title("Change keys" if row else "Add other action")
        dlg.transient(root)
        dlg.resizable(False, False)
        dlg.configure(background=pal["bg"])
        frm = ttk.Frame(dlg, padding=12)
        frm.pack()

        if row:
            ttk.Label(frm, text=row[1], font=("TkDefaultFont", 11, "bold")).grid(
                row=0, column=0, columnspan=3, sticky="w")
            ttk.Label(frm, text=row[2], style="Muted.TLabel").grid(row=1, column=0, columnspan=3,
                                                                 sticky="w")
            action_var = tk.StringVar(value=row[2])
        else:
            ttk.Label(frm, text="Internal name:").grid(row=0, column=0, sticky="w")
            action_var = tk.StringVar(value="IA_")
            ttk.Entry(frm, textvariable=action_var, width=34).grid(row=0, column=1, columnspan=2,
                                                                  sticky="w")
            ttk.Label(frm, text="Only for actions not already in the list. Names the game "
                                "doesn't use are ignored.", style="Muted.TLabel",
                      wraplength=360).grid(row=1, column=0, columnspan=3, sticky="w")

        key_vars, default_lbls = {}, {}
        for r, slot in ((2, PRIMARY), (3, SECONDARY)):
            ttk.Label(frm, text=SLOT_NAMES[slot].capitalize() + ":").grid(
                row=r, column=0, sticky="w", pady=(10 if slot == PRIMARY else 4, 0))
            cur = effective(p, row, scheme, slot)[0] if row else "None"
            v = tk.StringVar(value=key_label(cur) if cur else "")
            key_vars[slot] = v
            ttk.Combobox(frm, textvariable=v, state="readonly", values=[lbl for _k, lbl in KEYS],
                         width=22).grid(row=r, column=1, sticky="w",
                                        pady=(10 if slot == PRIMARY else 4, 0))
            d = default_key(row, scheme, slot) if row else None
            default_lbls[slot] = d
            ttk.Button(frm, text="Use default: %s" % ("none" if d == "None" else key_label(d)),
                       command=lambda s_=slot: key_vars[s_].set(
                           key_label(default_lbls[s_]) if default_lbls[s_] else "")).grid(
                row=r, column=2, sticky="w", padx=6, pady=(10 if slot == PRIMARY else 4, 0))

        target = tk.IntVar(value=PRIMARY)
        cap = ttk.Frame(frm)
        cap.grid(row=4, column=0, columnspan=3, sticky="we", pady=(10, 0))
        ttk.Label(cap, text="Capture into:").pack(side="left")
        ttk.Radiobutton(cap, text="Primary", variable=target, value=PRIMARY).pack(side="left")
        ttk.Radiobutton(cap, text="Secondary", variable=target, value=SECONDARY).pack(side="left")
        capture = tk.Label(frm, text="Click here, then press a key or mouse button",
                           relief="groove", padx=8, pady=10, bg=pal["cap_idle"],
                           fg=pal["cap_fg"], cursor="hand2")
        capture.grid(row=5, column=0, columnspan=3, sticky="we", pady=6)

        def set_key(k):
            if k:
                key_vars[target.get()].set(key_label(k))
                capture.configure(text="%s set to %s" % (SLOT_NAMES[target.get()].capitalize(),
                                                         key_label(k)), bg=pal["cap_done"])
            stop_capture()

        def on_key(ev):
            if ev.keysym == "Escape":
                stop_capture()
                capture.configure(text="Cancelled. Click to try again.", bg=pal["cap_idle"])
                return "break"
            k = tk_event_to_key(ev)
            if k:
                set_key(k)
            return "break"

        def start_capture(_e=None):
            capture.configure(text="Press a key or mouse button...  (Esc cancels)", bg=pal["cap_armed"])
            capture.unbind("<Button-1>")

            def arm():
                dlg.bind("<KeyPress>", on_key)
                capture.bind("<ButtonPress>", lambda e: (set_key(mouse_event_to_key(e.num)), "break")[1])
                capture.bind("<MouseWheel>", lambda e: (set_key(
                    "MouseScrollUp" if e.delta > 0 else "MouseScrollDown"), "break")[1])
                capture.focus_set()
            dlg.after(150, arm)

        def stop_capture():
            dlg.unbind("<KeyPress>")
            capture.unbind("<ButtonPress>")
            capture.unbind("<MouseWheel>")
            capture.bind("<Button-1>", start_capture)

        capture.bind("<Button-1>", start_capture)

        def ok():
            act = action_var.get().strip()
            if not act or act == "IA_":
                messagebox.showwarning("Internal name", "Enter the action's internal name.",
                                       parent=dlg)
                return
            the_row = row or ("Other", act, act, {})
            changes = []
            for slot in (PRIMARY, SECONDARY):
                lbl = key_vars[slot].get()
                if lbl not in LABEL_KEY:
                    continue
                key = LABEL_KEY[lbl]
                d = default_key(the_row, scheme, slot)
                if row is None and slot == SECONDARY and key == "None":
                    continue
                if d is not None and key == d:
                    p.clear(act, slot)  # game doesn't store defaults either
                else:
                    p.set(act, slot, key)
                changes.append("%s %s" % (SLOT_NAMES[slot], key_label(key)))
            refresh_tree()
            mark_dirty("%s: %s (%s)." % (the_row[1], ", ".join(changes) or "no change", scheme))
            dlg.destroy()

        row_ = ttk.Frame(frm)
        row_.grid(row=6, column=0, columnspan=3, sticky="e", pady=(6, 0))
        ttk.Button(row_, text="Cancel", command=dlg.destroy).pack(side="right")
        ttk.Button(row_, text="OK", command=ok).pack(side="right", padx=4)
        dlg.bind("<Return>", lambda e: ok())
        dlg.grab_set()
        dlg.wait_window()

    def save(as_new=False):
        s = state["save"]
        if not s:
            return
        path = s.path
        if as_new:
            path = filedialog.asksaveasfilename(
                title="Save as", defaultextension=".sav",
                initialfile="EnhancedInputUserSettings.sav",
                filetypes=[("Unreal save", "*.sav")])
            if not path:
                return
        if mirror_var.get():
            s.sync_temp_profile()
        try:
            bak = s.save(path)
        except (OSError, SaveFormatError) as e:
            messagebox.showerror("Save failed", str(e))
            return
        state["dirty"] = False
        path_var.set(path)
        refresh_tree()
        msg = "Saved." + ("  Backup: %s" % os.path.basename(bak) if bak else "")
        status_var.set(msg)
        messagebox.showinfo(
            "Saved",
            msg + "\n\nBefore launching: make sure the game is closed and Steam Cloud is "
                  "off for it, or Steam may put the old file back.")

    def on_close():
        if confirm_discard():
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    if initial:
        load(initial)
    else:
        found = find_default_saves()
        if found:
            load(found[0])

    root.mainloop()


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--dump":
        if len(args) < 2:
            found = find_default_saves()
            if not found:
                sys.exit("Usage: --dump FILE.sav")
            args.append(found[0])
        try:
            dump(args[1])
        except (OSError, SaveFormatError) as e:
            sys.exit("Error: %s" % e)
    else:
        run_gui(args[0] if args else None)
