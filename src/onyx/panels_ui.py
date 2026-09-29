"""The shell's two modals: Settings, and Recent conversations.

They replace the launcher's Settings and History pages. Both are ``<dialog>``s
in the vault shell (``vault_ui.py``), opened from the cog and the clock at the
foot of its sidebar, from the app menu (⌘, and ⌘Y, through
``window.onyxShell``), or by a ``#settings``, ``#diagnostics`` or ``#history``
fragment on the shell's URL.

The look is cxtasks' SettingsDialog: an opaque panel over a click-catcher that
barely dims, because the controls in Settings change the window itself and it
has to stay visible while the slider moves. Settings apply as they change, one
field at a time, so there are no Save buttons; a text field commits on Enter or
when it loses focus, never per keystroke, since saving a vault folder also
registers it as a context root.

The script runs inside the shell's ``<script>`` and leans on it: ``$``,
``esc``, ``shortPath``, ``TOKEN``, ``native``, the glass functions
(``launcher_ui.glass_script``), and three hooks — ``openItem(item, action)``,
which puts a saved conversation in the reader, ``reloadTrees()`` after a vault
folder changes, and ``syncSidebarTheme(force)``. A saved answer is drawn by
ask.js's own Markdown renderer, which ``answer_markdown`` inlines.
"""

from __future__ import annotations

import logging
from typing import Any

from .launcher_ui import theme_settings

logger = logging.getLogger("onyx.panels")

_ICON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true">{body}</svg>'
)
# lucide's Settings and History, the glyphs cxtasks uses for the same two things.
ICONS = {
    "settings": _ICON.format(
        body='<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73'
        "l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0"
        " 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73"
        "l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1"
        ' 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2'
        ' 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>'
    ),
    "history": _ICON.format(body='<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>'),
    "close": _ICON.format(body='<path d="M18 6 6 18"/><path d="m6 6 12 12"/>'),
    "back": _ICON.format(body='<path d="m15 18-6-6 6-6"/>'),
}

PANELS_CSS = """/* The two modals. cxtasks' SettingsDialog: opaque (a translucent panel over a translucent window is two sheets of
   glass and no readable text), over a dim that only catches the click outside; a quick rise in, no slide out. */
.modal{width:min(560px,calc(100vw - 32px));max-height:min(780px,calc(100vh - 48px));padding:0;border:1px solid var(--line);border-radius:12px;background:rgb(var(--bg-elevated));color:rgb(var(--ink));box-shadow:0 25px 60px -12px rgb(0 0 0/.42),0 0 0 .5px rgb(0 0 0/.1);overflow:hidden}
.modal[open]{display:flex;flex-direction:column;animation:modal-in .14s cubic-bezier(.2,.8,.2,1)} .modal::backdrop{background:rgb(0 0 0/.1)}
@keyframes modal-in{from{opacity:0;transform:translateY(4px) scale(.985)}} @media(prefers-reduced-motion:reduce){.modal[open]{animation:none}}
.modal-head{display:flex;flex:none;align-items:center;gap:6px;padding:13px 14px 9px 16px} .modal-head h2{flex:1;min-width:0;margin:0;overflow:hidden;font-size:13px;font-weight:600;white-space:nowrap;text-overflow:ellipsis}
.modal-x{display:grid;flex:none;place-items:center;width:24px;height:24px;padding:0;border:0;border-radius:6px;background:transparent;color:rgb(var(--faint));transition:background-color 75ms,color 75ms} .modal-x:hover{background:rgb(var(--ink)/.08);color:rgb(var(--ink))} .modal-x svg{width:14px;height:14px} .modal-x[hidden]{display:none}
/* flex:1 1 auto, not flex:1: Settings has only a max-height, and WebKit sizes a 0% basis in it as 0, so the body
   collapsed to its padding. From its content's height it shrinks to the cap instead, and still fills a sized dialog. */
.modal-body{flex:1 1 auto;min-height:0;overflow:auto;padding:0 16px 16px}
.set-sec{padding:13px 0 14px;border-top:1px solid var(--line-soft)} .set-sec:first-child{padding-top:2px;border-top:0}
.set-sec h3{margin:0 0 10px;color:rgb(var(--faint));font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase}
.set-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:11px 12px} .set-grid .wide{grid-column:1/-1} .set-grid>.field-help{margin-top:-5px}
.modal label{display:block;margin:0 0 5px;color:rgb(var(--secondary));font-size:11px;font-weight:600} .modal label.check{display:flex;align-items:center;gap:7px;margin:0;color:rgb(var(--ink));font-size:12.5px;font-weight:400} .modal label.check input{margin:0}
.modal .row{display:flex;gap:6px} .modal .field+.field{margin-top:0}
.modal input:not([type=checkbox]):not([type=range]){width:100%;min-width:0;padding:6px 9px;border:1px solid var(--line);border-radius:7px;background:rgb(var(--bg-input));color:rgb(var(--ink));font-size:12.5px}
.modal input:not([type=checkbox]):not([type=range]):focus{outline:2px solid rgb(var(--accent)/.26);outline-offset:0;border-color:rgb(var(--accent))}
/* Popup buttons and their menus stay WebKit's: a styled closed control whose open menu is AppKit's reads as two things. */
.modal select{width:100%;min-width:0;min-height:28px;font:-apple-system-body;-webkit-appearance:menulist;appearance:menulist;cursor:default}
.modal .primary,.modal .secondary,.modal .danger{padding:5px 11px;border-radius:7px;font-size:12px;font-weight:600;white-space:nowrap;transition:background-color 75ms,border-color 75ms}
.primary{border:1px solid transparent;background:rgb(var(--button-bg));color:rgb(var(--button-ink));box-shadow:0 1px 2px rgb(0 0 0/.12)} .primary:hover{background:rgb(var(--button-hover))}
.danger{border:1px solid rgb(var(--bad)/.28);background:rgb(var(--bad)/.07);color:rgb(var(--bad))} .modal .pick{flex:none}
.glass-scale{display:flex;align-items:center;justify-content:space-between;margin-top:2px;color:rgb(var(--muted));font-size:10px} .glass-scale strong{color:rgb(var(--secondary));font-weight:600;font-variant-numeric:tabular-nums}
.cx-slider{display:block;width:100%;height:20px;margin:0;padding:0;border:0;background:transparent;box-shadow:none;cursor:grab;-webkit-appearance:none;appearance:none} .cx-slider:focus{outline:none}
.cx-slider::-webkit-slider-runnable-track{height:4px;border-radius:2px;background:linear-gradient(to right,rgb(var(--accent)) 0%,rgb(var(--accent)) var(--fill,38%),rgb(var(--faint)/.35) var(--fill,38%),rgb(var(--faint)/.35) 100%)}
.cx-slider::-webkit-slider-thumb{width:14px;height:14px;margin-top:-5px;border:1px solid rgb(0 0 0/.08);border-radius:50%;background:#fff;box-shadow:0 1px 3px rgb(0 0 0/.4);-webkit-appearance:none;appearance:none}
.root-row{display:flex;align-items:center;gap:8px;padding:6px 0;border-top:1px solid var(--line-soft)} .root-row:first-child{border-top:0} .root-row code{flex:1;min-width:0;overflow:hidden;font-size:12px;white-space:nowrap;text-overflow:ellipsis} #roots{margin-bottom:8px}
.diag-bar{display:flex;gap:8px;margin-bottom:6px} .status>div{flex:1;min-width:0} .status>button{flex:none} .status{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--line-soft)} .status:last-child{border-bottom:0}
.dot{flex:none;width:8px;height:8px;border-radius:50%;background:rgb(var(--faint));box-shadow:0 0 0 3px rgb(var(--faint)/.12)} .dot.ok{background:rgb(var(--good));box-shadow:0 0 0 3px rgb(var(--good)/.12)} .dot.bad{background:rgb(var(--bad));box-shadow:0 0 0 3px rgb(var(--bad)/.12)}
.modal .meta{color:rgb(var(--muted));font-size:11px} .modal .empty{margin:0;padding:22px;color:rgb(var(--muted));text-align:center}
/* Recent conversations: search, the question-type segment, the rarer filters folded away, then one row per answer. */
.history-modal{width:min(640px,calc(100vw - 32px));height:min(780px,calc(100vh - 48px))}
.hist-bar{display:flex;align-items:center;gap:8px;margin:0 0 8px} .hist-bar input{flex:1}
.seg{display:inline-flex;flex:none;gap:1px;padding:1px;border:1px solid var(--line-soft);border-radius:7px} .seg button{padding:3px 8px;border:0;border-radius:5px;background:transparent;color:rgb(var(--secondary));font-size:11.5px;transition:background-color 75ms,color 75ms}
.seg button:hover{background:rgb(var(--ink)/.07);color:rgb(var(--ink))} .seg button[aria-checked=true]{background:rgb(var(--accent-hover));color:#fff;font-weight:500}
.hist-more{margin:0 0 6px} .hist-more summary{display:inline-block;color:rgb(var(--muted));font-size:11.5px;cursor:default;list-style:none} .hist-more summary::-webkit-details-marker{display:none} .hist-more summary::before{content:"›";display:inline-block;margin-right:5px;transition:transform .12s} .hist-more[open] summary::before{transform:rotate(90deg)} .hist-more summary:hover{color:rgb(var(--ink))}
.hist-filters{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:8px 0 4px} #history-count{margin:4px 2px 6px}
/* A row is a <button>; stretch its lines, as WebKit 17 gives buttons align-items:flex-start (see .home-ask). */
.hist-list{display:flex;flex-direction:column;gap:1px} .hist-row{display:flex;flex-direction:column;align-items:stretch;gap:2px;width:100%;padding:8px 10px;border:0;border-radius:8px;background:transparent;color:inherit;text-align:left;transition:background-color 75ms}
.hist-row:hover,.hist-row:focus-visible{background:rgb(var(--ink)/.055);outline:none} .hist-row .t{display:flex;align-items:baseline;gap:8px;min-width:0} .hist-row strong{flex:1;min-width:0;overflow:hidden;font-weight:600;white-space:nowrap;text-overflow:ellipsis}
.hist-row .q{display:-webkit-box;overflow:hidden;color:rgb(var(--secondary));-webkit-line-clamp:2;-webkit-box-orient:vertical}
.badges{display:flex;flex-wrap:wrap;gap:5px;margin:8px 0 0} .badge{display:inline-flex;padding:1px 7px;border-radius:999px;background:rgb(var(--ink)/.065);color:rgb(var(--secondary));font-size:9px;font-weight:650;letter-spacing:.035em;text-transform:uppercase;vertical-align:1px}
.badge.generated{background:rgb(var(--good)/.12);color:rgb(var(--good))} .badge.rerun,.badge.edited,.badge.continue{background:rgb(var(--accent)/.13);color:rgb(var(--accent))}
.hist-block{margin-top:14px} .hist-block>h4{margin:0 0 5px;color:rgb(var(--faint));font-size:10px;font-weight:600;letter-spacing:.06em;text-transform:uppercase}
.hist-block>pre,.hist-answer{max-height:240px;margin:0;padding:11px 12px;overflow:auto;border:1px solid var(--line-soft);border-radius:8px;background:rgb(var(--ink)/.035);color:rgb(var(--ink));font:12.5px/1.55 var(--ui-font)} .hist-block>pre,.hist-answer.plain{white-space:pre-wrap}
/* The answer is Markdown, drawn by the widget's own renderer (answer_markdown below): the panel's answer rules
   (ask.js, .askw-body) in the shell's tokens, which the vault look sets. */
.hist-answer :is(p,ul,ol,pre,.askw-table){margin:0 0 8px} .hist-answer>:last-child{margin-bottom:0} .hist-answer :is(ul,ol){padding-left:20px} .hist-answer li{margin:2px 0}
.hist-answer :is(h1,h2,h3,h4,h5,h6){margin:12px 0 6px;font-size:13px;font-weight:700} .hist-answer>:first-child{margin-top:0} .hist-answer a{color:rgb(var(--accent))}
.hist-answer code{padding:1px 5px;border-radius:4px;background:rgb(var(--ink)/.07);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px} .hist-answer pre{padding:9px 11px;overflow-x:auto;border-radius:7px;background:rgb(var(--ink)/.07)} .hist-answer pre code{padding:0;background:none}
.hist-answer .askw-table{overflow-x:auto} .hist-answer table{border-collapse:collapse;font-size:12px} .hist-answer :is(th,td){padding:4px 8px;border:1px solid var(--line);text-align:left;vertical-align:top} .hist-answer th{background:rgb(var(--ink)/.05)}
.hist-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
@media(max-width:560px){.set-grid,.hist-filters{grid-template-columns:1fr} .hist-bar{flex-wrap:wrap}}"""

PANELS_HTML = """<dialog id=settings-modal class="modal settings-modal" aria-labelledby=settings-title><div class=modal-head><h2 id=settings-title>Settings</h2><button type=button class=modal-x data-close aria-label="Close settings">__CLOSE__</button></div><div class=modal-body>
<section class=set-sec id=setup aria-labelledby=set-setup><h3 id=set-setup>Setup</h3><div id=setup-steps class=meta>Checking…</div><div class=diag-bar><button id=setup-dismiss type=button class=secondary>Don’t open Setup on launch</button></div></section>
<section class=set-sec aria-labelledby=set-appearance><h3 id=set-appearance>Appearance</h3><form id=appearance-form class=set-grid>
<div class=field><label for=appearance-theme>Color theme</label><select id=appearance-theme name=appearance_theme><option value=system>System</option><option value=light>Light</option><option value=dark>Dark</option></select><p id=theme-follow class=field-help hidden>Following your vault while Match vault appearance is on.</p></div>
<div class=field><label for=glass-transparency>Window transparency</label><input id=glass-transparency class=cx-slider name=glass_transparency type=range min=0 max=100 value="__GLASS__" style="--fill:__GLASS__%"><div class=glass-scale><span>Solid</span><strong id=glass-value>__GLASS__% glass</strong><span>Glass</span></div></div>
<div class="field wide"><label class=check><input id=vault-look-toggle type=checkbox> Match vault appearance</label><p id=vault-look-status class=field-help>The whole app in your vault’s colours and font — the sidebar, these dialogs, your notes — shared by the Onyx Obsidian plugin.</p></div>
<div class="field wide"><label class=check><input id=page-look-toggle name=html_follow_page type=checkbox> Match page appearance (HTML)</label><p class=field-help>Use the HTML page’s colours around it. Notes and pages without a clear background keep your usual look.</p></div></form></section>
<section class=set-sec aria-labelledby=set-answers><h3 id=set-answers>Answers</h3><form id=answers-form class=set-grid>
<div class=field><label for=provider>Subscription provider</label><select id=provider name=provider><option value=claude>Claude</option><option value=codex>Codex</option></select></div>
<div class=field><label id=model-label for=model>Model</label><select id=model><option>Loading models…</option></select></div>
<p id=provider-status class="field-help wide">Checking installed subscriptions…</p>
<div class=field><label for=reasoning-effort>Reasoning effort</label><select id=reasoning-effort></select></div>
<div class=field><label for=response-style>Response style</label><select id=response-style name=response_style><option value=concise>Concise</option><option value=balanced>Balanced</option><option value=detailed>Detailed</option></select></div>
<p id=model-help class="field-help wide"></p>
<div class=field><label for=first-activity>First activity timeout (seconds)</label><input id=first-activity name=first_activity_timeout type=number min=10 max=120></div>
<div class=field><label for=request-timeout>Total timeout (seconds)</label><input id=request-timeout name=request_timeout type=number min=30 max=600></div>
<div class="field wide"><label class=check><input name=web_lookups type=checkbox> Check outside facts on the web</label><p class=field-help>Answers can search and read web pages to confirm a fact from outside the folder, which takes longer. Turn it off for faster answers from the folder and skills only.</p></div></form></section>
<section class=set-sec aria-labelledby=set-vaults><h3 id=set-vaults>Vaults</h3><form id=vault-form class=set-grid>
<div class="field wide"><label for=vault-root>Notes vault folder</label><div class=row><input id=vault-root name=vault_root placeholder="~/Documents/CX" spellcheck=false autocomplete=off><button class="secondary pick" type=button data-pick=folder data-target=vault-root>Choose…</button></div><p class=field-help>Browsed read-only under Notes. Saving it also lets answers cite your notes. Leave it empty to hide Notes.</p></div>
<div class="field wide"><label for=html-vault-root>Artifacts folder</label><div class=row><input id=html-vault-root name=html_vault_root placeholder="~/Documents/Artifacts" spellcheck=false autocomplete=off><button class="secondary pick" type=button data-pick=folder data-target=html-vault-root>Choose…</button></div><p class=field-help>A folder of links to HTML pages anywhere on your Mac; its top-level folders are projects. Leave it empty to hide Artifacts.</p></div></form></section>
<section class=set-sec aria-labelledby=set-storage><h3 id=set-storage>Storage and network</h3><form id=privacy-form class=set-grid>
<div class=field><label for=cache-ttl>Answer cache lifetime (hours)</label><input id=cache-ttl name=cache_ttl_hours type=number min=0 max=8760></div>
<div class=field><label for=cache-max>Maximum cached answers</label><input id=cache-max name=cache_max_entries type=number min=0 max=1000></div>
<div class="field wide"><label class=check><input name=history_enabled type=checkbox> Save reading history</label></div>
<div class="field wide"><label class=check><input name=allow_private_remote type=checkbox> Allow trusted private-network URLs</label></div></form></section>
<section class=set-sec aria-labelledby=set-roots><h3 id=set-roots>Allowed context folders</h3><div id=roots></div><button id=add-root class="secondary pick" type=button>Add folder…</button></section>
<section class=set-sec id=diagnostics aria-labelledby=set-diag><h3 id=set-diag>Diagnostics</h3><div class=diag-bar><button id=refresh-diag type=button class=secondary>Refresh</button><button id=probe type=button class=primary>Probe selected provider</button></div><div id=diag class=meta>Checking…</div></section>
</div></dialog>
<dialog id=history-modal class="modal history-modal" aria-labelledby=history-title><div class=modal-head><button type=button id=history-back class=modal-x aria-label="All conversations" hidden>__BACK__</button><h2 id=history-title>Recent conversations</h2><button type=button class=modal-x data-close aria-label="Close conversations">__CLOSE__</button></div><div class=modal-body>
<div id=history-list-view><div class=hist-bar><input id=history-search type=search placeholder="Search questions, passages, and answers…" autocomplete=off spellcheck=false aria-label="Search conversations">
<div id=history-action class=seg role=radiogroup aria-label="Question type"><button type=button role=radio aria-checked=true data-action="">All</button><button type=button role=radio aria-checked=false data-action=ask>Questions</button><button type=button role=radio aria-checked=false data-action=eli5>ELI5</button><button type=button role=radio aria-checked=false data-action=prove>Prove it</button></div></div>
<details class=hist-more><summary>More filters</summary><div class=hist-filters><select id=history-provider aria-label="Filter by provider"><option value="">All providers</option></select><select id=history-model aria-label="Filter by model"><option value="">All models</option></select><select id=history-document aria-label="Filter by document"><option value="">All documents</option></select><select id=history-date aria-label="Filter by date"><option value=0>Any time</option><option value=1>Today</option><option value=7>Past week</option><option value=30>Past month</option><option value=365>Past year</option></select></div></details>
<p id=history-count class=meta></p><div id=history-results class=hist-list></div></div>
<div id=history-detail hidden><p id=history-detail-meta class=meta></p><div id=history-detail-badges class=badges></div><div class=hist-block><h4>Passage</h4><pre id=history-detail-selection></pre></div><div class=hist-block><h4>Question</h4><pre id=history-detail-question></pre></div><div class=hist-block><h4>Answer</h4><div id=history-detail-answer class=hist-answer></div></div><div id=history-detail-actions class=hist-actions></div></div>
</div></dialog>"""

PANELS_JS = r"""// MARK: panels — Settings and Recent conversations (panels_ui.py). One dialog at a time; a click on the dim closes it.
const PANELS=(()=>{const settingsDlg=$('#settings-modal'),historyDlg=$('#history-modal'),say=(text,tone)=>window.OnyxMenu&&OnyxMenu.toast(text,tone);
async function getJSON(url){const r=await fetch(url);const d=await r.json();if(!r.ok||d.ok===false)throw new Error(d.error||`HTTP ${r.status}`);return d}
async function send(url,method,body){const r=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify({token:TOKEN,...body})});const d=await r.json().catch(()=>({ok:false,error:'HTTP '+r.status}));if(!r.ok||d.ok===false)throw new Error(d.error||'HTTP '+r.status);return d}
function since(ts){const d=Math.max(0,Date.now()/1000-ts);if(d<60)return 'just now';if(d<3600)return Math.floor(d/60)+'m ago';if(d<86400)return Math.floor(d/3600)+'h ago';if(d<86400*30)return Math.floor(d/86400)+'d ago';return new Date(ts*1000).toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'})}
function show(dlg){for(const d of [settingsDlg,historyDlg])if(d!==dlg&&d.open)d.close();if(!dlg.open)dlg.showModal()}
for(const dlg of [settingsDlg,historyDlg]){dlg.addEventListener('click',e=>{if(e.target===dlg)dlg.close()});dlg.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>dlg.close())}
// MARK: Settings. A field saves as it changes, and only that field, so one out-of-range number can't hold back a theme
// change; the model and its effort go together, since the server checks one against the other. A refusal puts it back.
const forms=['appearance-form','answers-form','vault-form','privacy-form'].map(id=>$('#'+id));let SETTINGS={},PROVIDERS=null;
function fillForm(form,s){for(const e of form.elements)if(e.name&&s[e.name]!==undefined){if(e.type==='checkbox')e.checked=!!s[e.name];else e.value=s[e.name]}}
function valueOf(e){return e.type==='checkbox'?e.checked:(e.type==='number'||e.type==='range'?Number(e.value):e.value)}
function applyTheme(raw){const theme=['light','dark'].includes(raw)?raw:'system';document.documentElement.dataset.theme=theme;$('#appearance-theme').value=theme;syncAppearance();paintGlass()}
GLASS.onchange=()=>{const pct=Math.round(GLASS.t*100),s=$('#glass-transparency');s.value=pct;s.style.setProperty('--fill',pct+'%');$('#glass-value').textContent=GLASS.reduce?'Reduce Transparency is on':pct+'% glass'};
function effortLabel(v){return({low:'Low',medium:'Medium',high:'High',xhigh:'Extra high',max:'Maximum',ultra:'Ultra'})[v]||v}
function renderEfforts(provider,catalog,model){const field=$('#reasoning-effort'),info=(catalog.models||[]).find(x=>x.id===model)||{},saved=SETTINGS[provider+'_effort']||catalog.selected_effort||info.default_effort||'medium';const efforts=(info.efforts||[]).length?info.efforts.slice():[saved];field.name=provider+'_effort';field.innerHTML=efforts.map(v=>`<option value="${esc(v)}">${esc(effortLabel(v))}</option>`).join('');field.value=efforts.includes(saved)?saved:(efforts.includes(info.default_effort)?info.default_effort:efforts[0])}
function describeModel(provider){const catalog=(PROVIDERS||{})[provider]||{models:[]},model=$('#model').value,item=(catalog.models||[]).find(x=>x.id===model)||{};$('#model-help').textContent=item.description||'Reported by the installed CLI model catalog.';renderEfforts(provider,catalog,model)}
function renderProvider(provider){const catalog=(PROVIDERS||{})[provider]||{id:provider,label:provider,models:[]},model=$('#model');$('#provider').value=provider;$('#model-label').textContent=(catalog.label||provider)+' model';const status=$('#provider-status');status.className='field-help wide '+(catalog.subscription?'ok':'bad');status.textContent=catalog.subscription?`Using ${provider==='claude'?(catalog.plan?catalog.label+' '+catalog.plan:catalog.label):'ChatGPT'} subscription · no API key billing`:(catalog.repair||'Subscription login unavailable');let models=(catalog.models||[]).slice();const key=provider+'_model',saved=SETTINGS[key]||catalog.selected_model||'';if(!models.length&&saved)models=[{id:saved,label:saved,description:'Saved selection; live catalog unavailable.'}];model.name=key;model.innerHTML=models.length?models.map(x=>`<option value="${esc(x.id)}"${x.unavailable?' disabled':''}>${esc(x.label||x.id)}</option>`).join(''):'<option value="" disabled>No available models</option>';if(models.some(x=>x.id===saved&&!x.unavailable))model.value=saved;else if(models.length)model.value=(models.find(x=>!x.unavailable)||models[0]).id;describeModel(provider)}
function revert(form){fillForm(form,SETTINGS);if(form.id==='answers-form')renderProvider(SETTINGS.provider||'claude');if(form.id==='appearance-form'){applyTheme(SETTINGS.appearance_theme);setGlass(SETTINGS.glass_transparency)}}
async function commit(el){if(!el.name||!el.form)return;if(el.type==='number'&&!el.checkValidity()){say(el.validationMessage||'That number is out of range.','bad');revert(el.form);return}
const patch={[el.name]:valueOf(el)};if(el.id==='provider')renderProvider(el.value);if(el.id==='model')describeModel($('#provider').value);if(el.id==='provider'||el.id==='model'){const m=$('#model'),f=$('#reasoning-effort');if(m.value)patch[m.name]=m.value;if(f.value)patch[f.name]=f.value}
try{SETTINGS=(await send('/api/settings','POST',{settings:patch})).settings}catch(e){say(e.message,'bad');revert(el.form);return}
if('vault_root' in patch||'html_vault_root' in patch){refreshRoots();reloadTrees();syncSidebarTheme(true);loadSetup()}
if('html_follow_page' in patch)await syncSidebarTheme(true);
if(el.form.id!=='appearance-form')say('Saved')}
for(const form of forms){form.addEventListener('submit',e=>e.preventDefault());form.addEventListener('change',e=>{if(e.target.id==='appearance-theme')applyTheme(e.target.value);commit(e.target)})}
$('#glass-transparency').addEventListener('input',e=>setGlass(e.target.value));
settingsDlg.querySelectorAll('[data-pick]').forEach(b=>b.onclick=async()=>{const el=$('#'+b.dataset.target);try{const p=await window.webkit.messageHandlers.askwPick.postMessage({kind:b.dataset.pick,initial:el.value});if(p&&p!==el.value){el.value=p;commit(el)}}catch(e){say(e.message,'bad')}});
function renderRoots(roots){$('#roots').innerHTML=(roots||[]).map(r=>`<div class=root-row><code title="${esc(r.path)}">${esc(shortPath(r.path))}</code><span class=meta>${r.builtin?'built in':''}</span>${r.builtin?'':`<button type=button class=danger data-remove="${esc(r.path)}">Remove</button>`}</div>`).join('')||'<p class=meta>No folders yet.</p>'}
async function refreshRoots(){try{renderRoots((await getJSON('/api/settings')).roots)}catch(e){}}
$('#roots').addEventListener('click',async e=>{const b=e.target.closest('[data-remove]');if(!b)return;try{renderRoots((await send('/api/roots','DELETE',{path:b.dataset.remove})).roots)}catch(err){say(err.message,'bad')}});
$('#add-root').onclick=async()=>{try{const p=await window.webkit.messageHandlers.askwPick.postMessage({kind:'folder',initial:''});if(p){renderRoots((await send('/api/roots','POST',{path:p})).roots);say('Allowed folder added')}}catch(e){say(e.message,'bad')}};
// One switch for the whole vault look: the reading styles and the explorer's look are its two settings, set together.
async function lookStatus(){try{const [l,t]=await Promise.all([getJSON('/api/vault-look'),getJSON('/api/sidebar-theme')]),sync=t.last_sync;$('#vault-look-status').textContent=!l.enabled?'Using Onyx’s own look.':sync&&!sync.ok?'Obsidian’s look was refused: '+sync.error:l.available?'Following your vault. Updates automatically while Obsidian is open.':'Waiting for Obsidian. Enable the Onyx plugin in your configured vault; after updating it, turn it off and on in Obsidian ▸ Settings ▸ Community plugins.'}catch(e){}}
function syncThemeControl(){const on=!!LOOK.css;$('#appearance-theme').disabled=on;$('#theme-follow').hidden=!on}
document.addEventListener('onyx:look',()=>{syncThemeControl();if(settingsDlg.open)lookStatus()});syncThemeControl();
$('#vault-look-toggle').addEventListener('change',async e=>{const on=e.target.checked;try{SETTINGS=(await send('/api/settings','POST',{settings:{markdown_follow_obsidian:on,sidebar_follow_obsidian:on}})).settings}catch(err){say(err.message,'bad');e.target.checked=!on;return}await syncSidebarTheme(true);lookStatus()});
// The model catalog asks both CLIs, so it is fetched once per page; the settings themselves every time the dialog opens.
async function loadSettings(){const d=await getJSON('/api/settings');SETTINGS=d.settings;for(const f of forms)fillForm(f,SETTINGS);GLASS.onchange();renderRoots(d.roots);$('#vault-look-toggle').checked=!!(SETTINGS.markdown_follow_obsidian||SETTINGS.sidebar_follow_obsidian);syncThemeControl();lookStatus();
if(!PROVIDERS){try{const c=await getJSON('/api/models');PROVIDERS=Object.fromEntries((c.providers||[]).map(p=>[p.id,p]))}catch(e){PROVIDERS={claude:{id:'claude',label:'Claude',selected_model:SETTINGS.claude_model,models:[]},codex:{id:'codex',label:'Codex',selected_model:SETTINGS.codex_model,models:[]}};say('Model catalog unavailable: '+e.message,'bad')}}renderProvider(SETTINGS.provider||'claude')}
function diagRow(name,ok,detail){return`<div class=status><span class="dot ${ok?'ok':'bad'}"></span><div><strong>${esc(name)}</strong><div class=meta>${esc(detail)}</div></div></div>`}
async function loadDiagnostics(probe){const box=$('#diag');box.textContent=probe?'Running a live subscription request…':'Checking…';try{const r=await fetch('/api/diagnostics'+(probe?'?probe=1':'')),d=await r.json();if(!d.providers)throw new Error(d.error||'HTTP '+r.status);const st=d.storage,selected=d.providers[d.selected_provider];let rows='';for(const id of ['claude','codex']){const p=d.providers[id];rows+=diagRow(`${p.label} CLI`,p.installed,p.version||p.path||p.repair)+diagRow(`${p.label} subscription`,p.subscription,p.subscription?(id==='claude'?`${p.plan||'active'} plan`:'Signed in with ChatGPT'):(p.repair||p.auth_detail||'Not signed in'))}rows+=diagRow('Selected runtime',selected.probe?selected.probe.ok:selected.ok,selected.probe?(selected.probe.ok?`${d.selected_provider}/${d.selected_model} passed in ${selected.probe.latency_ms}ms`:selected.probe.error):`${d.selected_provider}/${d.selected_model} · ${d.selected_effort} effort`)+diagRow('Reading database',st.ok,`${st.documents} documents · ${st.conversations} answers · schema ${st.schema_version}`)+diagRow('Default folder',d.folder.ok,d.folder.path);box.innerHTML=rows;DIAG=d;renderSetup()}catch(e){box.textContent=e.message}}
$('#refresh-diag').onclick=()=>loadDiagnostics(false);$('#probe').onclick=()=>loadDiagnostics(true);
// MARK: Setup. What a new Mac needs, one row each, with the fix beside it; it opens on launch until done or dismissed.
let DIAG=null,SETUP=null;
function setupDone(s){return s.vault.ok&&s.artifacts.ok&&s.plugin.installed&&s.plugin.enabled&&s.theme.ok}
function setupRow(name,ok,detail,act,label){return`<div class=status><span class="dot ${ok?'ok':'bad'}"></span><div><strong>${esc(name)}</strong><div class=meta>${esc(detail)}</div></div>${act&&!ok?`<button type=button class=secondary data-setup=${act}>${esc(label)}</button>`:''}</div>`}
function renderSetup(){const s=SETUP,box=$('#setup-steps');if(!s)return;let rows='';
if(DIAG){const ready=['claude','codex'].map(id=>DIAG.providers[id]).find(p=>p.installed&&p.subscription);rows+=setupRow('Claude or Codex, signed in',!!ready,ready?`${ready.label} CLI with a subscription`:'Install the Claude Code CLI and run `claude` once to sign in with a paid claude.ai plan, or the Codex CLI signed in with ChatGPT. Onyx never uses an API key.')}
rows+=setupRow('Obsidian vault',s.vault.ok,s.vault.ok?shortPath(s.vault.path):'Choose the folder of the Obsidian vault Onyx should browse and cite.','vault','Choose vault…');
rows+=setupRow('Artifacts folder',s.artifacts.ok,s.artifacts.ok?shortPath(s.artifacts.path):'Choose a folder for HTML pages, or leave Artifacts off.','artifacts','Choose folder…');
const p=s.plugin;rows+=setupRow('Onyx plugin in Obsidian',p.installed&&p.enabled,!p.bundled?'This build doesn’t carry the plugin.':!p.vault?'Choose an Obsidian vault first.':p.installed&&p.enabled?`Installed, version ${p.version}`:'Adds Ask to Obsidian and sends Onyx your vault’s look.',p.bundled&&p.vault?'plugin':'',p.installed?'Reinstall plugin':'Install plugin');
rows+=setupRow('Vault look',s.theme.ok,s.theme.ok?'Received from Obsidian. Onyx follows it while “Match vault appearance” is on.':p.installed?'Quit and reopen Obsidian. If it shows Restricted mode, turn that off in Settings ▸ Community plugins. The look arrives within a few seconds.':'Arrives from the plugin once it runs.');
box.innerHTML=rows;$('#setup-dismiss').hidden=s.dismissed}
async function loadSetup(){try{SETUP=await getJSON('/api/setup');renderSetup();return SETUP}catch(e){$('#setup-steps').textContent=e.message}}
$('#setup-steps').addEventListener('click',async e=>{const b=e.target.closest('[data-setup]');if(!b)return;const act=b.dataset.setup;
if(act==='vault'||act==='artifacts'){settingsDlg.querySelector(`[data-target=${act==='vault'?'vault-root':'html-vault-root'}]`).click();return}
b.disabled=true;try{SETUP=await send('/api/setup/obsidian-plugin','POST',{});renderSetup();say('Plugin installed. Quit and reopen Obsidian.')}catch(err){say(err.message,'bad');b.disabled=false}});
$('#setup-dismiss').onclick=async()=>{try{SETTINGS=(await send('/api/settings','POST',{settings:{setup_dismissed:true}})).settings;if(SETUP)SETUP.dismissed=true;renderSetup();say('Setup stays here in Settings')}catch(e){say(e.message,'bad')}};
async function maybeSetup(){const s=await loadSetup();if(s&&!s.dismissed&&!setupDone(s))openSettings('setup')}
function openSettings(section){show(settingsDlg);loadSettings().catch(e=>say(e.message,'bad'));loadSetup();loadDiagnostics(false);const target=section&&document.getElementById(section);if(target)requestAnimationFrame(()=>target.scrollIntoView({block:'start'}));return true}
// MARK: Recent conversations. The list, then one conversation in its place; that conversation's actions put it in the reader.
const HIST=new Map(),answerHtml=__ANSWER_MARKDOWN__;let histAction='',histTimer=0;
function modeLabel(c){return({generated:'Generated',rerun:'Asked again',edited:'Edited & asked',continue:'Continued'})[c.request_mode]||'Generated'}
function actionLabel(a){return({ask:'Question',eli5:'ELI5',prove:'Prove it'})[a]||a}
function remember(items){for(const c of items||[])HIST.set(c.request_id,c)}
function setOptions(id,items,label,valueOf=x=>x,labelOf=x=>x){const el=$(id),current=el.value;el.innerHTML=`<option value="">${esc(label)}</option>`+(items||[]).map(x=>`<option value="${esc(valueOf(x))}">${esc(labelOf(x))}</option>`).join('');if([...el.options].some(o=>o.value===current))el.value=current}
function histRow(c){return`<button type=button class=hist-row data-id="${esc(c.request_id)}"><span class=t><strong>${esc(c.document_title||'Untitled')}</strong><span class=meta>${esc(since(c.started_at))}</span></span><span class=q>${esc(c.question||actionLabel(c.action))}</span><span class=meta>${esc(c.provider||'claude')} · ${esc(c.model)}${c.effort?' · '+esc(c.effort):''} · <span class="badge ${esc(c.request_mode||'generated')}">${esc(modeLabel(c))}</span></span></button>`}
async function loadHistory(){const p=new URLSearchParams({q:$('#history-search').value,provider:$('#history-provider').value,model:$('#history-model').value,source:$('#history-document').value,days:$('#history-date').value,action:histAction});try{const d=await getJSON('/api/library?'+p),items=d.conversations||[],f=d.facets||{};remember(items);setOptions('#history-provider',f.providers,'All providers');setOptions('#history-model',f.models,'All models');setOptions('#history-document',f.documents,'All documents',x=>x.source,x=>x.title);$('#history-count').textContent=items.length+' saved '+(items.length===1?'answer':'answers');$('#history-results').innerHTML=items.length?items.map(histRow).join(''):'<p class=empty>No saved answers match.</p>'}catch(e){$('#history-results').innerHTML=`<p class=empty>${esc(e.message)}</p>`}}
function listView(){$('#history-detail').hidden=true;$('#history-list-view').hidden=false;$('#history-back').hidden=true;$('#history-title').textContent='Recent conversations'}
function openHistory(){listView();show(historyDlg);loadHistory();$('#history-search').focus();return true}
async function showConversation(id){let item=HIST.get(id);if(!item){try{item=(await getJSON('/api/conversations/'+encodeURIComponent(id))).conversation;HIST.set(id,item)}catch(e){say(e.message,'bad');return false}}
show(historyDlg);$('#history-list-view').hidden=true;$('#history-detail').hidden=false;$('#history-back').hidden=false;$('#history-title').textContent=item.document_title||'Saved answer';
$('#history-detail-meta').textContent=new Date(item.started_at*1000).toLocaleString()+' · '+(item.provider||'claude')+' · '+item.model+(item.effort?' · '+item.effort:'')+(item.latency_ms?' · '+(item.latency_ms/1000).toFixed(1)+'s':'');
$('#history-detail-badges').innerHTML=`<span class="badge ${esc(item.request_mode||'generated')}">${esc(modeLabel(item))}</span><span class=badge>${esc(actionLabel(item.action))}</span>`;
$('#history-detail-selection').textContent=item.selection||'No saved passage';$('#history-detail-question').textContent=item.question||actionLabel(item.action);const answer=$('#history-detail-answer'),md=!!(item.answer&&answerHtml);answer.classList.toggle('plain',!md);if(md)answer.innerHTML=answerHtml(item.answer);else answer.textContent=item.answer||item.error||'No answer';answer.scrollTop=0;
$('#history-detail-actions').innerHTML=[['rerun','Ask again','primary'],['edited','Edit & ask','secondary'],['continue','Continue','secondary'],['open','Open document','secondary']].map(([a,l,c])=>`<button type=button class=${c} data-act=${a} data-id="${esc(item.request_id)}">${l}</button>`).join('');
historyDlg.querySelector('.modal-body').scrollTop=0;$('#history-detail-actions button').focus();return true}
$('#history-results').addEventListener('click',e=>{const r=e.target.closest('[data-id]');if(r)showConversation(r.dataset.id)});
$('#history-detail-actions').addEventListener('click',e=>{const b=e.target.closest('[data-act]'),item=b&&HIST.get(b.dataset.id);if(!item)return;historyDlg.close();openItem(item,b.dataset.act==='open'?'':b.dataset.act)});
$('#history-back').onclick=()=>{listView();$('#history-search').focus()};
$('#history-search').addEventListener('input',()=>{clearTimeout(histTimer);histTimer=setTimeout(loadHistory,180)});
for(const id of ['history-provider','history-model','history-document','history-date'])$('#'+id).addEventListener('change',loadHistory);
$('#history-action').addEventListener('click',e=>{const b=e.target.closest('[role=radio]');if(!b)return;histAction=b.dataset.action;for(const x of $('#history-action').querySelectorAll('[role=radio]'))x.setAttribute('aria-checked',String(x===b));loadHistory()});
// ⌘, and ⌘Y for a browser; in the app, the menu's own items get there first.
document.addEventListener('keydown',e=>{if(!(e.metaKey||e.ctrlKey)||e.altKey||e.shiftKey)return;if(e.key===','){e.preventDefault();openSettings()}else if(e.key==='y'){e.preventDefault();openHistory()}});
return {openSettings,openHistory,showConversation,remember,maybeSetup}})();"""


def panels_style() -> str:
    return PANELS_CSS


def panels_markup(settings: dict[str, Any] | None) -> str:
    glass, _theme = theme_settings(settings)
    return (
        PANELS_HTML.replace("__GLASS__", str(glass))
        .replace("__CLOSE__", ICONS["close"])
        .replace("__BACK__", ICONS["back"])
    )


# ask.js's Markdown section runs between two of its own section rules and needs nothing else from the widget.
_MD_START = "// ============================================================ markdown"
_MD_END = "// ============================================================ helpers"


def answer_markdown() -> str:
    """The widget's own Markdown renderer, lifted out of ``static/ask.js`` as a JS expression.

    A saved answer is the Markdown the panel streamed, so Recent conversations draws it with the same renderer rather
    than a second one that would drift. Read on every render, as GET /ask.js is, so an edit shows on reload. Without
    the section the answer shows as plain text: never a broken shell over formatting.
    """
    from .app import ASK_JS  # app imports this module, through vault_ui

    try:
        text = ASK_JS.read_text(encoding="utf-8")
        start = text.index(_MD_START)
        end = text.index(_MD_END, start)
    except (OSError, ValueError):
        logger.warning("No Markdown section in %s; saved answers show as plain text", ASK_JS)
        return "null"
    return "(()=>{" + text[start:end] + "return mdToHtml})()"


def panels_script() -> str:
    return PANELS_JS.replace("__ANSWER_MARKDOWN__", answer_markdown())
