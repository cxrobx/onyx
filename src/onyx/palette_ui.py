"""⌘P: the search palette, as in Obsidian. One box over the window, for every page in Notes and Artifacts.

Empty, it lists what was opened lately. Typed into, it shows at once the pages whose titles match (the sidebar's own
``/api/vault/search``, over both vaults), then, once typing pauses, the passages inside pages that match by their words
or by their meaning (``/api/search``, ``search.py``). ↑↓ move, ↩ opens the pick in the reader, a passage at its
section (⌘↩ or a ⌘-click, in a new tab), and Escape or a click outside puts the box away. With other notes vaults set up, a switch in the box says what is searched — the primary vault (and Artifacts) or every
vault — starting each time from Settings' choice (``search_scope``); a click or ⌥S flips it for that search. It opens from File ▸ Search… in the app
(``window.onyxShell.openSearch``), from ⌘P in a browser or with focus inside the reader, and from a ``#search``
fragment on the shell's URL.

It wears the shell's modal look (``panels_ui``) but stands near the top of the window, as Obsidian's does, so the box
stays where it is while results come and go; and it shows at once, with no rise. The script runs inside the shell's
``<script>`` and leans on it: ``$``, ``esc``, ``api``, ``label``, ``navigate``, ``viewHref``, ``openItem``,
``docWhere``, ``switchVault``, ``currentSrc``, ``rootOf``, ``hidePeek``, ``KIND``, ``GROUPS``, ``BOTH``, ``reader``,
``onReaderLoad``, ``openTab``, ``viewOf``, ``EXTRA``, ``SCOPE``, and the outline's ``HEADS`` and ``setOutActive``.
"""

from __future__ import annotations

# lucide's search glyph, on the panels' 24 grid.
SEARCH_ICON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>'
)

PALETTE_CSS = """/* The search palette (⌘P, palette_ui.py): Obsidian's prompt in the modals' look. It stands near the top, so the box
   stays put while results come and go, and it shows at once. Its field is the bar itself, not a boxed input. */
.search-modal{width:min(680px,calc(100vw - 32px));max-height:min(620px,calc(88vh - 24px));margin:12vh auto auto} .search-modal[open]{animation:none}
.sr-bar{display:flex;flex:none;align-items:center;gap:10px;padding:13px 16px 12px;border-bottom:1px solid var(--line-soft)} .sr-bar svg{flex:none;width:16px;height:16px;color:rgb(var(--faint))}
#search-input{flex:1;min-width:0;padding:0;border:0;border-radius:0;background:transparent;color:rgb(var(--ink));font-size:15px;outline:none;box-shadow:none} #search-input::placeholder{color:rgb(var(--faint))}
.sr-scope{flex:none;padding:2px 9px;border:1px solid var(--line);border-radius:999px;background:transparent;color:rgb(var(--muted));font:inherit;font-size:11px;line-height:16px;white-space:nowrap;cursor:default} .sr-scope:hover{color:rgb(var(--ink))}
.sr-scope[aria-pressed=true]{border-color:rgb(var(--accent)/.45);background:rgb(var(--accent)/.12);color:rgb(var(--ink))}
#search-results{flex:1 1 auto;min-height:0;overflow:auto;padding:4px 6px 6px}
.sr-group{margin:10px 10px 4px;color:rgb(var(--faint));font-size:10.5px;font-weight:600;letter-spacing:.06em;text-transform:uppercase} .sr-group:first-child{margin-top:6px}
.sr-row{display:flex;flex-direction:column;gap:2px;padding:7px 10px;border-radius:8px;cursor:default} .sr-row[aria-selected=true]{background:rgb(var(--ink)/.075)}
.sr-t,.sr-l{display:flex;align-items:baseline;gap:10px;min-width:0}
.sr-title{flex:1;min-width:0;overflow:hidden;font-size:13.5px;font-weight:500;white-space:nowrap;text-overflow:ellipsis}
.sr-where{flex:none;max-width:45%;overflow:hidden;color:rgb(var(--muted));font-size:11px;white-space:nowrap;text-overflow:ellipsis}
.sr-sec{flex:1;min-width:0;overflow:hidden;color:rgb(var(--secondary));font-size:12px;white-space:nowrap;text-overflow:ellipsis} .sr-how{flex:none;color:rgb(var(--faint));font-size:10.5px}
.sr-snip{display:-webkit-box;overflow:hidden;color:rgb(var(--muted));font-size:12px;line-height:1.45;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.sr-row mark{padding:0 1px;border-radius:3px;background:rgb(var(--accent)/.2);color:inherit}
.sr-note{padding:12px 10px;color:rgb(var(--muted));font-size:12.5px} .sr-stale{opacity:.5}
.sr-foot{display:flex;flex:none;flex-wrap:wrap;align-items:center;gap:4px 14px;padding:8px 16px;border-top:1px solid var(--line-soft);color:rgb(var(--faint));font-size:11px} .sr-foot kbd{color:rgb(var(--secondary));font:inherit;font-weight:600}
#search-state{margin-left:auto;text-align:right}
@media(max-width:560px){.search-modal{margin-top:24px} .sr-where{display:none}}"""

PALETTE_HTML = """<dialog id=search-modal class="modal search-modal" aria-label="Search notes and artifacts"><div class=sr-bar>__ICON__<input id=search-input type=text placeholder="Search notes and artifacts…" autocomplete=off spellcheck=false role=combobox aria-expanded=true aria-controls=search-results aria-autocomplete=list aria-label="Search notes and artifacts"><button id=search-scope class=sr-scope type=button aria-pressed=false hidden></button></div>
<div id=search-results role=listbox aria-label="Results"></div>
<div class=sr-foot><span><kbd>↑↓</kbd> to navigate</span><span><kbd>↩</kbd> to open</span><span><kbd>⌘↩</kbd> in a new tab</span><span><kbd>esc</kbd> to dismiss</span><span id=search-scope-hint hidden><kbd>⌥S</kbd> vaults</span><span id=search-state></span></div></dialog>"""

PALETTE_JS = r"""// MARK: search palette — ⌘P (palette_ui.py). Titles on every keystroke, passages once typing pauses; a reply that lands
// after a newer query is dropped. The top row is highlighted until ↑↓ or the pointer picks another, and a pick stays on
// its result while the list fills in. ↩ before a query's passages arrive waits for them rather than open an older one.
const SEARCH=(()=>{const dlg=$('#search-modal'),input=$('#search-input'),list=$('#search-results'),state=$('#search-state'),scopeBtn=$('#search-scope');
const HOW={words:'words',meaning:'meaning',both:'words + meaning'};
let rows=[],sel=0,picked='',titles=[],passages=[],recent=[],pending=false,stale=false,openWhenReady=false,titleSeq=0,passSeq=0,titleTimer=0,passTimer=0,status=null,jump=null,jumpTimer=0,missing=[];
// What this search covers: Settings' choice as the box opens, flipped for the one search by the switch. With no other
// vault set up there is nothing to switch, and it searches as it always has.
let scope=SCOPE;
function scoped(){return scope==='all'?BOTH:['notes','html']}
function showScope(){const many=EXTRA.length>0;scopeBtn.hidden=!many;$('#search-scope-hint').hidden=!many;const all=scope==='all';
scopeBtn.textContent=all?'All vaults':'Primary vault';scopeBtn.setAttribute('aria-pressed',String(all));scopeBtn.title=(all?'Searching every vault':'Searching '+GROUPS.notes+' and '+GROUPS.html)+' — click or ⌥S to switch'}
function flipScope(){scope=scope==='all'?'primary':'all';showScope();titles=[];passages=[];refresh();input.focus()}
// A row marks what it matched on: a title holds the query as typed (the sidebar's filter matches it whole), a passage
// found by its words holds each word where a word starts (the last as a prefix), and one found by meaning alone holds
// nothing to mark. Marking every "on" inside "rationale" would only say the box can't read. Escaped piece by piece.
function escRe(s){return s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')}
function asTyped(q){return q?new RegExp('('+escRe(q)+')','giu'):null}
function byWords(q){const ts=q.toLowerCase().split(/\s+/).filter(t=>t.length>1).sort((a,b)=>b.length-a.length);return ts.length?new RegExp('(?<![\\p{L}\\p{N}_])('+ts.map(escRe).join('|')+')','giu'):null}
function marked(text,re){return re?String(text).split(re).map((part,i)=>i%2?`<mark>${esc(part)}</mark>`:esc(part)).join(''):esc(text)}
function where(r){return [GROUPS[r.vault]].concat(r.folder?r.folder.split('/'):[]).join(' › ')}
function head(title,place,re){return `<span class=sr-t><span class=sr-title>${marked(title,re)}</span><span class=sr-where>${esc(place)}</span></span>`}
function draw(){const q=input.value.trim(),out=[];rows=[];
const add=(key,go,body,old)=>{out.push(`<div class=sr-row role=option id=sr-${rows.length} data-i=${rows.length} aria-selected=false>${body}</div>`);rows.push({key,go,old})};
const group=t=>out.push(`<div class=sr-group role=presentation>${esc(t)}</div>`),note=t=>out.push(`<div class=sr-note>${esc(t)}</div>`);
if(!q){if(recent.length){group('Recently opened');for(const d of recent)add('d:'+d.source,inTab=>openItem(d,'',inTab),head(d.title,docWhere(d),null))}else note('Search your notes and artifacts by title, and by what is written in them.')}
else{const typed=asTyped(q),words=byWords(q);if(titles.length){group('Titles');for(const t of titles)add('t:'+t.path,inTab=>openPage(t,'',inTab),head(t.title,where(t),typed))}
if(!(status&&status.words&&!status.words.ok)&&(passages.length||pending)){group('Inside pages');if(!passages.length)note('Searching…');else{out.push(`<div role=group${stale?' class=sr-stale':''}>`);
for(const p of passages){const re=p.match==='meaning'?null:words;add('p:'+p.path+'#'+p.heading,inTab=>openPage(p,p.heading,inTab),head(p.title,where(p),re)+`<span class=sr-l><span class=sr-sec>${esc(p.section)}</span><span class=sr-how>${HOW[p.match]||''}</span></span>`+(p.snippet?`<span class=sr-snip>${marked(p.snippet,re)}</span>`:''),stale)}out.push('</div>')}}
if(!rows.length&&!pending)note(`Nothing matches “${q}”.`)}
list.innerHTML=out.join('');const at=picked?rows.findIndex(r=>r.key===picked):-1;select(at<0?0:at,false,false)}
function select(i,scroll,pick){input.removeAttribute('aria-activedescendant');if(!rows.length){sel=0;return}sel=Math.max(0,Math.min(i,rows.length-1));if(pick)picked=rows[sel].key;
const was=list.querySelector('[aria-selected=true]');if(was)was.setAttribute('aria-selected','false');const el=document.getElementById('sr-'+sel);el.setAttribute('aria-selected','true');input.setAttribute('aria-activedescendant',el.id);if(scroll)el.scrollIntoView({block:'nearest'})}
function choose(i,inTab){const r=rows[i];if(!r)return;dlg.close();r.go(!!inTab)}
async function findTitles(){const q=input.value.trim(),seq=++titleSeq;if(!q)return;let found=[];
try{found=(await Promise.all(scoped().filter(rootOf).map(k=>api('/api/vault/search?vault='+k+'&limit=6&q='+encodeURIComponent(q)).then(d=>d.items.map(i=>({vault:k,path:i.path,title:label(i,k),folder:i.folder||''})))))).flat()}catch(e){}
if(seq!==titleSeq)return;titles=found;draw()}
async function findPassages(){const q=input.value,seq=++passSeq;let d;try{d=await api('/api/search?q='+encodeURIComponent(q)+(EXTRA.length?'&scope='+scope:''))}catch(e){d={items:[],words:{ok:false,reason:e.message},meaning:{ok:false,reason:e.message}}}
if(seq!==passSeq||d.superseded)return;passages=d.items;status={words:d.words,meaning:d.meaning};missing=d.missing||[];pending=false;stale=false;showState();draw();if(openWhenReady){const inTab=openWhenReady==='tab';openWhenReady=false;choose(sel,inTab)}}
function refresh(){picked='';openWhenReady=false;clearTimeout(titleTimer);clearTimeout(passTimer);const q=input.value.trim();
if(!q){titleSeq++;passSeq++;titles=[];passages=[];pending=stale=false;draw();return}titleTimer=setTimeout(findTitles,40);
if(q.length>1){pending=true;stale=passages.length>0;passTimer=setTimeout(findPassages,180)}else{passSeq++;passages=[];pending=stale=false}}
function showState(){const s=status;state.textContent=(!s||!s.words?'':!s.words.ok?'Titles only — '+s.words.reason:!s.meaning.ok?'Titles and words — meaning is off: '+s.meaning.reason:'Titles, words and meaning')+(scope==='all'&&missing.length?' · titles only in '+missing.map(m=>m.split(':')[0]).join(', '):'');state.title=[state.textContent].concat(scope==='all'?missing:[]).join('\n')}
async function loadRecent(){try{recent=((await api('/api/library')).documents||[]).slice(0,8)}catch(e){recent=[]}if(!input.value.trim())draw()}
// A passage opens at its section: the reader stays hidden until the page is in and scrolled there, so it never shows the
// page's top (or the reading position it would put back) first. Already open, it just scrolls. With ⌘, it opens in a
// new tab, which lands the same way.
function openPage(r,heading,inTab){const k=KIND!=='library'&&viewOf(r.vault)!==KIND?viewOf(r.vault):KIND,href=viewHref(r.path,r.vault);if(inTab)openTab(href,{kind:k});else if(k!==KIND)switchVault(k,true);
if(!inTab&&currentSrc()===r.path){if(heading)land(heading);return}
if(heading){jump={path:r.path,heading,frame:reader};reader.style.visibility='hidden';clearTimeout(jumpTimer);jumpTimer=setTimeout(landed,1500)}if(!inTab)navigate(href)}
function land(heading){const want=heading.toLowerCase(),i=HEADS.findIndex(h=>h.text.toLowerCase()===want);if(i<0)return;const el=HEADS[i].el,root=el.ownerDocument.documentElement,was=root.style.scrollBehavior;
for(let d=el.closest('details');d;d=d.parentElement&&d.parentElement.closest('details'))d.open=true;root.style.scrollBehavior='auto';el.scrollIntoView({block:'start'});root.style.scrollBehavior=was;setOutActive(i)}
function landed(){clearTimeout(jumpTimer);const j=jump;jump=null;if(j&&currentSrc()===j.path)land(j.heading);(j?j.frame:reader).style.visibility=''}
function open(){if(window.OnyxMenu)OnyxMenu.close();hidePeek();for(const d of document.querySelectorAll('dialog[open]'))if(d!==dlg)d.close();
if(!dlg.open){scope=SCOPE;missing=[];showScope();dlg.showModal();loadRecent();api('/api/search/status').then(d=>{status=d;showState();draw()}).catch(()=>{});refresh()}input.focus();input.select();return true}
function key(e){if((e.metaKey||e.ctrlKey)&&!e.altKey&&!e.shiftKey&&(e.key||'').toLowerCase()==='p'){e.preventDefault();open()}}
document.addEventListener('keydown',key);
// Run after the shell's own (readerLoaded), so the outline's HEADS are the new page's by the time a passage lands.
onReaderLoad(()=>{try{reader.contentWindow.addEventListener('keydown',key)}catch(e){}if(jump)landed()});
input.addEventListener('input',refresh);
scopeBtn.addEventListener('click',flipScope);
input.addEventListener('keydown',e=>{if(e.altKey&&!e.metaKey&&!e.ctrlKey&&e.code==='KeyS'&&EXTRA.length){e.preventDefault();flipScope();return}if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();if(rows.length)select((sel+(e.key==='ArrowDown'?1:rows.length-1))%rows.length,true,true)}
else if(e.key==='Enter'&&!e.isComposing){e.preventDefault();const r=rows[sel],inTab=e.metaKey||e.ctrlKey;if(pending&&(!r||r.old))openWhenReady=inTab?'tab':true;else choose(sel,inTab)}});
list.addEventListener('mousedown',e=>e.preventDefault());
list.addEventListener('mousemove',e=>{const r=e.target.closest('.sr-row');if(r&&+r.dataset.i!==sel)select(+r.dataset.i,false,true)});
list.addEventListener('click',e=>{const r=e.target.closest('.sr-row');if(r)choose(+r.dataset.i,e.metaKey||e.ctrlKey)});
dlg.addEventListener('click',e=>{if(e.target===dlg)dlg.close()});
dlg.addEventListener('close',()=>{clearTimeout(titleTimer);clearTimeout(passTimer);titleSeq++;passSeq++;pending=openWhenReady=false});
if(location.hash==='#search'){history.replaceState(null,'',location.pathname+location.search);open()}
return {open}})();"""


def palette_style() -> str:
    return PALETTE_CSS


def palette_markup() -> str:
    return PALETTE_HTML.replace("__ICON__", SEARCH_ICON)


def palette_script() -> str:
    return PALETTE_JS
