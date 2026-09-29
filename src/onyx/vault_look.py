"""The whole app in the vault's colours: a palette for Onyx's own chrome, derived from the Obsidian snapshots.

The plugin already sends two measured snapshots of the vault's theme — the reading
view (markdown_theme) and the file explorer (sidebar_theme). Between them they
carry the vault's ground, ink, link colour, code-block ground and interface font.
``palette`` turns those into the app's own tokens (launcher_ui.theme_style), so
everything built on them — the main pane, Library's home page, Settings and Recent
conversations, menus, toasts, the answer panel — wears the vault while "Match vault
appearance" is on.

Text shades are not taken from the theme: they are mixed from its ink toward its
ground until they reach the contrasts the app's own palette keeps, so no theme can
leave a label unreadable. An accent too faint on the ground falls back to the ink.
Only numbers parsed out of rgb()/rgba() values reach the CSS, and the font list is
re-checked against markdown_theme._UNSAFE.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .markdown_theme import _UNSAFE

RGB = tuple[float, float, float]
_RGBA = re.compile(r"^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)$")

# The contrast each text shade keeps against the ground: the app's own light palette, measured on its 247 ground
# (--secondary 93 ≈ 6.4:1, --muted 143 ≈ 3.2:1, --faint 175 ≈ 2.1:1).
SHADES = {"secondary": 6.0, "muted": 3.2, "faint": 2.1}
MIN_INK_CONTRAST = 4.5
MIN_ACCENT_CONTRAST = 3.0
# Onyx's own red: the glow on the rim of the logo's stone (launcher/onyx-gem.png), its commonest bright red.
ONYX_RED: RGB = (240.0, 0.0, 0.0)


def _parse(value: Any) -> tuple[RGB, float] | None:
    if not isinstance(value, str):
        return None
    match = _RGBA.match(value.strip())
    if not match:
        return None
    try:
        rgb = tuple(min(255.0, max(0.0, float(part))) for part in match.groups()[:3])
        raw_alpha = match.group(4)
        alpha = 1.0 if raw_alpha is None else float(raw_alpha.rstrip("%")) / (100 if raw_alpha.endswith("%") else 1)
    except ValueError:
        return None
    return rgb, min(1.0, max(0.0, alpha))  # type: ignore[return-value]


def _opaque(value: Any) -> RGB | None:
    parsed = _parse(value)
    return parsed[0] if parsed and parsed[1] >= 0.99 else None


def _over(value: Any, ground: RGB) -> RGB | None:
    """A colour as it is drawn on the ground (a translucent one composited); None unless plain, visible rgb()/rgba()."""
    parsed = _parse(value)
    if parsed is None or parsed[1] == 0:
        return None
    rgb, alpha = parsed
    return tuple(c * alpha + g * (1 - alpha) for c, g in zip(rgb, ground))  # type: ignore[return-value]


def _mix(a: RGB, b: RGB, t: float) -> RGB:
    return tuple(x + (y - x) * t for x, y in zip(a, b))  # type: ignore[return-value]


def _luminance(color: RGB) -> float:
    def channel(value: float) -> float:
        value /= 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in color)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: RGB, b: RGB) -> float:
    """WCAG contrast between two sRGB colours."""
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _shade(ink: RGB, ground: RGB, target: float) -> RGB:
    """The ink mixed as far toward the ground as it can go while keeping ``target`` contrast against it."""
    best = ink
    for step in range(1, 101):
        candidate = _mix(ink, ground, step / 100)
        if contrast(candidate, ground) < target:
            break
        best = candidate
    return best


def _lift(color: RGB, ground: RGB, target: float) -> RGB:
    """The colour mixed toward white or black only as far as it must go to keep ``target`` contrast on the ground."""
    for step in range(101):
        for toward in ((255.0, 255.0, 255.0), (0.0, 0.0, 0.0)):
            candidate = _mix(color, toward, step / 100)
            if contrast(candidate, ground) >= target:
                return candidate
    return color


def _triplet(color: RGB) -> str:
    return " ".join(str(round(v)) for v in color)


def _style(snapshot: dict[str, Any] | None, element: str) -> dict[str, str]:
    return ((snapshot or {}).get("styles") or {}).get(element) or {}


def palette(markdown: dict[str, Any] | None, sidebar: dict[str, Any] | None) -> dict[str, Any] | None:
    """The app's tokens in the vault's colours, or None when the snapshots give no opaque ground and readable ink."""
    content, pane = _style(markdown, "content"), _style(sidebar, "pane")
    ground = _opaque(content.get("background-color")) or _opaque(pane.get("background-color"))
    if ground is None:
        return None
    ink = (
        _over(content.get("color"), ground)
        or _over(pane.get("color"), ground)
        or _over(_style(sidebar, "file").get("color"), ground)
    )
    if ink is None or contrast(ink, ground) < MIN_INK_CONTRAST:
        return None
    mode = (markdown or sidebar or {}).get("mode")
    if mode not in ("light", "dark"):
        mode = "dark" if _luminance(ground) < 0.18 else "light"
    dark = mode == "dark"
    code = _over(_style(markdown, "code").get("background-color"), ground) or _over(
        _style(markdown, "pre").get("background-color"), ground
    )
    surface = _mix(ground, code, 0.4) if code else _mix(ground, ink, 0.035)
    accent = _over(_style(markdown, "a").get("color"), ground) or _over(_style(markdown, "internal-link").get("color"), ground)
    if accent is None or contrast(accent, ground) < MIN_ACCENT_CONTRAST:
        accent = ink
    selected = _over(_style(sidebar, "active").get("background-color"), ground)
    # A primary button is the ink on a light vault (as the app's own is near-black), a raised ground on a dark one.
    button, button_hover, button_ink = (
        (_mix(ground, ink, 0.16), _mix(ground, ink, 0.24), ink) if dark else (ink, _mix(ink, ground, 0.18), ground)
    )
    tokens = {
        "--bg-primary": _triplet(ground),
        "--bg-sidebar": _triplet(_opaque(pane.get("background-color")) or ground),
        "--bg-surface": _triplet(surface),
        "--bg-elevated": _triplet(_mix(ground, ink, 0.05) if dark else ground),
        "--bg-input": _triplet(_over(_style(sidebar, "search").get("background-color"), ground) or surface),
        "--ink": _triplet(ink),
        **{f"--{name}": _triplet(_shade(ink, ground, target)) for name, target in SHADES.items()},
        "--line": f"rgb({_triplet(ink)}/.14)",
        "--line-soft": f"rgb({_triplet(ink)}/.07)",
        "--selected": f"rgb({_triplet(selected)})" if selected else f"rgb({_triplet(ink)}/.08)",
        "--accent": _triplet(accent),
        "--accent-hover": _triplet(_mix(accent, ink, 0.25)),
        "--button-bg": _triplet(button),
        "--button-hover": _triplet(button_hover),
        "--button-ink": _triplet(button_ink),
    }
    # The vault's interface font, which only the explorer measures; its reading font is for the reading pages alone,
    # and chrome set in it would read as part of the note.
    font = pane.get("font-family") or ""
    if len(font) > 512 or _UNSAFE.search(font):
        font = ""
    if font:
        tokens["--ui-font"] = font
    return {"mode": mode, "base": [round(v) for v in ground], "tokens": tokens}


def stylesheet(look: dict[str, Any] | None) -> str:
    """The shell's rules: the tokens on ``:root.vault-look``, which the shell wears while the look is on.

    The sidebar re-takes them too: sidebar_theme gives its pane tokens of the vault's mode so the explorer reads over a
    different app theme, and with the whole app in the vault's colours it takes the same ones as everything else.
    """
    if not look:
        return ""
    decls = ";".join(f"{name}:{value}" for name, value in look["tokens"].items())
    return (
        f":root.vault-look{{{decls};color-scheme:{look['mode']}}}\n"
        f":root.vault-look body.obsidian-tree aside{{{decls}}}"
    )


def reader_stylesheet(look: dict[str, Any] | None) -> str:
    """The answer panel (static/ask.js) in the vault's colours, on ``html[data-askw-look]``, which ask.js sets.

    Its variables, and the surfaces it paints with fixed colours of its own (a light and a dark set). It is injected
    after ask.js's own sheet and matches its selectors' weight, so these win; the warning colours stay the panel's own.

    Each colour lands only on a surface it was measured against. The accent is known to read on the vault's grounds
    (palette holds it to 3:1 there), so it is text and marks, never a fill: a filled button is the vault's button, as
    the shell's are. The glass chips lie on the page, so they wear the vault only over a page of the vault's own tone
    (data-askw-page, which ask.js sets); over the other they keep Onyx's own glass for that page. The send arrow is
    Onyx's own red, the logo's, lifted only as far as it must go to mark 3:1 on the vault's button and its hover.
    """
    if not look:
        return ""
    t, s = look["tokens"], "html[data-askw-look]"
    ink, secondary, muted, faint = t["--ink"], t["--secondary"], t["--muted"], t["--faint"]
    ground, surface, field, accent = t["--bg-elevated"], t["--bg-surface"], t["--bg-input"], t["--accent"]
    button, button_hover, button_ink = t["--button-bg"], t["--button-hover"], t["--button-ink"]
    glass = f'{s}[data-askw-page="{look["mode"]}"]'
    send, send_hover = (
        _triplet(_lift(ONYX_RED, tuple(float(v) for v in t[fill].split()), MIN_ACCENT_CONTRAST))  # type: ignore[arg-type]
        for fill in ("--button-bg", "--button-hover")
    )
    font = f";font-family:{t['--ui-font']}" if t.get("--ui-font") else ""

    def rule(selectors: str, body: str, scope: str = s) -> str:
        return ",".join(f"{scope} {sel.strip()}" for sel in selectors.split(",")) + "{" + body + "}"

    return "\n".join([
        rule(".askw-root", f"color:rgb({ink});--askw-accent:rgb({accent});--askw-accent-hover:rgb({t['--accent-hover']});"
             f"--askw-line:rgb({ink}/.14);--askw-soft:rgb({ink}/.07){font}"),
        rule(".askw-menu,.askw-panel,.askw-picker,.askw-chats-list", f"background:rgb({ground}/.97)"),
        rule(".askw-head,.askw-foot,.askw-followup", f"background:rgb({surface}/.45)"),
        rule(".askw-item,.askw-body,.askw-chats-q", f"color:rgb({ink})"),
        rule(".askw-selq,.askw-q,.askw-foot button,.askw-picker label,.askw-recent-item,.askw-history-actions button,"
             ".askw-citation pre,.askw-chats-sel", f"color:rgb({secondary})"),
        rule(".askw-think,.askw-citations-title,.askw-chats-title", f"color:rgb({muted})"),
        rule(".askw-x,.askw-request-meta,.askw-history-meta,.askw-chats-meta", f"color:rgb({faint})"),
        rule(".askw-item:hover,.askw-x:hover,.askw-recent-item:hover,.askw-chats-row:hover",
             f"background:rgb({ink}/.07);color:rgb({ink})"),
        rule(".askw-q,.askw-citation,.askw-foot button,.askw-history-entry,.askw-history-actions button",
             f"background:rgb({surface}/.7)"),
        rule(".askw-foot button:hover,.askw-citation:hover", f"background:rgb({surface});color:rgb({ink})"),
        rule(".askw-ask-input,.askw-follow-input,.askw-picker input,.askw-picker-browse",
             f"background:rgb({field});border-color:rgb({ink}/.14);color:rgb({ink})"),
        rule(".askw-ask-input:focus,.askw-follow-input:focus,.askw-picker input:focus",
             f"border-color:rgb({accent});box-shadow:0 0 0 2px rgb({accent}/.18)"),
        rule(".askw-body code,.askw-citation pre", f"background:rgb({surface})"),
        rule(".askw-body pre", f"background:rgb({surface});color:rgb({ink})"),
        rule(".askw-pillt,.askw-origin", f"background:rgb({accent}/.12);color:rgb({accent})"),
        rule(".askw-citation,.askw-foot .askw-retry", f"color:rgb({accent})"),
        rule(".askw-toast", f"background:rgb({ink}/.92);color:rgb({ground})"),
        rule(".askw-trigger,.askw-ask-go,.askw-follow-go,.askw-foot .askw-claude,.askw-picker-save,"
             ".askw-history-actions button:first-child", f"background:rgb({button});color:rgb({button_ink})"),
        rule(".askw-foot .askw-claude,.askw-history-actions button:first-child", f"border-color:rgb({button})"),
        rule(".askw-trigger:hover,.askw-follow-go:hover,.askw-foot .askw-claude:hover",
             f"background:rgb({button_hover});color:rgb({button_ink})"),
        rule(".askw-follow-go", f"color:rgb({send})"),
        rule(".askw-follow-go:hover", f"color:rgb({send_hover})"),
        rule(".askw-pill,.askw-chats", f"--askw-glass-accent:rgb({accent})", glass),
        rule(".askw-pill", f"color:rgb({secondary})", glass),
        rule(".askw-pill b", f"color:rgb({ink})", glass),
    ])


def revision(look: dict[str, Any] | None) -> str:
    return hashlib.sha256(json.dumps(look, sort_keys=True).encode()).hexdigest()[:20]
