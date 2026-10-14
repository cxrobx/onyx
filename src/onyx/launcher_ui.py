"""The shell's shared look: cxtasks-style macOS glass, theme tokens, and the sidebar grid.

The glass has two halves, driven by one slider. The page paints alpha-aware
pane tints (``--pane-alpha`` and friends, from :func:`glass_alphas`); the native
app blurs the desktop behind the window at a radius from :func:`blur_radius`
(``CGSSetWindowBackgroundBlurRadius`` — see ``launcher/Onyx.swift``). The
two are coupled on purpose: the more desktop the panes let through, the more
blur it takes to keep text on top of it readable.
"""

from __future__ import annotations

import json
import math
from typing import Any


# The blur range the slider sweeps, ported from cxtasks (767a8fb). Past ~48 the
# wallpaper turns to featureless smoke, which is the NSVisualEffectView look this
# replaces; below 10 a slightly-clear window over a sharp desktop reads as a
# rendering bug. The default 38% lands on 24.
BLUR_MIN = 10
BLUR_MAX = 48
# Each theme's opaque window colour. Must track --bg-primary below: the native
# window paints it before any page exists, and again whenever glass is off.
BASE_RGB = {"light": (247, 247, 247), "dark": (24, 24, 24)}


def blur_radius(transparency: float) -> int:
    """The desktop blur for a slider position; mirrors ``glassRadius`` in JS."""
    t = min(1.0, max(0.0, transparency)) if math.isfinite(transparency) else 0.0
    return round(BLUR_MIN + t * (BLUR_MAX - BLUR_MIN))


# The sidebar's readability floor. cxtasks lets its dark sidebar thin to 10% —
# the Codex character — which is glass over a dark desktop and unreadable over a
# bright one: seen over a green terminal block, where the labels washed out. The
# NSVisualEffectView material used to lay a tint of its own under the pane; the
# raw blur has none, so the pane's own alpha is all that stands between the text
# and the desktop. The sidebar therefore never goes thinner than the alpha that
# keeps its label text (--secondary) at WCAG AA 4.5:1 over the WORST backdrop
# for the theme: pure white behind dark mode, pure black behind light. Derived
# from the palette rather than tuned, so moving a token moves the floor with it.
SIDEBAR_TINT = {"dark": 43, "light": 255}  # --bg-sidebar (dark's brightest channel)
SIDEBAR_LABEL = {"dark": 205, "light": 93}  # --secondary
WORST_BACKDROP = {"dark": 255, "light": 0}
READABLE_CONTRAST = 4.5


def _luminance(grey: float) -> float:
    c = grey / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def contrast_ratio(a: float, b: float) -> float:
    """WCAG contrast between two sRGB greys (0–255)."""
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def sidebar_contrast(alpha: float, theme: str) -> float:
    """Label contrast with the sidebar at ``alpha`` over the theme's worst backdrop.

    WebKit composites alpha in gamma-encoded sRGB, so the mix is linear in 0–255.
    """
    composite = SIDEBAR_TINT[theme] * alpha + WORST_BACKDROP[theme] * (1 - alpha)
    return contrast_ratio(SIDEBAR_LABEL[theme], composite)


def _readable_floor(theme: str) -> float:
    return next(
        (step / 100 for step in range(101) if sidebar_contrast(step / 100, theme) >= READABLE_CONTRAST),
        1.0,
    )


SIDEBAR_READABLE = {theme: _readable_floor(theme) for theme in ("dark", "light")}


def glass_alphas(transparency: float, dark: bool) -> tuple[float, float, float]:
    pane_floor = 0.25 if dark else 0.55
    sidebar_boost = 1.9 if dark else 1.6
    sidebar_floor = 0.10 if dark else 0.45
    pane = 1 - transparency * (1 - pane_floor)
    sidebar_t = min(1, transparency * sidebar_boost)
    # cxtasks' curve, clamped at the readability floor: as glassy as it can be
    # while the labels stay legible over anything.
    sidebar = max(1 - sidebar_t * (1 - sidebar_floor), SIDEBAR_READABLE["dark" if dark else "light"])
    surface = pane + (1 - pane) * 0.5
    return pane, sidebar, surface


def theme_settings(settings: dict[str, Any] | None) -> tuple[int, str]:
    """Clamp the persisted appearance settings to ``(glass_percent, theme)``."""
    glass = max(0, min(100, int((settings or {}).get("glass_transparency", 38))))
    theme = str((settings or {}).get("appearance_theme", "system"))
    if theme not in {"system", "light", "dark"}:
        theme = "system"
    return glass, theme


def theme_style(settings: dict[str, Any] | None) -> str:
    """The shared glass tokens, shell grid, sidebar, and nav rules.

    The shell (``vault_ui.py``) starts its stylesheet with this block and
    overrides on top.
    """
    glass, _theme = theme_settings(settings)
    transparency = glass / 100
    light_pane, light_sidebar, light_surface = glass_alphas(transparency, False)
    dark_pane, dark_sidebar, dark_surface = glass_alphas(transparency, True)
    return f""":root{{--pane-alpha:{light_pane:.3f};--sidebar-alpha:{light_sidebar:.3f};--surface-alpha:{light_surface:.3f};--bg-primary:247 247 247;--bg-sidebar:255 255 255;--bg-surface:252 252 252;--bg-elevated:255 255 255;--bg-input:255 255 255;--ink:13 13 13;--secondary:93 93 93;--muted:143 143 143;--faint:175 175 175;--line:rgb(0 0 0/.10);--line-soft:rgb(0 0 0/.055);--accent:212 59 53;--accent-hover:183 46 41;--button-bg:13 13 13;--button-hover:47 47 47;--button-ink:255 255 255;--good:61 138 67;--bad:208 46 46;--warning:199 98 33;--selected:rgb(0 0 0/.07);--ui-font:-apple-system,BlinkMacSystemFont,"SF Pro Display","SF Pro Text","Helvetica Neue",sans-serif;color-scheme:light dark}}
:root[data-theme="light"]{{color-scheme:light}} :root[data-theme="dark"]{{--pane-alpha:{dark_pane:.3f};--sidebar-alpha:{dark_sidebar:.3f};--surface-alpha:{dark_surface:.3f};--accent:241 107 101;--accent-hover:212 59 53;--bg-primary:24 24 24;--bg-sidebar:42 43 43;--bg-surface:28 28 28;--bg-elevated:45 45 45;--bg-input:45 45 45;--ink:255 255 255;--secondary:205 205 205;--muted:175 175 175;--faint:143 143 143;--line:rgb(255 255 255/.15);--line-soft:rgb(255 255 255/.06);--button-bg:48 48 48;--button-hover:65 65 65;--button-ink:249 249 249;--good:83 181 89;--bad:255 133 131;--warning:241 162 117;--selected:rgb(255 255 255/.10);color-scheme:dark}}
@media(prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--pane-alpha:{dark_pane:.3f};--sidebar-alpha:{dark_sidebar:.3f};--surface-alpha:{dark_surface:.3f};--accent:241 107 101;--accent-hover:212 59 53;--bg-primary:24 24 24;--bg-sidebar:42 43 43;--bg-surface:28 28 28;--bg-elevated:45 45 45;--bg-input:45 45 45;--ink:255 255 255;--secondary:205 205 205;--muted:175 175 175;--faint:143 143 143;--line:rgb(255 255 255/.15);--line-soft:rgb(255 255 255/.06);--button-bg:48 48 48;--button-hover:65 65 65;--button-ink:249 249 249;--good:83 181 89;--bad:255 133 131;--warning:241 162 117;--selected:rgb(255 255 255/.10);color-scheme:dark}}}}
*{{box-sizing:border-box}} html,body{{min-height:100%;margin:0;background:transparent}} body{{color:rgb(var(--ink));font:13px/1.5 var(--ui-font);-webkit-font-smoothing:antialiased}}
body:not(.native)::before{{content:"";position:fixed;inset:0;z-index:-2;background:radial-gradient(circle at 15% 8%,#9ecbff 0,transparent 34%),radial-gradient(circle at 88% 18%,#d8bcff 0,transparent 31%),radial-gradient(circle at 54% 100%,#ffd6b8 0,transparent 38%),#dce4ee}} body.native::before{{display:none}}
button,input{{font:inherit}} button{{cursor:pointer}} .shell{{display:grid;grid-template-columns:210px minmax(0,1fr);min-height:100vh;background:transparent}}
aside{{position:sticky;top:0;height:100vh;padding:28px 15px;background:rgb(var(--bg-sidebar)/var(--sidebar-alpha));border-right:1px solid var(--line-soft);backdrop-filter:saturate(1.18)}}
.brand{{display:flex;gap:10px;align-items:center;margin:0 9px 32px;font-size:15px;font-weight:650}} .mark{{display:block;flex:none;width:auto;height:30px}}
nav button{{display:block;width:100%;margin:2px 0;padding:8px 11px;border:0;border-radius:7px;background:transparent;color:rgb(var(--secondary));text-align:left;transition:background .12s,color .12s}} nav button:hover{{background:rgb(var(--ink)/.06);color:rgb(var(--ink))}} nav button.active{{background:var(--selected);color:rgb(var(--ink));font-weight:600}}
.aside-foot{{position:absolute;bottom:20px;left:25px;color:rgb(var(--faint));font-size:11px}} main{{min-width:0;padding:42px 46px 80px;background:rgb(var(--bg-primary)/var(--pane-alpha));backdrop-filter:saturate(1.12)}}
body.native aside{{padding-top:52px}} body.native main{{padding-top:62px}}
@media(prefers-reduced-transparency:reduce){{:root{{--pane-alpha:1!important;--sidebar-alpha:1!important;--surface-alpha:1!important}} .open-card,.panel,.card{{backdrop-filter:none}}}}"""


def glass_script(settings: dict[str, Any] | None, vault: dict[str, Any] | None = None) -> str:
    """The glass half every shell page runs: pane alphas + the native blur.

    The window boots opaque (a clear window around an empty WebView is bare
    wallpaper and three floating traffic lights), so glass is armed only after
    the first paint — two animation frames, since one fires early often enough
    to show the flash. Reduce Transparency pins the EFFECTIVE transparency to 0
    in both halves without moving the saved slider, so turning it back off
    restores the window the user had. ``GLASS.sent`` keeps a slider drag from
    re-running the native window setup: the radius is an integer, so a full
    sweep sends at most ~38 messages, and only the radius when nothing else moved.

    ``vault`` is the vault look's ``{mode, base}`` (vault_look.palette): while it is on, the
    glass is of the vault's mode and the native window's base is the vault's ground, so a
    dark app theme doesn't frame a cream page. ``setGlassVault`` moves it live.
    """
    glass, _theme = theme_settings(settings)
    light, dark = BASE_RGB["light"], BASE_RGB["dark"]
    floor_dark, floor_light = SIDEBAR_READABLE["dark"], SIDEBAR_READABLE["light"]
    vault_json = json.dumps({"mode": vault["mode"], "base": vault["base"]} if vault else None)
    return f"""const GLASS={{t:{glass}/100,ready:false,reduce:false,sent:null,onchange:null,vault:{vault_json}}};const glassScheme=matchMedia('(prefers-color-scheme:dark)');
function glassDark(){{if(GLASS.vault)return GLASS.vault.mode==='dark';const t=document.documentElement.dataset.theme;return t==='dark'||(t!=='light'&&glassScheme.matches)}}
function glassAlphas(t,dark){{const pane=1-t*(1-(dark?.25:.55)),s=Math.min(1,t*(dark?1.9:1.6));return{{pane,sidebar:Math.max(1-s*(1-(dark?.10:.45)),dark?{floor_dark}:{floor_light}),surface:pane+(1-pane)*.5}}}}
function glassRadius(t){{t=Number.isFinite(t)?Math.min(1,Math.max(0,t)):0;return Math.round({BLUR_MIN}+t*({BLUR_MAX}-{BLUR_MIN}))}}
function glassBridge(){{return window.webkit&&window.webkit.messageHandlers&&window.webkit.messageHandlers.askwGlass}}
function sendGlass(eff){{const h=glassBridge();if(!GLASS.ready||!h)return;const dark=glassDark(),rgb=GLASS.vault?GLASS.vault.base:(dark?[{dark[0]},{dark[1]},{dark[2]}]:[{light[0]},{light[1]},{light[2]}]),key=rgb.join(),enabled=eff>0,radius=glassRadius(eff),prev=GLASS.sent;if(prev&&prev.enabled===enabled&&prev.radius===radius&&prev.key===key)return;const radiusOnly=!!(prev&&prev.enabled&&enabled&&prev.key===key);GLASS.sent={{enabled,radius,key}};Promise.resolve(h.postMessage({{enabled,radius,radiusOnly,rgb}})).catch(()=>{{}})}}
function paintGlass(){{const eff=GLASS.reduce?0:GLASS.t,a=glassAlphas(eff,glassDark()),r=document.documentElement.style;r.setProperty('--pane-alpha',a.pane.toFixed(3));r.setProperty('--sidebar-alpha',a.sidebar.toFixed(3));r.setProperty('--surface-alpha',a.surface.toFixed(3));sendGlass(eff);if(GLASS.onchange)GLASS.onchange()}}
function setGlassVault(v){{GLASS.vault=v&&v.mode?{{mode:v.mode,base:v.base}}:null;paintGlass()}}
function setGlass(percent){{const t=Number(percent)/100;GLASS.t=Number.isFinite(t)?Math.min(1,Math.max(0,t)):0;paintGlass()}}
function startGlass(){{const h=glassBridge();if(!h){{GLASS.ready=true;return}}Promise.resolve(h.postMessage({{query:true}})).then(r=>{{GLASS.reduce=!!(r&&r.reduceTransparency)}},()=>{{}}).finally(()=>{{GLASS.ready=true;paintGlass()}})}}
window.askwReduceTransparency=on=>{{GLASS.reduce=!!on;paintGlass()}};glassScheme.addEventListener?.('change',()=>paintGlass());paintGlass();requestAnimationFrame(()=>requestAnimationFrame(startGlass));"""
