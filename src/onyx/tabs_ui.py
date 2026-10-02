"""Tabs: several notes and artifacts open at once, each in a reader frame of its own, behind a hidden, pinnable bar.

Every tab owns an iframe stacked in the reader's place (``#stage``), so a tab keeps its page, its place in it, and a
conversation still streaming while another tab shows. The tab showing is the shell's ``reader`` and wears
``id=reader``; the rest are hidden (``visibility``, never ``display:none``: a frame with no size would tell ask.js its
page scrolled to the top) and ``inert``.

The sidebar's rows stay plain ``<a target=reader>`` links. A frame keeps the name it was born with, and a click points
the link at the frame showing, so the browser still does the navigating and keeps the history. Renaming the frames
instead looked simpler and fails in the app: WebKit 17 files joint history under frame *names*, so once two frames
had swapped names, Back loaded one tab's page into the other (measured 2026-09-21, system WebKit 17.5; Playwright's
newer WebKit and Chromium both got it right). History is joint — one Back for the window, stepping whichever frame
moved last — so when that is a tab behind, it comes forward.

The bar behaves as the app's other two do. Unpinned (the default) it is away, and comes out as a card centred at the
top of the reader after a beat with the pointer in the top 24 px of the reader's middle 60 %, watched from the reader's
own pointer moves as the outline's edge is: an overlay strip would cover the context pill, the find bar and the
outline's toggle in the top-right corner. It goes a moment after the pointer leaves, unless it is in use — its list
open, or focus inside it. Pinned (⌘⌥\\ or its pin), it is a band above the reader and the reader moves down under it.
The title band of the app's window passes clicks to the page everywhere but the traffic lights (measured 2026-09-21),
so a pill there takes its click, and the band's empty space drags the window. A pill whose title is cut widens under the
pointer, pushing the others aside, and rolls a title still cut; the script's ``tab hover`` mark says why it is built so.

The script runs inside the shell's ``<script>`` and leans on it: ``$``, ``esc``, ``store``, ``recall``, ``reader``
(reassigned here), ``stage``, ``KIND``, ``VAULTS``, ``switchVault``, ``showHome``, ``empty``, ``home``, ``goHome``,
``highlight``, ``traversed``, ``readerLoaded``, ``onReaderLoad``, ``api``, ``viewHref``, ``navigate``, ``readerPage``
and ``pageOnly`` (⌘B: no bar comes out); the shell adds ``TAB_SHELL`` to ``window.onyxShell``, runs ``restoreTabs`` as
it starts, and ``remapTabs`` when the sidebar moves or renames a page.
"""

from __future__ import annotations

import json

from .find_ui import DOWN_ICON
from .panels_ui import ICONS

# lucide's plus, on the panels' 24 grid.
PLUS_ICON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14"/><path d="M12 5v14"/></svg>'
)

TABS_CSS = """/* Tabs (tabs_ui.py). The reader's pane is two rows: the bar's, empty until it is pinned, then the stage. Its one
   column is held to the pane: an `auto` one grew to a pinned bar's pills and took the reader past the pane's edge. */
#reader-pane{--tabs-h:40px;grid-template-columns:minmax(0,1fr);grid-template-rows:0 minmax(0,1fr);transition:grid-template-rows .15s cubic-bezier(.2,.8,.2,1)} #stage{grid-row:2}
body.tabs-pinned #reader-pane{grid-template-rows:var(--tabs-h) minmax(0,1fr)}
/* Unpinned, the bar lies over the reader's top and only its centred card takes the pointer, so it never covers the
   context pill, the find bar or the outline's toggle. Pinned, it is a band of its own whose empty space drags the window. */
#tab-bar{position:absolute;top:0;right:0;left:0;z-index:5;display:flex;justify-content:center;align-items:flex-start;height:var(--tabs-h);padding:8px 12px 0;pointer-events:none}
body.tabs-pinned #tab-bar{position:relative;grid-row:1;align-items:center;padding:0 12px;border-bottom:1px solid var(--line-soft);pointer-events:auto}
body.native.side-unpinned.tabs-pinned #tab-bar{padding-left:84px}
.tab-group{display:flex;align-items:center;gap:2px;min-width:0;max-width:60%;padding:3px;border:1px solid var(--line);border-radius:12px;background:rgb(var(--bg-sidebar)/.96);box-shadow:0 18px 50px rgb(0 0 0/.24),0 2px 8px rgb(0 0 0/.08);backdrop-filter:blur(24px) saturate(1.3);-webkit-backdrop-filter:blur(24px) saturate(1.3);pointer-events:auto;visibility:hidden;transform:translateY(calc(-100% - 12px))}
@media(prefers-reduced-transparency:reduce){.tab-group{background:rgb(var(--bg-sidebar));backdrop-filter:none;-webkit-backdrop-filter:none}}
body.tabs-out .tab-group,body.tabs-pinned .tab-group{visibility:visible;transform:none}
body.tabs-pinned .tab-group{max-width:100%;padding:0;border-color:transparent;background:transparent;box-shadow:none;backdrop-filter:none;-webkit-backdrop-filter:none}
/* Motion, as the sidebar's: out with an ease-out slide, away with a quicker ease-in one, hidden once it is off. */
.tab-group{transition:transform .13s cubic-bezier(.4,0,1,1),visibility 0s linear .13s} body.tabs-out .tab-group{transition:transform .15s cubic-bezier(.2,.8,.2,1),visibility 0s}
body.tabs-pinned .tab-group,body.tabs-still .tab-group,body.tabs-still #reader-pane{transition:none!important}
@media(prefers-reduced-motion:reduce){body:not(.tabs-pinned) .tab-group{transform:none;opacity:0;transition:opacity .15s linear,visibility 0s linear .15s} body.tabs-out:not(.tabs-pinned) .tab-group{opacity:1;transition:opacity .15s linear,visibility 0s} #reader-pane{transition:none}}
/* The pills: a segmented strip, as the panel's Outline/Related switch — the tab showing on the raised ground, in ink.
   A pill's 180 px is a width, not a flex-basis: the strip sizes itself from its pills' widths, and a basis alone left
   every pill at its 88 px floor however much room there was. */
#tab-strip{display:flex;align-items:center;gap:2px;min-width:0;padding:2px;overflow:hidden;border-radius:9px;background:rgb(var(--ink)/.055)} .tab[hidden]{display:none}
.tab{display:flex;align-items:center;flex:0 1 auto;gap:4px;width:180px;min-width:88px;height:26px;padding:0 3px 0 10px;border-radius:7px;color:rgb(var(--secondary));font-size:12px;font-weight:500;cursor:default;user-select:none;-webkit-user-select:none;transition:background-color .12s,color .12s}
.tab:hover{background:rgb(var(--ink)/.05);color:rgb(var(--ink))} .tab[aria-selected=true]{background:rgb(var(--bg-elevated));color:rgb(var(--ink));font-weight:600;box-shadow:0 1px 2px rgb(0 0 0/.1)}
.tab:focus-visible{outline:2px solid rgb(var(--accent));outline-offset:-2px} .tab-title{flex:1;min-width:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
/* A cut title, hovered (tabHover in the script): the pills' widths are set there while the strip is held, and a title
   still cut rolls to its end and back on the compositor, holding at each end, then glides home as the pill falls back. */
#tab-strip.tabs-held .tab{flex:none} .tab-title.tab-rolling,.tab-title.tab-unrolling{text-overflow:clip}
.tab-title.tab-rolling .tab-text,.tab-title.tab-unrolling .tab-text{display:inline-block;will-change:transform}
.tab-title.tab-rolling .tab-text{animation:tab-roll var(--roll-dur,4s) ease-in-out infinite alternate}
@keyframes tab-roll{0%,18%{transform:none}82%,100%{transform:translateX(var(--roll,0))}}
@media(prefers-reduced-motion:reduce){.tab-title.tab-rolling .tab-text{animation:none}}
.tab-x{display:grid;flex:none;place-items:center;width:18px;height:18px;padding:0;border:0;border-radius:5px;background:transparent;color:rgb(var(--faint));opacity:0;transition:opacity .12s}
.tab:hover .tab-x,.tab[aria-selected=true] .tab-x{opacity:1} .tab-x:hover{background:rgb(var(--ink)/.1);color:rgb(var(--ink))} .tab-x svg{width:12px;height:12px}
.tab-btn{display:grid;flex:none;place-items:center;width:26px;height:26px;padding:0;border:0;border-radius:7px;background:transparent;color:rgb(var(--secondary))}
.tab-btn:hover,.tab-btn[aria-expanded=true]{background:rgb(var(--ink)/.08);color:rgb(var(--ink))} .tab-btn svg{width:15px;height:15px} .tab-btn:focus-visible{outline:2px solid rgb(var(--accent));outline-offset:-2px}
#tab-pin[aria-pressed=true] .pin-head{fill:currentColor}"""

TABS_HTML = """<div id=tab-bar data-drag><div class=tab-group data-nodrag><div id=tab-strip role=tablist aria-label="Open tabs"></div><button id=tab-new class=tab-btn type=button title="New tab (⌘T)" aria-label="New tab">__PLUS__</button><button id=tab-list class=tab-btn type=button title="All tabs" aria-label="All tabs" aria-haspopup=menu aria-expanded=false>__DOWN__</button><button id=tab-pin class=side-toggle type=button aria-pressed=false title="Pin tab bar (⌘⌥\\)" aria-label="Pin tab bar">__PIN__</button></div></div>"""

TABS_JS = r"""// MARK: tabs — a frame per tab in #stage (tabs_ui.py). The first adopts the frame the server drew, named `reader`;
// the rest are named once, at birth, and never again (see the module's note on WebKit's history).
const TABS={list:[],active:null},TAB_RUN=Date.now().toString(36),TAB_X=__X__;let tabSeq=0;
function makeTab(o){const id=++tabSeq,t=Object.assign({id,name:'reader-'+TAB_RUN+'-'+id,frame:null,href:'',src:'',folder:'',kind:KIND,title:'',loaded:false,used:Date.now()},o);
if(t.href&&!t.src){const q=new URLSearchParams((t.href.split('?')[1]||'').split('#')[0]);t.src=q.get('src')||'';t.folder=q.get('folder')||''}return t}
function tabById(id){return TABS.list.find(t=>t.id===id)||null}
function wireFrame(t,f){t.frame=f;f.addEventListener('load',()=>{tellPainted();try{f.contentWindow.addEventListener('popstate',tellPainted)}catch(e){}if(t.frame===f)frameLoaded(t)})}
// A frame after a page lands in a tab, the app hears it is on screen: a back/forward swipe holds a picture of it over
// the window until then (SwipeCover in Onyx.swift), because WebKit lifts its own once the shell has painted, before the
// frame has. A traversal inside one page loads nothing, so it says so on popstate.
function tellPainted(){const h=window.webkit&&window.webkit.messageHandlers&&window.webkit.messageHandlers.askwPainted;
if(h)requestAnimationFrame(()=>requestAnimationFrame(()=>Promise.resolve(h.postMessage({})).catch(()=>{})))}
// A tab's frame, made when the tab is opened or first shown. Its src is set before it joins the page, so the load that
// follows is the page's own and adds nothing to history.
function tabFrame(t,href){const f=document.createElement('iframe');f.name=t.name;f.setAttribute('aria-label','Reader');f.inert=true;f.src=href||'about:blank';
const frames=stage.querySelectorAll(':scope > iframe');(frames.length?frames[frames.length-1]:home).after(f);wireFrame(t,f);return f}
// Every frame's load: the tab learns what it holds; the one showing drives the shell, and one behind that Back or Forward
// just moved comes forward.
function frameLoaded(t){const f=t.frame;t.loaded=true;let l=null;try{l=f.contentWindow.location}catch(e){}
const blank=!l||!l.href||l.href==='about:blank',q=new URLSearchParams(blank?'':l.search);
t.href=blank?'':l.pathname+l.search+l.hash;t.src=q.get('src')||'';t.folder=q.get('folder')||'';
let title='';try{title=f.contentDocument.title}catch(e){}t.title=title||(t.src?t.src.split('/').pop():'');
if(f===reader)readerLoaded();else if(!blank&&traversed(f)){activateTab(t);return}tabsChanged()}
// Bring a tab forward: its frame becomes the reader, the sidebar goes back to the view it was read in, and everything
// that follows the reader follows it (readerLoaded), as a load would. A tab not shown since a restore gets its frame now.
function activateTab(t){if(!t||t===TABS.active||!TABS.list.includes(t))return;const was=TABS.active;if(was){was.kind=KIND;was.used=Date.now()}
if(!t.frame)tabFrame(t,t.href);
if(reader&&reader!==t.frame)reader.removeAttribute('id');for(const x of TABS.list)if(x.frame)x.frame.inert=x!==t;t.frame.id='reader';
reader=t.frame;TABS.active=t;t.used=Date.now();previewPageLook(t.href);
if(t.kind&&t.kind!==KIND&&VAULTS[t.kind])switchVault(t.kind,true);
if(t.loaded||!t.href)readerLoaded();else{showHome(false);empty.hidden=true;highlight(t.src)}
evictTabs();tabsChanged()}
// A new tab beside the one showing, reading `href` (a blank one is Library's home page), brought forward unless told not to.
function openTab(href,o){o=o||{};const at=TABS.list.indexOf(TABS.active),t=makeTab({href:href||'',kind:o.kind||KIND});TABS.list.splice(at+1,0,t);
tabFrame(t,href);if(o.background)tabsChanged();else activateTab(t);return t}
// Closing the tab showing brings its right-hand neighbour forward, or its left. The last tab is never closed: it goes
// home instead, and a lone home tab says no, so the window can close in its place.
function closeTab(t){t=t||TABS.active;const i=TABS.list.indexOf(t);if(i<0)return false;
if(TABS.list.length===1){if(KIND==='library'&&!home.hidden)return false;if(KIND==='library')goHome();else switchVault('library');return true}
if(t===TABS.active)activateTab(TABS.list[i+1]||TABS.list[i-1]);TABS.list.splice(i,1);if(t.frame)t.frame.remove();t.frame=null;tabsChanged();return true}
function closeOtherTabs(){for(const t of TABS.list.slice())if(t!==TABS.active)closeTab(t)}
// A plain `target=reader` link goes to the tab showing: pointed there as it is clicked (Return on a focused link clicks it too).
document.addEventListener('click',e=>{const a=e.target instanceof Element?e.target.closest('a[target]'):null;if(a&&reader&&/^reader(-|$)/.test(a.target))a.target=reader.name},true);
{const t=makeTab({name:'reader',href:reader.getAttribute('src')==='about:blank'?'':reader.getAttribute('src')||''});TABS.list.push(t);TABS.active=t;wireFrame(t,reader)}
// MARK: tab bar — the pills, one per tab, the tab showing marked; + for a new one and ⌄ for the whole list.
const tabBar=$('#tab-bar'),tabGroup=tabBar.querySelector('.tab-group'),tabStrip=$('#tab-strip'),tabPin=$('#tab-pin'),tabList=$('#tab-list');
function tabLabel(t){return t.title||(t.src?t.src.split('/').pop():t.href?'Loading…':'New Tab')}
function drawTabs(){const had=tabStrip.contains(document.activeElement),ps=[...tabStrip.children],same=ps.length===TABS.list.length&&TABS.list.every((t,i)=>+ps[i].dataset.id===t.id),keep=same&&tabHover;tabsFree();
// The same tabs (one brought forward, a title come in) are redrawn in place, never replaced: WebKit loses the hover of a
// node replaced under the pointer and never tells the strip it left, so a pill clicked while wide stayed wide (2026-09-25).
if(same)TABS.list.forEach((t,i)=>{const p=ps[i],on=t===TABS.active,l=tabLabel(t);p.setAttribute('aria-selected',on);p.tabIndex=on?0:-1;if(p.getAttribute('aria-label')===l)return;
p.setAttribute('aria-label',l);if(!p.classList.contains('tab-wide'))p.title=l;p.querySelector('.tab-text').textContent=l;p.querySelector('.tab-x').setAttribute('aria-label','Close '+l)});
else{tabHover=null;tabStrip.innerHTML=TABS.list.map(t=>{const on=t===TABS.active,l=esc(tabLabel(t));
return `<div class=tab role=tab id=tab-${t.id} data-id=${t.id} aria-selected=${on} tabindex=${on?0:-1} title="${l}" aria-label="${l}"><span class=tab-title><span class=tab-text>${l}</span></span><button class=tab-x type=button tabindex=-1 title="Close tab" aria-label="Close ${l}">${TAB_X}</button></div>`}).join('')}
fitTabs();if(had){const p=TABS.active&&document.getElementById('tab-'+TABS.active.id);if(p)p.focus()}
// The pill under the pointer stays as it was, measured again: a title shown in bold is a little wider.
if(keep){if(keep.hidden){tabUnwide(keep,true);tabHover=null}else tabPick(keep,true)}}
// Past what the bar holds (a pill gives up width down to 88 px), pills spill into the ⌄ list, which lists every tab:
// those furthest from the tab showing go first, so it always has its pill. Measured again as the bar changes width.
function fitTabs(){const ps=[...tabStrip.children],at=ps.findIndex(p=>p.getAttribute('aria-selected')==='true');let lo=0,hi=ps.length-1;for(const p of ps)p.hidden=false;
while(tabStrip.scrollWidth>tabStrip.clientWidth+1&&lo<hi){if(at-lo>hi-at)ps[lo++].hidden=true;else ps[hi--].hidden=true}
const more=ps.filter(p=>p.hidden).length,label=more?`All tabs (${more} more)`:'All tabs';tabList.title=label;tabList.setAttribute('aria-label',label)}
function tabsChanged(){drawTabs();saveTabs()}
// MARK: tab hover — a pill whose title is cut widens at once toward its full title (360 px at most) and the others give
// way down to their 88 px floor, pushed aside rather than covered, as Obsidian's are; a title still cut rolls. Leaving,
// they fall back more slowly than they grew. Ported from Meeting Copilot (present/index.ts, evStripMove), where each rule
// below was a bug Chris hit (2026-09-24):
// - The strip's width is held from the first real move over it until the fall-back after leaving has finished. The bar
//   is centred, so a strip that grew slid the pill out from under the pointer; letting go at once snapped the others
//   back and re-centred the bar mid-collapse.
// - Only real pointer movement picks a pill: a mousemove at a new spot. A pill that slides under a still pointer is not
//   chosen, and mouseenter fires for exactly that, which cascaded a sweep into more resizes. So there is no delay.
// - A widened pill stays until the pointer moves onto another, and the two swap in one step.
// - The title rolls with transform on an inner span; text-indent re-laid it out every frame, in whole pixels.
// Unlike there, every visible pill's width is set here while the strip is held, all eased by one animation from where
// they are, so the widths always add up to the strip. Meeting Copilot eases only the hovered pill's width and flips its
// flex-shrink at once, which makes both pills jump on the first frame of a swap or a fall-back (measured there,
// 2026-09-25: 373→256 px on a swap, 302→255 px on leaving).
const TAB_MAX=360,TAB_MIN=88,TAB_GROW=[200,'cubic-bezier(.2,.8,.2,1)'],TAB_FALL=[320,'cubic-bezier(.4,0,.2,1)'];
let tabHover=null,tabRest=null,tabPt=null,tabRelease=0;
function tabsHold(){clearTimeout(tabRelease);if(tabRest)return;const ps=[...tabStrip.querySelectorAll('.tab:not([hidden])')],ws=ps.map(p=>p.getBoundingClientRect().width);
tabStrip.style.width=getComputedStyle(tabStrip).width;tabStrip.classList.add('tabs-held');tabRest=new Map(ps.map((p,i)=>[p,ws[i]]));ps.forEach((p,i)=>{p.style.width=ws[i]+'px'})}
function tabsFree(){clearTimeout(tabRelease);if(!tabRest)return;for(const p of tabRest.keys()){if(p._tabGrow)p._tabGrow.cancel();p._tabGrow=null;p.style.width=''}
tabStrip.classList.remove('tabs-held');tabStrip.style.width='';tabRest=null}
// The padding, gap and × around a pill's title, and how wide its title would like to be.
function tabChrome(p){return p.getBoundingClientRect().width-p.querySelector('.tab-title').getBoundingClientRect().width}
// Where every pill goes with `p` widened: `p` to its full title, as far as the others can give at their floor, and the
// others giving it up in proportion to what each has above the floor. Null when its title fits or there is no room.
function tabWidths(p){const text=p.querySelector('.tab-text'),rest=tabRest.get(p);if(!text||rest==null)return null;
const natural=text.getBoundingClientRect().width+tabChrome(p)+2;if(natural<=rest+1)return null;let total=0,slack=0;
for(const [q,w] of tabRest){total+=w;if(q!==p)slack+=w-TAB_MIN}const want=Math.min(TAB_MAX,natural,total-(tabRest.size-1)*TAB_MIN);if(want<=rest+1)return null;
const give=want-rest;return new Map([...tabRest].map(([q,w])=>[q,q===p?want:w-give*(w-TAB_MIN)/slack]))}
// One animation for every pill, from the widths they are at now: read them all before any is changed.
function tabsTween(ws,[ms,ease]){const ps=[...tabRest.keys()],from=ps.map(p=>p.getBoundingClientRect().width);if(stillMotion.matches)ms=0;
ps.forEach((p,i)=>{const to=ws?ws.get(p):tabRest.get(p);if(p._tabGrow)p._tabGrow.cancel();p.style.width=to+'px';
p._tabGrow=ms&&Math.abs(from[i]-to)>.5?p.animate([{width:from[i]+'px'},{width:to+'px'}],{duration:ms,easing:ease}):null})}
// A title still cut once its pill has grown rolls to its end and back, paced by how much is hidden.
function tabRoll(p,w,delay){const title=p.querySelector('.tab-title'),text=p.querySelector('.tab-text'),over=text.getBoundingClientRect().width-(w-tabChrome(p));clearTimeout(p._tabRoll);
if(over<=1||stillMotion.matches){title.classList.remove('tab-rolling');return}const go=()=>{if(!p.classList.contains('tab-wide'))return;
title.style.setProperty('--roll',(-over-4)+'px');title.style.setProperty('--roll-dur',Math.max(2.5,over/30+1.5).toFixed(1)+'s');title.classList.add('tab-rolling')};
if(delay)p._tabRoll=setTimeout(go,delay);else go()}
// Back to a pill's own width: its tooltip returns, and a rolled title glides home from wherever it had got to.
function tabUnwide(p,still){if(!p.classList.contains('tab-wide'))return;p.classList.remove('tab-wide');p.title=p.getAttribute('aria-label')||'';clearTimeout(p._tabRoll);
const title=p.querySelector('.tab-title'),text=p.querySelector('.tab-text');if(!title.classList.contains('tab-rolling'))return;
const at=getComputedStyle(text).transform;title.classList.remove('tab-rolling');if(still||stillMotion.matches||at==='none')return;
title.classList.add('tab-unrolling');const a=text.animate([{transform:at},{transform:'none'}],{duration:TAB_FALL[0],easing:TAB_FALL[1]});a.onfinish=()=>title.classList.remove('tab-unrolling')}
// The pointer picked `p`: the pill widened before falls back as `p` widens, in one step. `still` puts it there at once.
function tabPick(p,still){tabsHold();tabHover=p;const ws=tabWidths(p);for(const q of tabRest.keys())if(q!==p||!ws)tabUnwide(q,still);
tabsTween(ws,still?[0]:ws?TAB_GROW:TAB_FALL);if(!ws)return;const was=p.classList.contains('tab-wide');p.classList.add('tab-wide');p.removeAttribute('title');
tabRoll(p,ws.get(p),was?0:TAB_GROW[0]+20)}
function tabLeave(){tabHover=null;tabPt=null;if(!tabRest)return;for(const q of tabRest.keys())tabUnwide(q);tabsTween(null,TAB_FALL);
clearTimeout(tabRelease);tabRelease=setTimeout(()=>{if(!tabHover)tabsFree()},TAB_FALL[0]+40)}
// Straight back, with no fall-back, when the bar itself changes width.
function tabsSettle(){for(const q of tabStrip.querySelectorAll('.tab.tab-wide'))tabUnwide(q,true);tabsFree();tabHover=null}
tabStrip.addEventListener('mousemove',e=>{if(tabPt&&tabPt.x===e.clientX&&tabPt.y===e.clientY)return;tabPt={x:e.clientX,y:e.clientY};
const p=e.target.closest('.tab');if(p&&p!==tabHover&&!p.hidden)tabPick(p)},{passive:true});
tabStrip.addEventListener('mouseleave',tabLeave);
tabStrip.addEventListener('click',e=>{const p=e.target.closest('.tab');if(!p)return;const t=tabById(+p.dataset.id);if(e.target.closest('.tab-x'))closeTab(t);else activateTab(t)});
// The middle button closes a pill, as in every browser; its mousedown is kept from starting the page's autoscroll.
tabStrip.addEventListener('mousedown',e=>{if(e.button===1)e.preventDefault()});
tabStrip.addEventListener('auxclick',e=>{const p=e.button===1&&e.target.closest('.tab');if(p){e.preventDefault();closeTab(tabById(+p.dataset.id))}});
tabStrip.addEventListener('keydown',e=>{const p=e.target.closest('.tab');if(!p)return;const i=TABS.list.indexOf(tabById(+p.dataset.id)),n=TABS.list.length;
const to=e.key==='ArrowRight'?(i+1)%n:e.key==='ArrowLeft'?(i+n-1)%n:e.key==='Home'?0:e.key==='End'?n-1:-1;
if(to>=0){e.preventDefault();activateTab(TABS.list[to]);const q=document.getElementById('tab-'+TABS.list[to].id);if(q)q.focus()}
else if(e.key==='Delete'||e.key==='Backspace'){e.preventDefault();closeTab(TABS.list[i])}});
$('#tab-new').onclick=()=>openTab('',{kind:'library'});
if(window.ResizeObserver)new ResizeObserver(()=>{tabsSettle();fitTabs()}).observe(tabBar);
// ⌄: every tab, the one showing checked, and Close Other Tabs. The bar stays out while the list is open.
let tabMenuOpen=false;
tabList.onclick=()=>{if(!window.OnyxMenu)return;const r=tabList.getBoundingClientRect(),items=TABS.list.map(t=>({id:'tab:'+t.id,label:tabLabel(t),checked:t===TABS.active}));
items.push({separator:true},{id:'close-others',label:'Close Other Tabs',enabled:TABS.list.length>1});tabMenuOpen=true;tabList.setAttribute('aria-expanded','true');
OnyxMenu.open({items,x:r.left,y:r.bottom+4,label:'Tabs',returnFocus:tabList,onClose:()=>{tabMenuOpen=false;tabList.setAttribute('aria-expanded','false');if(!tabsOver)tabsLater()},
onSelect:id=>{if(id==='close-others')closeOtherTabs();else activateTab(tabById(+id.slice(4)))}})};
// MARK: tab bar reveal and pin — as the sidebar's (a 40 ms beat to come out, 400 ms of grace to go, polled again while in
// use), watched from the reader's own pointer moves as the outline's edge is; on the home page, from the stage's.
const TABBAR_KEY='askw:vault:tabbar';let tabsOver=false,tabsTimer=0,tabsAtEdge=false;
function tabsPinned(){return document.body.classList.contains('tabs-pinned')}
function tabsOut(out){document.body.classList.toggle('tabs-out',out);tabGroup.inert=!tabsPinned()&&!out;if(!out&&!tabsPinned())tabLeave()}
function tabsInUse(){const a=document.activeElement;return !!((tabMenuOpen&&window.OnyxMenu&&OnyxMenu.isOpen())||(a&&tabBar.contains(a)))}
function tabsLater(){clearTimeout(tabsTimer);if(tabsPinned())return;tabsTimer=setTimeout(function check(){if(tabsPinned()||tabsOver)return;if(tabsInUse()){tabsTimer=setTimeout(check,400);return}tabsOut(false)},400)}
function setTabsPinned(on){document.body.classList.toggle('tabs-pinned',on);store(TABBAR_KEY,on?'pinned':'');tabPin.setAttribute('aria-pressed',String(on));const verb=on?'Unpin':'Pin';tabPin.title=verb+' tab bar (⌘⌥\\)';tabPin.setAttribute('aria-label',verb+' tab bar');
clearTimeout(tabsTimer);tabsAtEdge=false;tabsOut(!on&&(tabsOver||tabsInUse()));if(!on&&!tabsOver)tabsLater()}
// The top 24 px of the reader's middle 60 %: clear of the traffic lights on the left and the corner's floaters on the right.
function tabsZone(x,y,w){return y>=0&&y<=24&&Math.abs(x-w/2)<=w*.3}
function tabsAt(at){if(at===tabsAtEdge)return;tabsAtEdge=at;clearTimeout(tabsTimer);if(at)tabsTimer=setTimeout(()=>tabsOut(true),40);else if(document.body.classList.contains('tabs-out')&&!tabsOver)tabsLater()}
function tabsEdgeMove(e){if(tabsPinned()||pageOnly())return;const w=e.view||window;if(w===window){const r=stage.getBoundingClientRect(),t=e.target;tabsAt(!(t.closest&&t.closest('.find-bar,.outline-toggle'))&&tabsZone(e.clientX-r.left,e.clientY-r.top,r.width));return}tabsAt(tabsZone(e.clientX,e.clientY,w.innerWidth))}
function tabsEdgeLeave(){if(!tabsPinned())tabsAt(false)}
function tabsKey(e){if(e.code==='Backslash'&&e.altKey&&(e.metaKey||e.ctrlKey)&&!e.shiftKey){e.preventDefault();setTabsPinned(!tabsPinned())}}
stage.addEventListener('mousemove',tabsEdgeMove,{passive:true});stage.addEventListener('mouseleave',tabsEdgeLeave);
tabGroup.addEventListener('mouseenter',()=>{tabsOver=true;clearTimeout(tabsTimer)});tabGroup.addEventListener('mouseleave',()=>{tabsOver=false;tabsLater()});
tabGroup.addEventListener('focusout',()=>{if(!tabsOver)tabsLater()});
tabGroup.addEventListener('keydown',e=>{if(e.key==='Escape'&&!tabsPinned()){document.activeElement.blur();tabsOut(false)}});
tabPin.onclick=()=>setTabsPinned(!tabsPinned());document.addEventListener('keydown',tabsKey);
onReaderLoad(()=>{tabsAtEdge=false;try{const w=reader.contentWindow;w.addEventListener('mousemove',tabsEdgeMove,{passive:true});w.addEventListener('keydown',tabsKey);w.document.documentElement.addEventListener('mouseleave',tabsEdgeLeave)}catch(e){}});
// MARK: tab entry points — ⌘-click or a middle click on a link to a page opens it in a new tab, in the shell and inside
// the page alike (the row menu's Open in New Tab, ⌘ on a Related row and ⌘↩ in the palette are their own). Left to the
// browser, either asks for a new window, and the app used to load the page over the whole shell. A plain click is
// never touched: it is the browser's own navigation, in the tab showing.
function modClick(e){const mid=e.type==='auxclick'&&e.button===1,mod=e.type==='click'&&e.button===0&&(e.metaKey||e.ctrlKey);if(!mid&&!mod)return;
const el=e.target,a=el&&el.closest?el.closest('a[href]'):null;if(!a)return;let u;try{u=new URL(a.href)}catch(err){return}
if(u.origin!==location.origin||!/^\/(view|quick)$/.test(u.pathname))return;e.preventDefault();e.stopPropagation();openTab(u.pathname+u.search+u.hash)}
document.addEventListener('click',modClick,true);document.addEventListener('auxclick',modClick,true);
// ⌘⇧] and ⌘⇧[ step through the tabs, ⌘1–8 go to that one, ⌘9 to the last, as in Safari; from the shell and the reader.
function stepTab(d){const n=TABS.list.length;if(n>1)activateTab(TABS.list[(TABS.list.indexOf(TABS.active)+d+n)%n]);return true}
function tabKey(e){if(!(e.metaKey||e.ctrlKey)||e.altKey)return;const n=TABS.list.length;let to=null;
if(e.shiftKey&&(e.code==='BracketRight'||e.code==='BracketLeft')){e.preventDefault();stepTab(e.code==='BracketRight'?1:-1);return}
if(!e.shiftKey&&/^Digit[1-9]$/.test(e.code)){const d=+e.code.slice(5);to=TABS.list[d===9?n-1:d-1]}if(!to)return;e.preventDefault();activateTab(to)}
document.addEventListener('keydown',tabKey);
onReaderLoad(()=>{try{const w=reader.contentWindow;w.addEventListener('click',modClick,true);w.addEventListener('auxclick',modClick,true);w.addEventListener('keydown',tabKey)}catch(e){}});
// A page from outside — Finder, File ▸ Open, Alfred — comes forward in the tab already reading it, or opens in a new
// one; a tab resting on the home page takes it instead, as a browser's empty tab does. The service maps a real file to
// the row a vault lists it as (the tree's nodes carry vault paths, not real ones), so the tree highlights it.
async function openInTab(path){let loc=null;try{loc=await api('/api/vault/locate?src='+encodeURIComponent(path))}catch(e){}
const k=loc&&loc.vault,src=loc&&loc.path||path,have=TABS.list.find(t=>t.src===src);if(have){activateTab(have);return}
openHref(k?viewHref(src,k):'/view?src='+encodeURIComponent(src),k&&k===KIND?KIND:'library')}
function openHref(href,k){k=k||KIND;const t=TABS.active;if(t&&!t.href&&!readerPage()){if(k!==KIND)switchVault(k,true);navigate(href);return}openTab(href,{kind:k})}
// What the app's menu reaches (window.onyxShell): File ▸ New Tab and Close Tab, Window ▸ Show Next and Previous Tab, and
// a new-window request the page made anyway (Onyx.swift, createWebViewWith). Close Tab answers false on a lone home tab.
const TAB_SHELL={newTab:()=>{openTab('',{kind:'library'});return true},closeTab:()=>closeTab(),nextTab:()=>stepTab(1),prevTab:()=>stepTab(-1),
openInTab:path=>{openInTab(path);return true},openHref:href=>{openHref(href);return true},pinTabs:()=>{setTabsPinned(!tabsPinned());return true}};
// Put back as it was left without a glide: pinned under tabs-still, lifted once the first frame has painted.
if(recall(TABBAR_KEY)==='pinned'){document.body.classList.add('tabs-still');setTabsPinned(true);requestAnimationFrame(()=>requestAnimationFrame(()=>document.body.classList.remove('tabs-still')))}else tabsOut(false);
// MARK: tabs kept — the open tabs outlive a reload and a relaunch (askw:vault:tabs): each by its page, without the
// history=… of a conversation it was opened to replay, and a long /quick selection not at all. Only the tab showing
// gets a frame; the rest come back as stubs and get theirs when first shown, their reading position put back by the
// service (the page brings it, app.py first_paint), so nothing is lost. At most TAB_LIVE frames stay alive: past that,
// the one shown longest ago gives its frame up — never one whose answer panel is open or still streaming.
const TABS_KEY='askw:vault:tabs',TAB_LIVE=6;
function savedHref(h){if(!h||!/[?&]history(_action)?=/.test(h))return h;const u=new URL(h,location.origin);u.searchParams.delete('history');u.searchParams.delete('history_action');return u.pathname+u.search+u.hash}
function saveTabs(){const tabs=[];let active=0;for(const t of TABS.list){const href=savedHref(t.href);if(href.startsWith('/quick')&&href.length>2048)continue;if(t===TABS.active)active=tabs.length;
tabs.push({href,src:t.src,folder:t.folder,kind:t===TABS.active?KIND:t.kind,title:t.title})}store(TABS_KEY,JSON.stringify({v:1,active,tabs}))}
addEventListener('pagehide',saveTabs);
function tabBusy(t){try{return !!t.frame.contentDocument.querySelector('.askw-panel.open,.askw-panel[aria-busy=true]')}catch(e){return false}}
function dropFrame(t){t.frame.remove();t.frame=null;t.loaded=false}
function evictTabs(){const live=()=>TABS.list.filter(t=>t.frame).length;if(live()<=TAB_LIVE)return;
for(const t of TABS.list.filter(t=>t.frame&&t!==TABS.active&&t.loaded&&!tabBusy(t)).sort((a,b)=>a.used-b.used)){if(live()<=TAB_LIVE)break;dropFrame(t)}}
// Run by the shell as it starts (it needs the view switch, set up after this). The frame the server drew is the tab
// showing: it takes the place of the saved tab on the same page, or, new, the place after the one that showed. A blank
// one — the app opening on Library — reads the saved tab that showed instead, in its view.
function restoreTabs(){let saved=null;try{saved=JSON.parse(recall(TABS_KEY)||'null')}catch(e){}
if(!saved||saved.v!==1||!Array.isArray(saved.tabs))return false;const t0=TABS.list[0];
const list=saved.tabs.filter(x=>x&&typeof x.href==='string').map(x=>makeTab({href:x.href,src:String(x.src||''),folder:String(x.folder||''),kind:VAULTS[x.kind]?x.kind:'library',title:String(x.title||'')}));if(!list.length)return false;
let at=Math.min(Math.max(saved.active|0,0),list.length-1),restored=false;
if(t0.src){const m=list[at].src===t0.src?at:list.findIndex(t=>t.src===t0.src);if(m>=0){list[m]=t0;at=m}else list.splice(++at,0,t0)}
else if(KIND==='library'){const x=list[at];Object.assign(t0,{href:x.href,src:x.src,folder:x.folder,title:x.title});list[at]=t0;if(x.kind!==KIND)switchVault(x.kind,true);if(x.href){navigate(x.href);restored=true}}
else list.splice(++at,0,t0);
TABS.list=list;drawTabs();return restored}
// A move or rename in Artifacts (vault_ui's remap): a tab behind on a page that moved keeps its place in the bar and reads
// the page from its new path when next shown. Its frame goes (unless in use), so no page lives on at a path that is gone.
function remapTabs(move){for(const t of TABS.list){if(t===TABS.active)continue;const to=move(t.src);if(!to)continue;const hash=t.href.includes('#')?t.href.slice(t.href.indexOf('#')):'';
t.href=viewHref(to,'html')+hash;t.src=to;if(t.frame&&!tabBusy(t))dropFrame(t)}saveTabs()}
drawTabs();"""


def tabs_style() -> str:
    return TABS_CSS


def tabs_markup(pin_icon: str) -> str:
    return TABS_HTML.replace("__PLUS__", PLUS_ICON).replace("__DOWN__", DOWN_ICON).replace("__PIN__", pin_icon)


def tabs_script() -> str:
    return TABS_JS.replace("__X__", json.dumps(ICONS["close"]))
