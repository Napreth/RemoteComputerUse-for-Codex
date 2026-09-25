"""X11 keysyms used by RFB KeyEvent messages."""

KEYSYMS: dict[str, int] = {
    "backspace": 0xFF08,
    "tab": 0xFF09,
    "enter": 0xFF0D,
    "return": 0xFF0D,
    "escape": 0xFF1B,
    "esc": 0xFF1B,
    "home": 0xFF50,
    "left": 0xFF51,
    "up": 0xFF52,
    "right": 0xFF53,
    "down": 0xFF54,
    "pageup": 0xFF55,
    "pagedown": 0xFF56,
    "end": 0xFF57,
    "insert": 0xFF63,
    "delete": 0xFFFF,
    "shift": 0xFFE1,
    "ctrl": 0xFFE3,
    "control": 0xFFE3,
    "capslock": 0xFFE5,
    "meta": 0xFFE7,
    "alt": 0xFFE9,
    "super": 0xFFEB,
    "win": 0xFFEB,
    "windows": 0xFFEB,
    "space": 0x20,
}

for index in range(1, 13):
    KEYSYMS[f"f{index}"] = 0xFFBD + index

CONTROL_KEY_ALIASES = {
    "\b": "backspace",
    "\t": "tab",
    "\n": "return",
    "\r": "return",
    "\x1b": "escape",
    "\x7f": "delete",
}


def keysym(name: str) -> int:
    if name in CONTROL_KEY_ALIASES:
        return KEYSYMS[CONTROL_KEY_ALIASES[name]]
    normalized = name.strip().lower().replace("_", "")
    if normalized in KEYSYMS:
        return KEYSYMS[normalized]
    if len(name) == 1:
        codepoint = ord(name)
        return codepoint if codepoint <= 0xFF else 0x01000000 | codepoint
    raise ValueError(f"unsupported VNC key name: {name}")


def held_keysym(name: str) -> int:
    if len(name) == 1 and name not in CONTROL_KEY_ALIASES and not 32 <= ord(name) <= 126:
        raise ValueError("held VNC keys must be named keys, supported controls or printable ASCII; use write for Unicode")
    return keysym(name)


def text_keysym(character: str) -> int:
    if character in CONTROL_KEY_ALIASES:
        return keysym(character)
    codepoint = ord(character)
    if codepoint < 32 or 0x7F <= codepoint < 0xA0:
        raise ValueError(f"unsupported VNC text control character: U+{codepoint:04X}")
    return codepoint if codepoint <= 0xFF else 0x01000000 | codepoint
