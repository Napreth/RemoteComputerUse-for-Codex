"""RDP keyboard scan-code mapping."""

SCANCODES: dict[str, tuple[int, bool]] = {
    "escape": (0x01, False),
    "esc": (0x01, False),
    "backspace": (0x0E, False),
    "tab": (0x0F, False),
    "enter": (0x1C, False),
    "return": (0x1C, False),
    "ctrl": (0x1D, False),
    "control": (0x1D, False),
    "shift": (0x2A, False),
    "alt": (0x38, False),
    "space": (0x39, False),
    "capslock": (0x3A, False),
    "f1": (0x3B, False),
    "f2": (0x3C, False),
    "f3": (0x3D, False),
    "f4": (0x3E, False),
    "f5": (0x3F, False),
    "f6": (0x40, False),
    "f7": (0x41, False),
    "f8": (0x42, False),
    "f9": (0x43, False),
    "f10": (0x44, False),
    "f11": (0x57, False),
    "f12": (0x58, False),
    "home": (0x47, True),
    "up": (0x48, True),
    "pageup": (0x49, True),
    "left": (0x4B, True),
    "right": (0x4D, True),
    "end": (0x4F, True),
    "down": (0x50, True),
    "pagedown": (0x51, True),
    "insert": (0x52, True),
    "delete": (0x53, True),
    "win": (0x5B, True),
    "windows": (0x5B, True),
    "meta": (0x5B, True),
    "super": (0x5B, True),
}

for index, letter in enumerate("qwertyuiop"):
    SCANCODES[letter] = (0x10 + index, False)
for index, letter in enumerate("asdfghjkl"):
    SCANCODES[letter] = (0x1E + index, False)
for index, letter in enumerate("zxcvbnm"):
    SCANCODES[letter] = (0x2C + index, False)
for index, digit in enumerate("1234567890"):
    SCANCODES[digit] = (0x02 + index, False)
