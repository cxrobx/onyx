/* Onyx — highlight-to-ask reading companion.
 *
 * Drop this on any local HTML page:
 *   <script src="http://localhost:8899/ask.js"></script>
 *
 * Highlight text, then use the nearby Ask button or right-click for ELI5 /
 * Prove it / Ask a question. Right-click the page without a selection to ask
 * about the page. Each answer
 * streams from a subscription-backed provider CLI on the local server with full context of a
 * folder you point it at. Single self-contained file (no CDN/deps): it injects
 * its own CSS, renders Markdown with a built-in renderer, and builds all DOM
 * under <body>.
 */
(function () {
  'use strict';
  if (window.__askWidget) return;
  window.__askWidget = true;

  // Origin of the server that served this script (standard currentScript trick).
  var SERVER = (function () {
    try { return new URL(document.currentScript.src).origin; }
    catch (e) { return window.location.origin; }
  })();
  // Replaced server-side at /ask.js serve time with the per-server random token.
  var TOKEN = '__ASK_TOKEN__';

  var MAX_SEL = 4000;
  var MAX_CTX = 600;
  var MAX_PAGE_CTX = 4000;
  var MAX_HIGHLIGHT_INDEX = 250000;
  var PANEL_W = 380;

  // ---- state ----
  var folder = null;
  var defaultFolder = null;
  var recentFolders = [];
  var serverConfig = { version: 'unknown', provider: 'claude', model: 'sonnet', reasoning_effort: 'medium', cache_ttl_hours: 168, cache_max_entries: 100 };
  var appearanceTheme = 'system';
  var vaultLook = null;  // Match vault appearance: {mode, reader_css} while the app wears the vault
  var sel = null;              // { text, context, rect }
  var rightClickSelection = null; // whether the press began on an existing selection
  var abort = null;            // AbortController for the active stream
  var activeAction = null;     // 'eli5' | 'prove' | 'ask'
  var userPinned = false;      // user dragged/resized the panel → stop auto-positioning
  var isFileProto = window.location.protocol === 'file:';
  var lastAnswer = '', lastAction = null, lastQuestion = '';   // for provider handoff
  var transcript = [];         // completed turns: { role:'user'|'assistant', text }
  var liveEl = null;           // the .askw-a element receiving the current stream
  var streaming = false;       // a /ask stream is in flight (defer live-reload while true)
  var currentRequestId = null, lastRequestBody = null, lastCacheKey = null;
  var currentRequestMode = 'generated', historyOrigin = null;
  var requestCitations = [], requestTrace = [];
  // ---- live reload (only on /view pages, which seed askw-src) ----
  var reloadSrc = null, reloadSig = null, reloadSeen = null, reloadPending = false;
  var reloadRestoring = false;   // this load is one live reload (or the editor) asked for, and it puts back its own place
  var landingReload = false;     // leaving for a reload that lands the page itself (back from the editor)
  // ---- editing (⌘E on a Markdown page; the editor is static/onyx-editor.js, loaded on first use) ----
  var editSession = null, editOpening = false, editorScript = null;

  // ---- DOM refs ----
  var triggerEl, menuEl, askWrap, askInput;
  var panelEl, panelTitle, panelSel, panelTools, panelBody, claudeBtn, stopBtn, retryBtn, historyBtn;
  var followWrap, followInput, followGo;
  var pillEl, pillLabel, pickerEl, toastEl;
  var chatsEl, chatsCount, chatsListEl, chatsRows, highlightsSection, highlightsRows, highlightsToggle;
  var pageChats = [], savedHighlights = [], highlightsOn = false, markedPassages = [], highlightOverlay = null;

  // ============================================================ styles
  var CSS = [
    '.askw-root{all:revert;box-sizing:border-box;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","SF Pro Text","Helvetica Neue",sans-serif;color:#0d0d0d;line-height:1.5;-webkit-font-smoothing:antialiased;--askw-accent:#3a83f7;--askw-accent-hover:#2c67c5;--askw-line:rgba(0,0,0,.10);--askw-soft:rgba(0,0,0,.055);}',
    '.askw-root *{box-sizing:border-box;}',
    // A page's own text styles reach in here: one that inks its strong, code or td
    // (the HTML Artifact Kit's do) would ink them in the panel too, dark on a dark
    // panel. :where() keeps this at one class's weight, so the panel's own rules win.
    '.askw-root :where(p,h1,h2,h3,h4,h5,h6,strong,b,em,i,code,li,th,td){color:inherit;}',
    '.askw-menu{position:fixed;z-index:2147483600;display:none;min-width:190px;background:rgba(255,255,255,.82);border:1px solid var(--askw-line);border-radius:11px;box-shadow:0 20px 55px rgba(0,0,0,.18),inset 0 1px 0 rgba(255,255,255,.65);backdrop-filter:blur(24px) saturate(1.35);-webkit-backdrop-filter:blur(24px) saturate(1.35);padding:6px;font-size:13px;cursor:move;}',
    '.askw-trigger{position:fixed;z-index:2147483598;display:none;align-items:center;gap:5px;padding:5px 10px;border:1px solid rgba(255,255,255,.28);border-radius:999px;background:var(--askw-accent);color:#fff;box-shadow:0 10px 28px rgba(0,0,0,.20);font:600 12px/1.4 -apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;cursor:pointer;}',
    '.askw-trigger:hover{background:var(--askw-accent-hover);}',
    '.askw-item{display:flex;width:100%;align-items:center;gap:8px;padding:8px 10px;border:0;border-radius:7px;background:transparent;font:inherit;text-align:left;cursor:pointer;color:#0d0d0d;user-select:none;}',
    '.askw-item:hover{background:rgba(13,13,13,.07);}',
    '.askw-trigger:focus-visible,.askw-item:focus-visible,.askw-pill:focus-visible,.askw-x:focus-visible,.askw-follow-go:focus-visible,.askw-foot button:focus-visible,.askw-ask-go:focus-visible,.askw-picker button:focus-visible{outline:2px solid var(--askw-accent);outline-offset:2px;}',
    '.askw-item .askw-ico{width:16px;text-align:center;opacity:.75;}',
    '.askw-ask-wrap{display:none;padding:6px 6px 4px;border-top:1px solid var(--askw-soft);margin-top:4px;}',
    '.askw-ask-wrap.open{display:block;}',
    // Its own cursor, not the menu's move: cursor is inherited.
    '.askw-ask-input{cursor:auto;width:100%;min-height:54px;resize:vertical;border:1px solid var(--askw-line);border-radius:7px;background:#fff;padding:7px 9px;font:inherit;font-size:13px;color:#0d0d0d;outline:none;}',
    '.askw-ask-input:focus{border-color:var(--askw-accent);box-shadow:0 0 0 2px rgba(58,131,247,.18);}',
    '.askw-ask-go{margin-top:6px;float:right;background:var(--askw-accent);color:#fff;border:none;border-radius:7px;padding:6px 14px;font:inherit;font-size:12px;font-weight:600;cursor:pointer;}',
    '.askw-ask-go:disabled{opacity:.45;cursor:not-allowed;}',
    '.askw-panel{position:fixed;z-index:2147483601;display:none;width:' + PANEL_W + 'px;height:auto;min-width:300px;min-height:180px;max-width:96vw;max-height:92vh;background:rgba(252,252,252,.88);border:1px solid var(--askw-line);border-radius:15px;box-shadow:0 26px 70px rgba(0,0,0,.22),inset 0 1px 0 rgba(255,255,255,.72);backdrop-filter:blur(28px) saturate(1.28);-webkit-backdrop-filter:blur(28px) saturate(1.28);overflow:hidden;flex-direction:column;resize:both;}',
    '.askw-panel.open{display:flex;}',
    '.askw-head{padding:13px 40px 11px 15px;border-bottom:1px solid var(--askw-soft);background:rgba(255,255,255,.28);position:relative;flex:0 0 auto;cursor:move;user-select:none;}',
    '.askw-eyebrow{font-size:10px;letter-spacing:.08em;text-transform:uppercase;font-weight:700;color:var(--askw-accent);margin:0 0 3px;}',
    '.askw-selq{font-size:12.5px;line-height:1.45;color:#5d5d5d;margin:0;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:3;overflow:hidden;}',
    '.askw-selq.clamped{cursor:pointer;}.askw-selq.expanded{display:block;max-height:30vh;overflow-y:auto;}',
    '.askw-x{position:absolute;top:9px;right:9px;width:26px;height:26px;border:none;background:transparent;color:#a8a29e;font-size:17px;line-height:1;border-radius:6px;cursor:pointer;}',
    '.askw-x:hover{background:rgba(13,13,13,.07);color:#0d0d0d;}',
    '.askw-tools{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 0;}.askw-tools:empty{display:none;}',
    '.askw-pillt{display:inline-flex;align-items:center;gap:5px;background:rgba(58,131,247,.11);color:#2c67c5;font-size:10.5px;font-weight:600;padding:3px 9px;border-radius:999px;}',
    '.askw-dot{width:6px;height:6px;border-radius:50%;background:var(--askw-accent);animation:askw-pulse 1.1s infinite;}',
    '@keyframes askw-pulse{0%,100%{opacity:.35}50%{opacity:1}}',
    '.askw-body{padding:12px 15px 15px;overflow-y:auto;overscroll-behavior:contain;font-size:14px;line-height:1.55;color:#0d0d0d;flex:1 1 auto;min-height:0;}',
    '.askw-q{margin:15px 0 9px;padding:7px 11px;background:rgba(255,255,255,.56);border:1px solid var(--askw-soft);border-radius:9px;font-size:13px;color:#5d5d5d;white-space:pre-wrap;}',
    '.askw-q:first-child{margin-top:1px;}',
    '.askw-a{font-size:14px;}',
    '.askw-body p{margin:0 0 9px;}.askw-body p:last-child{margin-bottom:0;}',
    '.askw-body ul,.askw-body ol{margin:0 0 9px;padding-left:20px;}.askw-body li{margin:2px 0;}',
    '.askw-body code{background:#f5f5f4;border-radius:4px;padding:1px 5px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;}',
    '.askw-body pre{background:#1c1917;color:#fafaf9;border-radius:8px;padding:11px 13px;overflow-x:auto;font-size:12.5px;}',
    '.askw-body pre code{background:transparent;color:inherit;padding:0;}',
    '.askw-body h1,.askw-body h2,.askw-body h3{font-size:14.5px;margin:12px 0 6px;font-weight:700;}',
    '.askw-body a{color:var(--askw-accent);}',
    '.askw-body .askw-table{overflow-x:auto;margin:0 0 9px;}.askw-body .askw-table:last-child{margin-bottom:0;}',
    '.askw-body table{border-collapse:collapse;font-size:13px;line-height:1.45;}',
    '.askw-body th,.askw-body td{border:1px solid rgba(127,127,127,.32);padding:4px 8px;text-align:left;vertical-align:top;}',
    '.askw-body th{font-weight:700;background:rgba(127,127,127,.08);}',
    '.askw-fallback{white-space:pre-wrap;}',
    '.askw-think{display:flex;align-items:center;gap:8px;color:#78716c;font-size:13px;}',
    '.askw-err{color:#b91c1c;font-size:13px;}',
    '.askw-request-meta{color:#a8a29e;font-size:10px;margin:6px 0 0;}',
    '.askw-origin{margin:8px 0;padding:6px 9px;border-radius:7px;background:rgba(58,131,247,.09);color:#2c67c5;font-size:10.5px;font-weight:600;}',
    '.askw-history-entry{margin:0 0 13px;padding:11px;border:1px solid var(--askw-line);border-radius:10px;background:rgba(255,255,255,.28);}',
    '.askw-history-entry .askw-q{margin-top:0}.askw-history-meta{margin:8px 0 0;color:#a8a29e;font-size:10px}.askw-history-actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:9px}.askw-history-actions button{border:1px solid var(--askw-line);border-radius:7px;background:rgba(255,255,255,.76);color:#5d5d5d;padding:5px 8px;font:inherit;font-size:10.5px;cursor:pointer}.askw-history-actions button:first-child{background:var(--askw-accent);border-color:var(--askw-accent);color:#fff}',
    '.askw-citations{margin:14px 0 2px;padding-top:10px;border-top:1px solid var(--askw-soft);}',
    '.askw-citations-title{margin:0 0 7px;font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#78716c;}',
    '.askw-citation{display:block;width:100%;margin:5px 0;padding:7px 9px;border:1px solid var(--askw-line);border-radius:7px;background:rgba(255,255,255,.62);color:#2c67c5;text-align:left;font:inherit;font-size:11.5px;cursor:pointer;}',
    '.askw-citation:hover{background:#fff}.askw-citation pre{display:none;margin:7px 0 0;white-space:pre-wrap;color:#5d5d5d;background:#f7f7f7;padding:7px;font-size:10px}.askw-citation.expanded pre{display:block}',
    // The passage a piece of evidence lands on, lit for a moment. It is the page's own element, not one of Onyx's.
    '@keyframes askw-evidence{0%,45%{background-color:rgba(250,204,21,.34)}100%{background-color:rgba(250,204,21,0)}}',
    '.askw-evidence-hit{animation:askw-evidence 1.8s ease-out;}',
    '.askw-foot{display:flex;justify-content:flex-end;gap:8px;padding:8px 13px;border-top:1px solid var(--askw-soft);background:rgba(255,255,255,.22);flex:0 0 auto;}',
    '.askw-foot button{background:rgba(255,255,255,.78);border:1px solid var(--askw-line);border-radius:7px;padding:5px 11px;font:inherit;font-size:12px;color:#5d5d5d;cursor:pointer;}',
    '.askw-foot button:hover{background:#fff;color:#0d0d0d;}',
    '.askw-foot .askw-claude{background:var(--askw-accent);border-color:var(--askw-accent);color:#fff;margin-right:auto;}',
    '.askw-foot .askw-claude:hover{background:var(--askw-accent-hover);}',
    '.askw-foot .askw-claude:disabled{opacity:.55;cursor:default;}',
    '.askw-foot .askw-stop{display:none;color:#b91c1c}.askw-foot .askw-retry{display:none;color:#2c67c5}',
    '.askw-followup{display:none;align-items:flex-end;gap:7px;padding:9px 13px;border-top:1px solid var(--askw-soft);background:rgba(255,255,255,.2);flex:0 0 auto;}',
    '.askw-follow-field{flex:1 1 auto;min-width:0;position:relative;}',
    '.askw-follow-input{display:block;width:100%;resize:none;min-height:34px;border:1px solid var(--askw-line);border-radius:9px;background:#fff;padding:7px 10px;font:inherit;font-size:13px;color:#0d0d0d;outline:none;line-height:1.4;}',
    '.askw-follow-input:focus{border-color:var(--askw-accent);box-shadow:0 0 0 2px rgba(58,131,247,.18);}',
    '.askw-follow-input:disabled{opacity:.55;background:#fafaf9;}',
    // The follow-up box's own drag grip, drawn as the native one (see dragFollow).
    '.askw-follow-grip{position:absolute;right:2px;bottom:2px;width:14px;height:14px;cursor:ns-resize;opacity:.38;background:linear-gradient(135deg,transparent 0 55%,currentColor 55% 62%,transparent 62% 75%,currentColor 75% 82%,transparent 82%);}',
    '.askw-follow-go{flex:0 0 auto;width:34px;height:34px;background:var(--askw-accent);color:#fff;border:none;border-radius:9px;font-size:15px;line-height:1;cursor:pointer;}',
    '.askw-follow-go:hover{background:var(--askw-accent-hover);}',
    '.askw-follow-go:disabled{opacity:.4;cursor:not-allowed;}',
    '.askw-toast{position:fixed;bottom:24px;left:50%;z-index:2147483603;background:rgba(28,28,28,.88);backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);color:#fff;padding:9px 16px;border:1px solid rgba(255,255,255,.1);border-radius:10px;font-size:13px;box-shadow:0 12px 34px rgba(0,0,0,.28);opacity:0;pointer-events:none;transform:translateX(-50%) translateY(8px);transition:opacity .15s,transform .15s;}',
    '.askw-toast.show{opacity:1;transform:translateX(-50%) translateY(0);}',
    // The context folder rests as a folder icon and slides its name out on hover,
    // keyboard focus, or while the picker is open. It is glass lying on the page,
    // so it is tinted for the page under it (data-askw-page), not the app theme:
    // thin at rest, frosted to a readable floor once its name shows. Its icon is
    // --askw-glass-accent, Onyx's blue, which reads on either glass; the vault look
    // recolours it, and the name, only over a page of the vault's own tone.
    '.askw-pill{--askw-glass-accent:#3a83f7;--askw-glass:rgba(255,255,255,.2);--askw-frost:rgba(255,255,255,.74);position:fixed;top:12px;right:var(--askw-pill-right,12px);z-index:2147483599;display:flex;align-items:center;height:30px;max-width:240px;background:var(--askw-glass);border:1px solid rgba(255,255,255,.55);border-radius:999px;box-shadow:0 6px 18px rgba(0,0,0,.1),inset 0 1px 0 rgba(255,255,255,.6);backdrop-filter:blur(14px) saturate(1.8);-webkit-backdrop-filter:blur(14px) saturate(1.8);padding:0 7px;font-size:11.5px;color:#5d5d5d;cursor:pointer;transition:padding .2s ease,background-color .2s ease;}',
    '.askw-pill .askw-ico{display:block;flex:none;width:14px;height:14px;color:var(--askw-glass-accent);}',
    '.askw-pill b{color:#0d0d0d;font-weight:600;max-width:0;margin-left:0;opacity:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;transition:max-width .2s ease,margin-left .2s ease,opacity .15s;}',
    '.askw-pill:hover,.askw-pill:focus-visible,.askw-pill[aria-expanded="true"]{padding-right:11px;background:var(--askw-frost);}',
    '.askw-pill:hover b,.askw-pill:focus-visible b,.askw-pill[aria-expanded="true"] b{max-width:180px;margin-left:6px;opacity:1;}',
    'html[data-askw-page="dark"] .askw-pill{--askw-glass:rgba(22,22,22,.24);--askw-frost:rgba(30,30,30,.74);border-color:rgba(255,255,255,.14);box-shadow:0 6px 18px rgba(0,0,0,.3),inset 0 1px 0 rgba(255,255,255,.1);color:#cdcdcd;}',
    'html[data-askw-page="dark"] .askw-pill b{color:#fff;}',
    // Page Only (⌘B in Onyx, initPageOnly): the page alone. Its buttons go; the
    // right-click menu and ⌘⇧A stay, since they show only when called.
    'html[data-askw-page-only] .askw-pill,html[data-askw-page-only] .askw-chats,html[data-askw-page-only] .askw-trigger{display:none!important;}',
    // The page's chats rest in the corner opposite the pill: a bubble and how many
    // there are, which opens a list of them. Its glass takes the page's tone like
    // the pill's, but frosted from the start, since it always carries a number.
    // The list is a surface like the picker, in the app theme. The icon and count
    // carry the glass's own accent, as the pill's icon does: the dark app theme
    // recolours .askw-root, which would leave white on the light glass of a light
    // page, and the vault look its accent, which is only known to read on the vault.
    '.askw-chats{--askw-glass-accent:#3a83f7;--askw-glass:rgba(255,255,255,.74);--askw-frost:rgba(255,255,255,.92);position:fixed;right:12px;bottom:12px;z-index:2147483599;display:flex;align-items:center;gap:5px;height:30px;padding:0 10px 0 8px;background:var(--askw-glass);border:1px solid rgba(255,255,255,.55);border-radius:999px;box-shadow:0 6px 18px rgba(0,0,0,.1),inset 0 1px 0 rgba(255,255,255,.6);backdrop-filter:blur(14px) saturate(1.8);-webkit-backdrop-filter:blur(14px) saturate(1.8);font-size:11.5px;font-weight:600;font-variant-numeric:tabular-nums;cursor:pointer;transition:background-color .15s ease;}',
    '.askw-chats[hidden]{display:none;}',
    '.askw-chats .askw-ico{display:block;flex:none;width:15px;height:15px;}',
    '.askw-chats .askw-ico,.askw-chats-n{color:var(--askw-glass-accent);}',
    '.askw-chats:hover,.askw-chats:focus-visible,.askw-chats[aria-expanded="true"]{background:var(--askw-frost);}',
    'html[data-askw-page="dark"] .askw-chats{--askw-glass:rgba(30,30,30,.74);--askw-frost:rgba(38,38,38,.92);border-color:rgba(255,255,255,.14);box-shadow:0 6px 18px rgba(0,0,0,.3),inset 0 1px 0 rgba(255,255,255,.1);}',
    '.askw-chats-list{position:fixed;right:12px;bottom:50px;z-index:2147483602;display:none;flex-direction:column;width:320px;max-width:calc(100vw - 24px);max-height:min(440px,calc(100vh - 70px));background:rgba(255,255,255,.86);border:1px solid var(--askw-line);border-radius:12px;box-shadow:0 20px 55px rgba(0,0,0,.19),inset 0 1px 0 rgba(255,255,255,.7);backdrop-filter:blur(24px) saturate(1.32);-webkit-backdrop-filter:blur(24px) saturate(1.32);padding:6px;font-size:12.5px;}',
    '.askw-chats-list.open{display:flex;}',
    '.askw-chats-title{flex:none;margin:4px 8px 6px;font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#78716c;}',
    '.askw-chats-rows{overflow-y:auto;overscroll-behavior:contain;min-height:0;}',
    '.askw-chats-row{display:block;width:100%;padding:7px 9px;border:0;border-radius:7px;background:transparent;font:inherit;text-align:left;cursor:pointer;color:#0d0d0d;}',
    '.askw-chats-row:hover{background:rgba(13,13,13,.07);}',
    '.askw-chats:focus-visible{outline:2px solid var(--askw-accent);outline-offset:2px;}.askw-chats-row:focus-visible{outline:2px solid var(--askw-accent);outline-offset:-2px;}',
    '.askw-chats-q{display:block;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}',
    '.askw-chats-sel{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden;margin-top:2px;font-size:11.5px;line-height:1.4;color:#5d5d5d;}',
    '.askw-chats-meta{display:block;margin-top:3px;font-size:10px;color:#a8a29e;}',
    '.askw-highlights-switch{display:flex;align-items:center;gap:8px;margin:0 6px 5px;padding:6px 4px;color:#5d5d5d;font-size:11.5px;cursor:pointer;}',
    '.askw-highlights-switch[hidden],.askw-highlights-section[hidden]{display:none;}',
    '.askw-highlights-switch input{width:15px;height:15px;margin:0;accent-color:var(--askw-accent);}',
    '.askw-highlights-section{border-top:1px solid var(--askw-line);padding-top:5px;overflow-y:auto;min-height:0;}',
    '.askw-highlights-heading{margin:3px 10px 5px;color:#78716c;font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;}',
    '.askw-highlight-card{position:relative;padding:5px 28px 7px 5px;border-radius:7px;}',
    '.askw-highlight-card:hover{background:rgba(13,13,13,.05);}',
    '.askw-highlight-jump{display:block;width:100%;padding:3px 4px;border:0;background:transparent;color:inherit;text-align:left;font:inherit;line-height:1.4;cursor:pointer;}',
    '.askw-highlight-note{display:block;margin:2px 4px 0;padding:2px 0;border:0;background:transparent;color:var(--askw-accent);font:inherit;font-size:11px;text-align:left;cursor:pointer;}',
    '.askw-highlight-remove{position:absolute;top:8px;right:5px;width:20px;height:20px;border:0;border-radius:5px;background:transparent;color:#a8a29e;cursor:pointer;}',
    '.askw-highlight-remove:hover{background:rgba(13,13,13,.08);color:#b91c1c;}',
    '.askw-highlight-editor{padding:4px;}.askw-highlight-editor textarea{display:block;width:100%;min-height:54px;resize:vertical;padding:6px;border:1px solid var(--askw-line);border-radius:6px;background:#fff;color:#0d0d0d;font:inherit;}',
    '.askw-highlight-editor button{margin-top:4px;padding:4px 8px;border:0;border-radius:5px;background:var(--askw-accent);color:#fff;font:inherit;cursor:pointer;}',
    'html[data-askw-color="dark"] .askw-highlights-switch{color:#cdcdcd;}html[data-askw-color="dark"] .askw-highlight-card:hover{background:rgba(255,255,255,.08);}',
    'html[data-askw-color="dark"] .askw-highlight-editor textarea{background:#292929;color:#fff;}',
    '.askw-highlight-overlay{position:fixed;inset:0;z-index:2147483590;pointer-events:none;overflow:hidden;}.askw-highlight-overlay i{position:absolute;background:rgba(58,131,247,.12);border-bottom:1px solid rgba(58,131,247,.72);}',
    '::highlight(askw-passages){background-color:rgba(58,131,247,.14);text-decoration:underline;text-decoration-color:rgba(58,131,247,.72);}',
    'html[data-askw-color="dark"] .askw-chats-list{background:rgba(35,35,35,.84);box-shadow:0 26px 70px rgba(0,0,0,.42),inset 0 1px 0 rgba(255,255,255,.10);}',
    'html[data-askw-color="dark"] .askw-chats-row{color:#fff;}html[data-askw-color="dark"] .askw-chats-row:hover{background:rgba(255,255,255,.10);}html[data-askw-color="dark"] .askw-chats-sel{color:#cdcdcd;}',
    '@supports not ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){.askw-chats-list{background:#fff}.askw-chats{--askw-glass:#fff;--askw-frost:#fff}html[data-askw-color="dark"] .askw-chats-list{background:#242424}html[data-askw-page="dark"] .askw-chats{--askw-glass:#242424;--askw-frost:#242424}}',
    '@media(prefers-reduced-transparency:reduce){.askw-chats,.askw-chats-list{backdrop-filter:none;-webkit-backdrop-filter:none}.askw-chats-list{background:rgba(255,255,255,.98)}.askw-chats{--askw-glass:rgba(255,255,255,.98);--askw-frost:rgba(255,255,255,.98)}html[data-askw-color="dark"] .askw-chats-list{background:rgba(36,36,36,.98)}html[data-askw-page="dark"] .askw-chats{--askw-glass:rgba(36,36,36,.98);--askw-frost:rgba(36,36,36,.98)}}',
    '@media(prefers-reduced-motion:reduce){.askw-chats{transition:none}}',
    '.askw-picker{position:fixed;top:42px;right:12px;z-index:2147483602;display:none;width:300px;background:rgba(255,255,255,.86);border:1px solid var(--askw-line);border-radius:12px;box-shadow:0 20px 55px rgba(0,0,0,.19),inset 0 1px 0 rgba(255,255,255,.7);backdrop-filter:blur(24px) saturate(1.32);-webkit-backdrop-filter:blur(24px) saturate(1.32);padding:10px;font-size:12.5px;}',
    '.askw-picker.open{display:block;}',
    '.askw-picker label{display:block;font-weight:600;color:#44403c;margin:0 0 5px;font-size:11px;text-transform:uppercase;letter-spacing:.05em;}',
    '.askw-picker input{width:100%;border:1px solid var(--askw-line);border-radius:7px;background:#fff;color:#0d0d0d;padding:7px 9px;font:inherit;font-size:12.5px;outline:none;}',
    '.askw-picker input:focus{border-color:var(--askw-accent);box-shadow:0 0 0 2px rgba(58,131,247,.18);}',
    '.askw-picker-row{display:flex;gap:6px}.askw-picker-browse{display:none;white-space:nowrap;border:1px solid #d6d3d1;border-radius:7px;background:#fff;padding:0 9px;font:inherit;font-size:11px}.askw-native .askw-picker-browse{display:block}',
    '.askw-recent{margin-top:8px;max-height:160px;overflow-y:auto;}',
    '.askw-recent-item{display:block;width:100%;padding:6px 8px;border:0;border-radius:6px;background:transparent;font:inherit;text-align:left;cursor:pointer;color:#44403c;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}',
    '.askw-recent-item:hover{background:rgba(13,13,13,.07);}',
    '.askw-hint{margin-top:8px;color:#a16207;font-size:11px;line-height:1.4;}',
    '.askw-picker-save{margin-top:8px;width:100%;background:var(--askw-accent);color:#fff;border:none;border-radius:7px;padding:7px;font:inherit;font-size:12px;font-weight:600;cursor:pointer;}',
    'html[data-askw-color="dark"] .askw-root{color:#fff;--askw-line:rgba(255,255,255,.15);--askw-soft:rgba(255,255,255,.07);}',
    'html[data-askw-color="dark"] .askw-menu,html[data-askw-color="dark"] .askw-panel,html[data-askw-color="dark"] .askw-picker{background:rgba(35,35,35,.84);box-shadow:0 26px 70px rgba(0,0,0,.42),inset 0 1px 0 rgba(255,255,255,.10);}',
    'html[data-askw-color="dark"] .askw-item,html[data-askw-color="dark"] .askw-body{color:#fff;}',
    'html[data-askw-color="dark"] .askw-selq,html[data-askw-color="dark"] .askw-q,html[data-askw-color="dark"] .askw-foot button,html[data-askw-color="dark"] .askw-picker label,html[data-askw-color="dark"] .askw-recent-item{color:#cdcdcd;}',
    'html[data-askw-color="dark"] .askw-head,html[data-askw-color="dark"] .askw-foot,html[data-askw-color="dark"] .askw-followup{background:rgba(15,15,15,.22);}',
    'html[data-askw-color="dark"] .askw-item:hover,html[data-askw-color="dark"] .askw-x:hover,html[data-askw-color="dark"] .askw-recent-item:hover{background:rgba(255,255,255,.10);color:#fff;}',
    'html[data-askw-color="dark"] .askw-ask-input,html[data-askw-color="dark"] .askw-follow-input,html[data-askw-color="dark"] .askw-picker input,html[data-askw-color="dark"] .askw-picker-browse{background:rgba(45,45,45,.92);border-color:var(--askw-line);color:#fff;}',
    'html[data-askw-color="dark"] .askw-q,html[data-askw-color="dark"] .askw-citation,html[data-askw-color="dark"] .askw-foot button{background:rgba(45,45,45,.72);}',
    'html[data-askw-color="dark"] .askw-history-entry{background:rgba(45,45,45,.42)}html[data-askw-color="dark"] .askw-history-actions button{background:rgba(45,45,45,.78);color:#cdcdcd}',
    'html[data-askw-color="dark"] .askw-foot button:hover,html[data-askw-color="dark"] .askw-citation:hover{background:rgba(58,58,58,.94);color:#fff;}',
    'html[data-askw-color="dark"] .askw-body code{background:rgba(255,255,255,.09);}',
    'html[data-askw-color="dark"] .askw-citation pre{background:#181818;color:#cdcdcd;}',
    'html[data-askw-color="dark"] .askw-follow-input:disabled{background:#242424;}',
    // The panel's own warning colours, which no palette recolours: a dark ground takes
    // lighter ones, and Stop keeps its red over the footer-button ink the themes set.
    'html[data-askw-color] .askw-foot .askw-stop{color:#b91c1c;}html[data-askw-color="dark"] .askw-err,html[data-askw-color="dark"] .askw-foot .askw-stop{color:#f87171;}html[data-askw-color="dark"] .askw-hint{color:#fbbf24;}',
    '@supports not ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){.askw-menu,.askw-panel,.askw-picker{background:#fff}.askw-pill{--askw-glass:#fff;--askw-frost:#fff}}',
    '@supports not ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){html[data-askw-color="dark"] .askw-menu,html[data-askw-color="dark"] .askw-panel,html[data-askw-color="dark"] .askw-picker{background:#242424}html[data-askw-page="dark"] .askw-pill{--askw-glass:#242424;--askw-frost:#242424}}',
    '@media(prefers-reduced-transparency:reduce){.askw-menu,.askw-panel,.askw-picker{background:rgba(255,255,255,.98)}.askw-menu,.askw-panel,.askw-picker,.askw-pill{backdrop-filter:none;-webkit-backdrop-filter:none}.askw-pill{--askw-glass:rgba(255,255,255,.98);--askw-frost:rgba(255,255,255,.98)}html[data-askw-color="dark"] .askw-menu,html[data-askw-color="dark"] .askw-panel,html[data-askw-color="dark"] .askw-picker{background:rgba(36,36,36,.98)}html[data-askw-page="dark"] .askw-pill{--askw-glass:rgba(36,36,36,.98);--askw-frost:rgba(36,36,36,.98)}}',
    // Pinned chrome lets the pointer through while a press elsewhere is held (see
    // watchPinnedChrome), its descendants too, whatever pointer-events they set.
    '[data-askw-passthrough],[data-askw-passthrough] *{pointer-events:none!important;}',
    '@media(prefers-reduced-motion:reduce){.askw-dot{animation:none}.askw-toast,.askw-pill,.askw-pill b{transition:none}}'
  ].join('\n');

  function injectStyle() {
    var s = document.createElement('style');
    s.id = 'askw-style';
    s.textContent = CSS;
    (document.head || document.documentElement).appendChild(s);
  }

  // ============================================================ markdown
  function esc(s) { return String(s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  }); }

  // Collision-free placeholder sentinels (Unicode Private Use Area), built at
  // runtime so this source file stays plain ASCII and diffable.
  var MD_C0 = String.fromCharCode(0xE000), MD_C1 = String.fromCharCode(0xE001);
  var MD_F0 = String.fromCharCode(0xE002), MD_F1 = String.fromCharCode(0xE003);
  var MD_RE_CODE = new RegExp(MD_C0 + '(\\d+)' + MD_C1, 'g');
  var MD_RE_FENCE = new RegExp('^' + MD_F0 + '(\\d+)' + MD_F1 + '$');

  // Inline spans on an ALREADY-escaped string: `code`, [link](url), **bold**,
  // *italic*. Code spans are pulled out first so their contents aren't reformatted.
  function inlineMd(s) {
    var codes = [];
    s = s.replace(/`([^`]+)`/g, function (_, c) { codes.push(c); return MD_C0 + (codes.length - 1) + MD_C1; });
    s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (m, t, u) {
      return /^(https?:\/\/|\/)/.test(u) ? '<a href="' + u + '" target="_blank" rel="noopener">' + t + '</a>' : m;
    });
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    s = s.replace(MD_RE_CODE, function (_, i) { return '<code>' + codes[+i] + '</code>'; });
    return s;
  }

  // GFM table pieces. A row's outer pipes are optional, and `\|` is a literal
  // pipe inside a cell.
  var MD_PIPE = String.fromCharCode(0xE004);
  function mdCells(row) {
    var s = row.replace(/\\\|/g, MD_PIPE).trim();
    if (s.charAt(0) === '|') s = s.slice(1);
    if (s.charAt(s.length - 1) === '|') s = s.slice(0, -1);
    return s.split('|').map(function (c) { return c.split(MD_PIPE).join('|').trim(); });
  }
  function mdIsTableSep(line) {
    return line.indexOf('|') >= 0 && mdCells(line).every(function (c) { return /^:?-+:?$/.test(c); });
  }

  // The first table in a block: a header row, then a delimiter row with as many
  // cells. Body rows run for as long as lines still carry a pipe, so a sentence
  // written straight after the table stays a paragraph.
  function mdTableAt(lines) {
    for (var i = 0; i + 1 < lines.length; i++) {
      if (lines[i].indexOf('|') < 0 || !mdIsTableSep(lines[i + 1])) continue;
      if (mdCells(lines[i]).length !== mdCells(lines[i + 1]).length) continue;
      var end = i + 2;
      while (end < lines.length && lines[end].indexOf('|') >= 0) end++;
      return { start: i, end: end };
    }
    return null;
  }

  function mdTable(lines) {
    var aligns = mdCells(lines[1]).map(function (c) {
      var l = c.charAt(0) === ':', r = c.charAt(c.length - 1) === ':';
      return l && r ? 'center' : r ? 'right' : l ? 'left' : '';
    });
    function row(line, tag) {
      var cells = mdCells(line);
      return '<tr>' + aligns.map(function (a, i) {
        return '<' + tag + (a ? ' style="text-align:' + a + '"' : '') + '>' +
          inlineMd(esc(cells[i] || '')) + '</' + tag + '>';
      }).join('') + '</tr>';
    }
    return '<div class="askw-table"><table><thead>' + row(lines[0], 'th') + '</thead><tbody>' +
      lines.slice(2).map(function (l) { return row(l, 'td'); }).join('') + '</tbody></table></div>';
  }

  // Self-contained Markdown -> safe HTML. Escapes everything first, then only adds
  // our own tags, so reflected file contents from "Prove it" can't inject script
  // (no CDN, no DOMPurify, works offline).
  function mdToHtml(src) {
    src = String(src).replace(/\r\n/g, '\n');
    var fences = [];
    src = src.replace(/```[^\n]*\n([\s\S]*?)```/g, function (_, code) {
      fences.push('<pre><code>' + esc(code.replace(/\n$/, '')) + '</code></pre>');
      return MD_F0 + (fences.length - 1) + MD_F1;
    });
    var out = [];
    function block(text) {
      text = text.replace(/^\n+|\n+$/g, '');
      if (!text) return;
      var fm = text.match(MD_RE_FENCE);
      if (fm) { out.push(fences[+fm[1]]); return; }
      var lines = text.split('\n');
      var table = mdTableAt(lines);
      if (table) {
        // A table needs no blank line around it: whatever shares its block
        // renders as blocks of its own, before and after.
        block(lines.slice(0, table.start).join('\n'));
        out.push(mdTable(lines.slice(table.start, table.end)));
        block(lines.slice(table.end).join('\n'));
        return;
      }
      if (lines.every(function (l) { return /^\s*[-*]\s+/.test(l); })) {
        out.push('<ul>' + lines.map(function (l) { return '<li>' + inlineMd(esc(l.replace(/^\s*[-*]\s+/, ''))) + '</li>'; }).join('') + '</ul>');
        return;
      }
      if (lines.every(function (l) { return /^\s*\d+\.\s+/.test(l); })) {
        out.push('<ol>' + lines.map(function (l) { return '<li>' + inlineMd(esc(l.replace(/^\s*\d+\.\s+/, ''))) + '</li>'; }).join('') + '</ol>');
        return;
      }
      var hm = lines.length === 1 && text.match(/^(#{1,6})\s+(.*)$/);
      if (hm) { var lv = Math.min(hm[1].length, 6); out.push('<h' + lv + '>' + inlineMd(esc(hm[2])) + '</h' + lv + '>'); return; }
      out.push('<p>' + lines.map(function (l) { return inlineMd(esc(l)); }).join('<br>') + '</p>');
    }
    src.split(/\n{2,}/).forEach(function (text) { block(text); });
    return out.join('\n');
  }

  function renderMarkdown(el, text) {
    el.classList.remove('askw-fallback');
    el.innerHTML = mdToHtml(text);
  }

  // ============================================================ helpers
  function isOurs(node) {
    if (!node) return false;
    var el = node.nodeType === 3 ? node.parentElement : node;
    return !!(el && el.closest && el.closest('.askw-root'));
  }

  // Can anything from `el` up to the answer panel still scroll the way this
  // wheel turns? Only the dominant axis counts, as it does for the browser.
  function panelCanScroll(el, dx, dy) {
    var vertical = Math.abs(dy) >= Math.abs(dx), d = vertical ? dy : dx;
    if (!d) return true;
    for (; el && el.nodeType === 1; el = el.parentElement) {
      var cs = getComputedStyle(el), ov = vertical ? cs.overflowY : cs.overflowX;
      if (ov === 'auto' || ov === 'scroll') {
        var pos = vertical ? el.scrollTop : el.scrollLeft;
        var max = vertical ? el.scrollHeight - el.clientHeight : el.scrollWidth - el.clientWidth;
        if (d < 0 ? pos >= 1 : pos <= max - 1) return true;
      }
      if (el === panelEl) break;
    }
    return false;
  }

  function basename(p) {
    if (!p) return '(none)';
    var parts = String(p).replace(/\/+$/, '').split('/');
    return parts[parts.length - 1] || p;
  }

  function surroundingContext(range) {
    var node = range.commonAncestorContainer;
    if (node && node.nodeType === 3) node = node.parentElement;
    var block = node && node.closest
      ? node.closest('p,li,td,th,blockquote,pre,section,article,figure,h1,h2,h3,h4,h5,h6,div')
      : null;
    var txt = block ? (block.innerText || block.textContent || '') : '';
    return txt.replace(/\s+/g, ' ').trim().slice(0, MAX_CTX);
  }

  function captureFromSelection() {
    var s = window.getSelection();
    if (!s || s.isCollapsed || s.rangeCount === 0) return null;
    var text = s.toString().trim();
    if (!text) return null;
    var range = s.getRangeAt(0);
    if (isOurs(range.commonAncestorContainer)) return null;
    var selectedNode = range.commonAncestorContainer;
    if (selectedNode && selectedNode.nodeType === 3) selectedNode = selectedNode.parentElement;
    var pageEl = selectedNode && selectedNode.closest ? selectedNode.closest('[data-askw-page]') : null;
    return {
      text: text.slice(0, MAX_SEL),
      context: surroundingContext(range),
      rect: range.getBoundingClientRect(),
      page: pageEl ? Number(pageEl.getAttribute('data-askw-page')) || null : null,
      range: range.cloneRange()
    };
  }

  function pageContext() {
    var root = document.querySelector('main') || document.body;
    var walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    var parts = [], length = 0, node;
    while ((node = walk.nextNode()) && length < MAX_PAGE_CTX) {
      var parent = node.parentElement;
      if (!parent || parent.closest('.askw-root,script,style,template,noscript,[hidden]')) continue;
      var part = (node.textContent || '').replace(/\s+/g, ' ').trim();
      if (!part) continue;
      parts.push(part);
      length += part.length + 1;
    }
    return parts.join(' ').slice(0, MAX_PAGE_CTX);
  }

  function normalizedPassage(text) { return String(text || '').replace(/\s+/g, ' ').trim(); }

  // Keep positions in text nodes rather than wrapping authored HTML. A quote is
  // marked only when its text and saved surroundings identify one place.
  function passageIndex() {
    var text = '', points = [], lastBlock = null;
    var blocks = 'p,li,td,th,blockquote,pre,h1,h2,h3,h4,h5,h6,div,section,article,figure';
    var walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT), node;
    function put(ch, n, offset) {
      if (ch === ' ' && (!text || text[text.length - 1] === ' ')) return;
      text += ch;
      points.push(n ? { node: n, offset: offset } : null);
    }
    while ((node = walk.nextNode()) && text.length < MAX_HIGHLIGHT_INDEX) {
      var parent = node.parentElement;
      if (!parent || parent.closest('.askw-root,script,style,template,noscript,[hidden]')) continue;
      var block = parent.closest(blocks) || document.body;
      if (lastBlock && block !== lastBlock) put(' ', null, 0);
      lastBlock = block;
      var value = node.nodeValue || '';
      for (var i = 0; i < value.length && text.length < MAX_HIGHLIGHT_INDEX; i++) {
        put(/\s/.test(value[i]) ? ' ' : value[i], node, i);
      }
    }
    return { text: text, points: points };
  }

  function passageMatches(index, item) {
    var quote = normalizedPassage(item.selection);
    if (!quote) return [];
    var matches = [], start = index.text.indexOf(quote);
    while (start >= 0 && matches.length < 100) {
      var end = start + quote.length, a = index.points[start], b = index.points[end - 1];
      if (a && b) {
        var range = document.createRange();
        try {
          range.setStart(a.node, a.offset); range.setEnd(b.node, b.offset + 1);
          var page = a.node.parentElement.closest('[data-askw-page]');
          if (normalizedPassage(range.toString()) === quote &&
              (!item.document_page || page && Number(page.getAttribute('data-askw-page')) === Number(item.document_page))) {
            matches.push({ start: start, end: end, range: range });
          }
        } catch (e) {}
      }
      start = index.text.indexOf(quote, start + 1);
    }
    return matches;
  }

  function passageRange(index, item) {
    var matches = passageMatches(index, item);
    if (!matches.length) return null;
    var prefix = item.prefix || '', suffix = item.suffix || '';
    if (prefix || suffix) matches = matches.filter(function (m) {
      return (!prefix || index.text.slice(m.start - prefix.length, m.start) === prefix) &&
        (!suffix || index.text.slice(m.end, m.end + suffix.length) === suffix);
    });
    if (matches.length === 1) return matches[0].range;
    var context = normalizedPassage(item.context);
    if (!context) return null;
    matches = matches.filter(function (m) {
      var block = m.range.startContainer.parentElement.closest('p,li,td,th,blockquote,pre,h1,h2,h3,h4,h5,h6,div,section,article,figure');
      return block && normalizedPassage(block.innerText || block.textContent).slice(0, 600) === context;
    });
    return matches.length === 1 ? matches[0].range : null;
  }

  function selectionAnchors(captured) {
    var index = passageIndex(), matches = passageMatches(index, {
      selection: captured.text, document_page: captured.page
    }), selected = captured.range;
    var match = matches.filter(function (m) {
      return selected && selected.comparePoint(m.range.startContainer, m.range.startOffset) === 0 &&
        selected.comparePoint(m.range.endContainer, m.range.endOffset) === 0;
    })[0];
    if (!match && captured.rect) match = matches.filter(function (m) {
      return Array.prototype.some.call(m.range.getClientRects(), function (r) {
        return Math.min(r.right, captured.rect.right) - Math.max(r.left, captured.rect.left) > 1 &&
          Math.min(r.bottom, captured.rect.bottom) - Math.max(r.top, captured.rect.top) > 1;
      });
    })[0];
    if (!match && matches.length === 1) match = matches[0];
    return match ? {
      prefix: index.text.slice(Math.max(0, match.start - 48), match.start),
      suffix: index.text.slice(match.end, match.end + 48)
    } : { prefix: '', suffix: '' };
  }

  // ============================================================ build DOM
  function build() {
    // --- automatic selection affordance ---
    triggerEl = document.createElement('button');
    triggerEl.type = 'button';
    triggerEl.className = 'askw-root askw-trigger';
    triggerEl.setAttribute('aria-label', 'Ask about the selected text');
    triggerEl.setAttribute('aria-hidden', 'true');
    triggerEl.title = 'Ask about this selection (Command or Control + Shift + A)';
    triggerEl.innerHTML = '<span aria-hidden="true">✦</span> Ask';
    document.body.appendChild(triggerEl);
    triggerEl.addEventListener('click', function () {
      var captured = captureFromSelection();
      if (captured) sel = captured;
      if (!sel) return;
      var rect = triggerEl.getBoundingClientRect();
      showMenu(rect.left, rect.bottom + 6);
    });

    // --- context menu ---
    menuEl = document.createElement('div');
    menuEl.className = 'askw-root askw-menu';
    menuEl.setAttribute('role', 'dialog');
    menuEl.setAttribute('aria-label', 'Ask about selected text');
    menuEl.setAttribute('aria-modal', 'false');
    menuEl.setAttribute('aria-hidden', 'true');
    menuEl.innerHTML =
      '<button type="button" class="askw-item" data-act="eli5"><span class="askw-ico" aria-hidden="true">○</span>ELI5</button>' +
      '<button type="button" class="askw-item" data-act="prove"><span class="askw-ico" aria-hidden="true">✓</span>Prove it</button>' +
      '<button type="button" class="askw-item" data-act="ask"><span class="askw-ico" aria-hidden="true">…</span>Ask a question…</button>' +
      '<button type="button" class="askw-item" data-act="save-highlight"><span class="askw-ico" aria-hidden="true">⌑</span>Save highlight</button>' +
      '<div class="askw-ask-wrap"><textarea class="askw-ask-input" aria-label="Question about the highlighted text" placeholder="Ask about the highlighted text…"></textarea>' +
      '<button type="button" class="askw-ask-go">Go</button><div style="clear:both"></div></div>';
    document.body.appendChild(menuEl);
    askWrap = menuEl.querySelector('.askw-ask-wrap');
    askInput = menuEl.querySelector('.askw-ask-input');
    var askGo = menuEl.querySelector('.askw-ask-go');

    menuEl.querySelectorAll('.askw-item').forEach(function (item) {
      item.addEventListener('click', function () {
        var act = item.getAttribute('data-act');
        if (act === 'ask') {
          askWrap.classList.add('open');
          var r = menuEl.getBoundingClientRect();
          placeMenu(r.left, r.top);   // the field it opens stays inside the window
          askInput.focus();
        } else if (act === 'save-highlight') {
          saveHighlight();
        } else {
          start(act);
        }
      });
    });
    // The menu moves with the pointer from a press anywhere on it but the question
    // field, which keeps its caret, selection and resize grip. The press leaves focus
    // where it was, so a half-typed question stays in the field. Under 4px it is
    // still a click; past that it is a move, and the click it ends in runs nothing.
    var menuMoved = false;
    menuEl.addEventListener('mousedown', function (e) {
      if (e.button !== 0 || e.target === askInput) return;
      e.preventDefault();
      var r = menuEl.getBoundingClientRect(), sx = e.clientX, sy = e.clientY;
      function mv(ev) {
        if (!menuMoved && Math.abs(ev.clientX - sx) + Math.abs(ev.clientY - sy) < 4) return;
        menuMoved = true;
        placeMenu(r.left + ev.clientX - sx, r.top + ev.clientY - sy);
      }
      function up() {
        document.removeEventListener('mousemove', mv); document.removeEventListener('mouseup', up);
        setTimeout(function () { menuMoved = false; });   // after the click this release makes
      }
      document.addEventListener('mousemove', mv);
      document.addEventListener('mouseup', up);
    });
    menuEl.addEventListener('click', function (e) {
      if (menuMoved) { e.preventDefault(); e.stopPropagation(); }
    }, true);
    menuEl.addEventListener('keydown', function (e) {
      var items = Array.prototype.slice.call(menuEl.querySelectorAll('.askw-item')).filter(function (item) {
        return item.style.display !== 'none';
      });
      var index = items.indexOf(document.activeElement);
      if (e.key === 'Escape') {
        e.preventDefault(); e.stopPropagation(); hideMenu();
        if (sel && sel.text) { showTrigger(sel); triggerEl.focus(); }
      } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'Home' || e.key === 'End') {
        if (!items.length) return;
        e.preventDefault();
        if (e.key === 'Home') index = 0;
        else if (e.key === 'End') index = items.length - 1;
        else if (e.key === 'ArrowDown') index = (index + 1 + items.length) % items.length;
        else index = (index - 1 + items.length) % items.length;
        items[index].focus();
      }
    });
    askGo.addEventListener('click', submitAsk);
    askInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitAsk(); }
    });

    // --- answer panel ---
    panelEl = document.createElement('div');
    panelEl.className = 'askw-root askw-panel';
    panelEl.setAttribute('role', 'dialog');
    panelEl.setAttribute('aria-label', 'Onyx answer');
    panelEl.setAttribute('aria-modal', 'false');
    panelEl.setAttribute('aria-busy', 'false');
    panelEl.innerHTML =
      '<div class="askw-head"><p class="askw-eyebrow"></p><p class="askw-selq"></p>' +
      '<button type="button" class="askw-x" title="Close" aria-label="Close answer">×</button></div>' +
      '<div class="askw-body" aria-live="polite" aria-relevant="additions text"></div>' +
      '<div class="askw-followup"><div class="askw-follow-field"><textarea class="askw-follow-input" aria-label="Follow-up question" rows="1" placeholder="Ask a follow-up…"></textarea><span class="askw-follow-grip" aria-hidden="true"></span></div>' +
      '<button type="button" class="askw-follow-go" title="Send (Enter)" aria-label="Send follow-up">↑</button></div>' +
      '<div class="askw-foot"><button class="askw-claude" title="Open a dedicated provider session in this folder — hold ⌥ Option to copy the prompt instead">Open session</button><button class="askw-history">History</button><button class="askw-stop">Stop</button><button class="askw-retry">Retry</button><button class="askw-copy">Copy</button></div>';
    document.body.appendChild(panelEl);
    panelTitle = panelEl.querySelector('.askw-eyebrow');
    panelSel = panelEl.querySelector('.askw-selq');
    // The tool pills live in the conversation, straight under the answer being
    // written (appendLive puts them there), not in a bar under the header.
    panelTools = document.createElement('div');
    panelTools.className = 'askw-tools';
    panelTools.setAttribute('aria-live', 'polite');
    panelBody = panelEl.querySelector('.askw-body');
    followWrap = panelEl.querySelector('.askw-followup');
    followInput = panelEl.querySelector('.askw-follow-input');
    followGo = panelEl.querySelector('.askw-follow-go');
    claudeBtn = panelEl.querySelector('.askw-claude');
    historyBtn = panelEl.querySelector('.askw-history');
    stopBtn = panelEl.querySelector('.askw-stop');
    retryBtn = panelEl.querySelector('.askw-retry');
    panelEl.querySelector('.askw-x').addEventListener('click', closePanel);
    panelSel.addEventListener('click', function () { if (!panelDragged) toggleQuote(); });
    panelSel.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleQuote(); }
    });
    panelEl.querySelector('.askw-copy').addEventListener('click', function () {
      var t = panelBody.innerText || '';
      if (navigator.clipboard) navigator.clipboard.writeText(t).catch(function () {});
    });
    claudeBtn.addEventListener('click', function (e) { openInProvider(e.altKey); });
    historyBtn.addEventListener('click', loadSelectionHistory);
    stopBtn.addEventListener('click', stopRequest);
    retryBtn.addEventListener('click', retryRequest);
    followGo.addEventListener('click', submitFollowup);
    followInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitFollowup(); }
    });
    followInput.addEventListener('input', autosizeFollow);
    panelEl.querySelector('.askw-follow-grip').addEventListener('mousedown', dragFollow);
    makeDragResize(panelEl, panelEl.querySelector('.askw-head'));
    // While the pointer is over the panel the page underneath never scrolls: a
    // wheel nothing in the panel can take (the answer at its top or bottom, or
    // the header and footer, which don't scroll) goes nowhere. Pinch-zoom
    // (ctrlKey) stays the page's.
    panelEl.addEventListener('wheel', function (e) {
      if (!e.ctrlKey && !panelCanScroll(e.target, e.deltaX, e.deltaY)) e.preventDefault();
    }, { passive: false });

    // --- folder pill + picker ---
    pillEl = document.createElement('button');
    pillEl.type = 'button';
    pillEl.className = 'askw-root askw-pill';
    pillEl.innerHTML = '<svg class="askw-ico" aria-hidden="true" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"><path d="M2 4.5A1.5 1.5 0 0 1 3.5 3h2.6l1.5 1.5h4.9A1.5 1.5 0 0 1 14 6v5.5a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 11.5z"/></svg><b class="askw-pill-label">…</b>';
    pillLabel = pillEl.querySelector('.askw-pill-label');
    pillEl.title = 'Context folder the selected provider reads — click to change';
    pillEl.setAttribute('aria-haspopup', 'dialog');
    pillEl.setAttribute('aria-expanded', 'false');
    document.body.appendChild(pillEl);
    pillEl.addEventListener('click', togglePicker);

    pickerEl = document.createElement('div');
    pickerEl.className = 'askw-root askw-picker';
    pickerEl.setAttribute('role', 'dialog');
    pickerEl.setAttribute('aria-label', 'Choose context folder');
    pickerEl.setAttribute('aria-hidden', 'true');
    pickerEl.innerHTML =
      '<label for="askw-folder-input">Context folder</label><div class="askw-picker-row"><input id="askw-folder-input" class="askw-picker-input" spellcheck="false" /><button type="button" class="askw-picker-browse">Choose…</button></div>' +
      '<div class="askw-hint">Only files inside this folder are available to the selected provider as supporting evidence.</div>' +
      '<button type="button" class="askw-picker-save">Use this folder</button>' +
      '<div class="askw-recent"></div>' +
      (isFileProto ? '<div class="askw-hint">You opened this over file:// — if requests fail, serve the page over http (e.g. <code>python3 -m http.server</code>).</div>' : '');
    document.body.appendChild(pickerEl);
    pickerEl.querySelector('.askw-picker-save').addEventListener('click', function () {
      var v = pickerEl.querySelector('.askw-picker-input').value.trim();
      if (v) setFolder(v);
    });
    if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.askwPick) {
      pickerEl.classList.add('askw-native');
      pickerEl.querySelector('.askw-picker-browse').addEventListener('click', async function () {
        try {
          var input = pickerEl.querySelector('.askw-picker-input');
          var chosen = await window.webkit.messageHandlers.askwPick.postMessage({ kind: 'folder', initial: input.value });
          if (chosen) input.value = chosen;
        } catch (e) { toast('Folder picker failed.'); }
      });
    }

    // --- the page's chats: a bubble in the corner, and the list it opens ---
    chatsEl = document.createElement('button');
    chatsEl.type = 'button';
    chatsEl.className = 'askw-root askw-chats';
    chatsEl.hidden = true;   // until the page has a chat
    chatsEl.innerHTML = '<svg class="askw-ico" aria-hidden="true" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"><path d="M8 2.5c3.3 0 6 2.2 6 5s-2.7 5-6 5c-.8 0-1.5-.1-2.2-.3L2.5 13.5l.9-2.6C2.5 10 2 8.8 2 7.5c0-2.8 2.7-5 6-5z"/></svg><span class="askw-chats-n"></span>';
    chatsCount = chatsEl.querySelector('.askw-chats-n');
    chatsEl.title = 'Chats on this page';
    chatsEl.setAttribute('aria-haspopup', 'dialog');
    chatsEl.setAttribute('aria-expanded', 'false');
    document.body.appendChild(chatsEl);
    chatsEl.addEventListener('click', toggleChats);

    chatsListEl = document.createElement('div');
    chatsListEl.className = 'askw-root askw-chats-list';
    chatsListEl.setAttribute('role', 'dialog');
    chatsListEl.setAttribute('aria-label', 'Chats on this page');
    chatsListEl.setAttribute('aria-hidden', 'true');
    chatsListEl.innerHTML = '<p class="askw-chats-title"></p>' +
      '<label class="askw-highlights-switch" hidden><input type="checkbox" aria-label="Show highlights">Show highlights</label>' +
      '<div class="askw-chats-rows"></div><section class="askw-highlights-section" hidden>' +
      '<p class="askw-highlights-heading">Saved highlights</p><div class="askw-highlights-rows"></div></section>';
    chatsRows = chatsListEl.querySelector('.askw-chats-rows');
    highlightsSection = chatsListEl.querySelector('.askw-highlights-section');
    highlightsRows = chatsListEl.querySelector('.askw-highlights-rows');
    highlightsToggle = chatsListEl.querySelector('.askw-highlights-switch input');
    highlightsToggle.addEventListener('change', function () {
      highlightsOn = highlightsToggle.checked;
      highlightsSection.hidden = !highlightsOn || !savedHighlights.length;
      paintHighlights();
    });
    document.body.appendChild(chatsListEl);
    chatsListEl.addEventListener('keydown', function (e) {
      if (!e.target.classList.contains('askw-chats-row')) return;
      var rows = Array.prototype.slice.call(chatsRows.querySelectorAll('.askw-chats-row'));
      if (!rows.length || ['ArrowDown', 'ArrowUp', 'Home', 'End'].indexOf(e.key) < 0) return;
      e.preventDefault();
      var index = rows.indexOf(document.activeElement);
      if (e.key === 'Home') index = 0;
      else if (e.key === 'End') index = rows.length - 1;
      else if (e.key === 'ArrowDown') index = (index + 1 + rows.length) % rows.length;
      else index = (index - 1 + rows.length) % rows.length;
      rows[index].focus();
    });
  }

  // ============================================================ menu
  function showTrigger(captured) {
    // Not beside the open menu either: moving the menu can end in a release over the page.
    if (!triggerEl || !captured || panelEl.classList.contains('open') || menuEl.style.display === 'block') return;
    sel = captured;
    triggerEl.style.display = 'flex';
    triggerEl.setAttribute('aria-hidden', 'false');
    var rect = captured.rect;
    var width = triggerEl.offsetWidth || 58;
    var height = triggerEl.offsetHeight || 28;
    var left = rect.left + rect.width / 2 - width / 2;
    var top = rect.bottom + 7;
    if (top + height > window.innerHeight - 8) top = rect.top - height - 7;
    triggerEl.style.left = Math.max(8, Math.min(left, window.innerWidth - width - 8)) + 'px';
    triggerEl.style.top = Math.max(8, top) + 'px';
  }
  function hideTrigger() {
    if (!triggerEl) return;
    triggerEl.style.display = 'none';
    triggerEl.setAttribute('aria-hidden', 'true');
  }
  function showMenu(x, y, pageAsk) {
    hideTrigger();
    askWrap.classList.toggle('open', !!pageAsk);
    menuEl.querySelectorAll('.askw-item').forEach(function (item) { item.style.display = pageAsk ? 'none' : ''; });
    menuEl.setAttribute('aria-label', pageAsk ? 'Ask about this page' : 'Ask about selected text');
    askInput.setAttribute('aria-label', pageAsk ? 'Question about this page' : 'Question about the highlighted text');
    askInput.placeholder = pageAsk ? 'Ask about this page…' : 'Ask about the highlighted text…';
    askInput.value = '';
    menuEl.style.display = 'block';
    menuEl.setAttribute('aria-hidden', 'false');
    placeMenu(x, y);
    var firstItem = menuEl.querySelector('.askw-item');
    if (pageAsk) askInput.focus();
    else if (firstItem) firstItem.focus();
  }
  // Put the menu's top-left corner at (x, y), kept whole inside the window.
  function placeMenu(x, y) {
    var mw = menuEl.offsetWidth || 190;
    var mh = menuEl.offsetHeight || 130;
    menuEl.style.left = Math.max(6, Math.min(x, window.innerWidth - mw - 8)) + 'px';
    menuEl.style.top = Math.max(6, Math.min(y, window.innerHeight - mh - 8)) + 'px';
  }
  function hideMenu() {
    if (!menuEl) return;
    menuEl.style.display = 'none';
    menuEl.setAttribute('aria-hidden', 'true');
  }

  function submitAsk() {
    var q = askInput.value.trim();
    if (!q) return;
    start('ask', q);
  }

  function saveHighlight() {
    if (!sel || !sel.text) return;
    var captured = sel;
    hideMenu();
    var anchors = selectionAnchors(captured);
    fetch(SERVER + '/api/highlights', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: TOKEN, document_source: documentSource(), selection: captured.text,
        context: captured.context || '', document_page: captured.page || null,
        prefix: anchors.prefix, suffix: anchors.suffix })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.ok) throw new Error(d.error || 'Could not save highlight.');
      highlightsLoad++;
      savedHighlights.unshift(d.highlight);
      renderHighlights();
      renderBubble();
      paintHighlights();
      toast('Highlight saved. Open Chats on this page to add a note.');
    }).catch(function (e) { toast(e.message || 'Could not save highlight.'); });
  }

  // ============================================================ panel
  var EYEBROW = { eli5: 'ELI5', prove: 'Prove it', ask: 'Your question' };

  function openPanel(action) {
    activeAction = action;
    panelTitle.textContent = EYEBROW[action] || 'Answer';
    panelSel.textContent = sel ? (sel.text ? '“' + sel.text + '”' : 'About this page') : '';
    panelTools.innerHTML = '';
    panelBody.innerHTML = '';
    stopBtn.style.display = 'none';
    retryBtn.style.display = 'none';
    hideFollowup();
    panelEl.classList.add('open');
    fitQuote();
    if (!userPinned) positionPanel();
    panelEl.querySelector('.askw-x').focus();
  }
  // The quoted passage shows three whole lines; a longer one ends in an ellipsis
  // and becomes a button that unfolds the rest.
  function fitQuote() {
    panelSel.classList.remove('expanded');
    var clamped = panelSel.scrollHeight > panelSel.clientHeight + 1;
    panelSel.classList.toggle('clamped', clamped);
    if (clamped) {
      panelSel.setAttribute('role', 'button');
      panelSel.setAttribute('tabindex', '0');
      panelSel.setAttribute('aria-expanded', 'false');
      panelSel.title = 'Show the whole passage';
    } else {
      ['role', 'tabindex', 'aria-expanded', 'title'].forEach(function (a) { panelSel.removeAttribute(a); });
    }
  }
  function toggleQuote() {
    if (!panelSel.classList.contains('clamped')) return;
    var open = panelSel.classList.toggle('expanded');
    panelSel.setAttribute('aria-expanded', open ? 'true' : 'false');
    panelSel.title = open ? 'Collapse the passage' : 'Show the whole passage';
  }
  function closePanel() {
    panelEl.classList.remove('open');
    panelEl.setAttribute('aria-busy', 'false');
    activeAction = null;
    if (abort) { abort.abort(); abort = null; }
    streaming = false;
    stopBtn.style.display = 'none';
    retryBtn.style.display = 'none';
    resetConversation();
    historyOrigin = null;
    hideFollowup();
    maybeApplyReload();
  }

  // ---- conversation transcript (grows in the panel body as follow-ups arrive) ----
  function resetConversation() { transcript = []; liveEl = null; }

  function renderConversation() {
    panelBody.innerHTML = '';
    transcript.forEach(function (t) {
      var el = document.createElement('div');
      if (t.role === 'user') { el.className = 'askw-q'; el.textContent = t.text; }
      else { el.className = 'askw-a'; renderMarkdown(el, t.text); }
      panelBody.appendChild(el);
    });
  }

  function historyModeLabel(mode) {
    return ({ generated: 'Generated', rerun: 'Asked again', edited: 'Edited & asked', continue: 'Continued' })[mode] || 'Generated';
  }

  function setSelectionFromHistory(item) {
    var x = Math.max(20, window.innerWidth / 2 - 1);
    var y = Math.max(70, Math.min(window.innerHeight / 3, window.innerHeight - 220));
    sel = {
      text: item.selection || '',
      context: item.context || item.selection || '',
      page: item.document_page || null,
      rect: { left: x, right: x + 2, top: y, bottom: y + 2, width: 2, height: 2 }
    };
    if (item.folder) commitFolder(item.folder);
  }

  function restoreHistory(item, mode) {
    setSelectionFromHistory(item);
    historyOrigin = { request_id: item.request_id, mode: mode };
    lastAction = item.action || 'ask';
    lastQuestion = item.question || '';
    lastAnswer = item.answer || '';
    openPanel(item.action || 'ask');
    resetConversation();

    if (mode === 'edited') {
      panelTitle.textContent = 'Edit & ask';
      var note = document.createElement('div');
      note.className = 'askw-origin';
      note.textContent = 'Editing a saved question · the next answer will use your current ' + providerLabel() + ' model';
      panelBody.appendChild(note);
      showFollowup(true);
      followInput.value = item.question || (item.action === 'eli5' ? 'Explain this passage simply.' : item.action === 'prove' ? 'What evidence supports this passage?' : '');
      autosizeFollow();
      followInput.focus();
      return;
    }

    if (item.question) transcript.push({ role: 'user', text: item.question });
    transcript.push({ role: 'assistant', text: item.answer || item.error || 'No saved answer.' });
    renderConversation();
    var origin = document.createElement('div');
    origin.className = 'askw-origin';
    origin.textContent = historyModeLabel(item.request_mode) + ' · ' + (item.provider || 'claude') + ' · ' + item.model;
    panelBody.appendChild(origin);
    if (item.citations && item.citations.length) renderCitations(item.citations);
    panelTitle.textContent = mode === 'continue' ? 'Continue saved answer' : 'Saved answer';
    showFollowup(true);
    anchorTurn(panelBody.firstElementChild);
  }

  function askAgainHistory(item) {
    setSelectionFromHistory(item);
    historyOrigin = null;
    start(item.action || 'ask', item.question || '', {
      bypass_cache: true,
      request_mode: 'rerun',
      parent_request_id: item.request_id
    });
  }
  // Append a fresh answer block (showing "Thinking…") for the stream to fill,
  // with the tool pills under it. They sit beside it rather than in it, since
  // every token re-renders the answer.
  function appendLive() {
    if (panelTools.parentNode) panelTools.parentNode.removeChild(panelTools);
    var asked = panelBody.lastElementChild;
    liveEl = document.createElement('div');
    liveEl.className = 'askw-a';
    liveEl.innerHTML = '<div class="askw-think"><span class="askw-dot"></span>Thinking…</div>';
    panelBody.appendChild(liveEl);
    panelBody.appendChild(panelTools);
    anchorTurn(asked && asked.classList.contains('askw-q') ? asked : liveEl);
  }
  function liveError(msg) {
    if (liveEl) liveEl.innerHTML = '<div class="askw-err">' + esc(msg) + '</div>';
    else showError(msg);
  }

  // ---- follow-up composer ----
  // It grows with what you type, up to four lines, and drags taller by the grip
  // in its corner. The height you drag it to is the least it keeps through
  // typing and sending, for the rest of this page's answers.
  var followFloor = 0;
  function showFollowup(enabled) {
    if (!followWrap) return;
    followWrap.style.display = 'flex';
    followInput.disabled = followGo.disabled = !enabled;
    autosizeFollow();
  }
  function hideFollowup() {
    if (!followWrap) return;
    followWrap.style.display = 'none';
    followInput.value = '';
    followInput.style.height = followFloor ? followFloor + 'px' : '';
  }
  function autosizeFollow() {
    followInput.style.maxHeight = followCap() + 'px';
    // Measure from the floor, not from nothing: collapsing a tall box even for a
    // moment would cost the answer above it its scroll position.
    followInput.style.height = followFloor ? followFloor + 'px' : 'auto';
    followInput.style.height = Math.max(followFloor, Math.min(followInput.scrollHeight, 96)) + 'px';
  }
  // The grip sizes the box by how far the pointer has moved since the press.
  // Not resize:vertical: with the panel at full height the box grows upward,
  // and WebKit, measuring from the moving box, runs it away to the cap.
  function dragFollow(e) {
    e.preventDefault();
    var y0 = e.clientY, h0 = followInput.offsetHeight, cap = followCap();
    followInput.style.maxHeight = cap + 'px';
    function mv(ev) {
      followFloor = Math.round(Math.max(34, Math.min(cap, h0 + ev.clientY - y0)));
      followInput.style.height = followFloor + 'px';
    }
    function up() { document.removeEventListener('mousemove', mv); document.removeEventListener('mouseup', up); }
    document.addEventListener('mousemove', mv);
    document.addEventListener('mouseup', up);
  }
  // The tallest the box may be while the answer keeps a few lines in view and
  // the footer stays inside the panel.
  function followCap() {
    var inner = panelEl.style.height ? panelEl.clientHeight   // the reader sized the panel
      : parseFloat(getComputedStyle(panelEl).maxHeight) - (panelEl.offsetHeight - panelEl.clientHeight);
    var rest = followWrap.offsetHeight - followInput.offsetHeight;
    for (var c = panelEl.firstElementChild; c; c = c.nextElementSibling) {
      if (c !== panelBody && c !== followWrap) rest += c.offsetHeight;
    }
    return Math.max(34, inner - rest - 96);
  }
  function positionPanel() {
    if (!sel || !sel.rect) return;
    var m = 12, vw = window.innerWidth, vh = window.innerHeight;
    var h = panelEl.offsetHeight || 240;
    var left = sel.rect.left + sel.rect.width / 2 - PANEL_W / 2;
    left = Math.max(m, Math.min(left, vw - PANEL_W - m));
    var top = sel.rect.bottom + 8;
    if (top + h > vh - m) top = sel.rect.top - 8 - h;
    top = Math.max(m, Math.min(top, vh - h - m));
    panelEl.style.left = left + 'px';
    panelEl.style.top = top + 'px';
    // The answer can grow dramatically after this initial placement. Cap its
    // material sheet to the remaining viewport so the footer/Stop button never
    // streams below the screen; the flex body becomes the scroll container.
    panelEl.style.maxHeight = Math.max(180, vh - top - m) + 'px';
  }
  // Follow a streaming answer down, but never past the top of the turn that asked
  // for it: the question stays in view and the answer reads from its first line.
  // Once the reader scrolls on their own, later tokens leave them where they are.
  var scrollAnchor = null, scrollPinned = -1;
  function anchorTurn(el) { scrollAnchor = el; scrollPinned = -1; autoscroll(); }
  function autoscroll() {
    var max = panelBody.scrollHeight - panelBody.clientHeight, now = panelBody.scrollTop;
    var clamped = scrollPinned > max && now >= max - 1;   // a re-render shrank the content
    if (scrollPinned >= 0 && Math.abs(now - scrollPinned) > 2 && !clamped) return;
    var top = max;
    if (scrollAnchor && panelBody.contains(scrollAnchor)) {
      top = Math.min(top, scrollAnchor.getBoundingClientRect().top - panelBody.getBoundingClientRect().top + now - 8);
    }
    panelBody.scrollTop = Math.max(0, top);
    scrollPinned = panelBody.scrollTop;
  }

  // Drag the panel by its header; resize from the bottom-right corner (CSS
  // resize:both). Either gesture "pins" it so auto-positioning stops fighting you.
  var panelDragged = false;   // a drag that ends on the quote is not a click on it
  function makeDragResize(panel, handle) {
    handle.addEventListener('mousedown', function (e) {
      // close button isn't a drag grip; an unfolded quote keeps its scrollbar
      if (e.target.closest('.askw-x, .askw-selq.expanded')) return;
      e.preventDefault();
      var r = panel.getBoundingClientRect();
      var sx = e.clientX, sy = e.clientY, ox = r.left, oy = r.top;
      panelDragged = false;
      function mv(ev) {
        if (!panelDragged && Math.abs(ev.clientX - sx) + Math.abs(ev.clientY - sy) < 4) return;
        panelDragged = userPinned = true;
        var nx = ox + (ev.clientX - sx), ny = oy + (ev.clientY - sy);
        nx = Math.max(4, Math.min(nx, window.innerWidth - panel.offsetWidth - 4));
        ny = Math.max(4, Math.min(ny, window.innerHeight - 44));
        panel.style.left = nx + 'px';
        panel.style.top = ny + 'px';
        panel.style.maxHeight = Math.max(180, window.innerHeight - ny - 4) + 'px';
      }
      function up() { document.removeEventListener('mousemove', mv); document.removeEventListener('mouseup', up); }
      document.addEventListener('mousemove', mv);
      document.addEventListener('mouseup', up);
    });
    // grabbing the resize corner also pins it
    panel.addEventListener('mousedown', function (e) {
      var r = panel.getBoundingClientRect();
      if (e.clientX > r.right - 22 && e.clientY > r.bottom - 22) userPinned = true;
    });
  }

  // Fallback error render into the whole body (used only when there's no live
  // answer element to target — see liveError).
  function showError(msg) {
    panelBody.innerHTML = '<div class="askw-err">' + esc(msg) + '</div>';
  }

  function toast(msg) {
    if (!toastEl) {
      toastEl = document.createElement('div');
      toastEl.className = 'askw-root askw-toast';
      toastEl.setAttribute('role', 'status');
      toastEl.setAttribute('aria-live', 'polite');
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = msg;
    toastEl.classList.add('show');
    clearTimeout(toastEl._t);
    toastEl._t = setTimeout(function () { toastEl.classList.remove('show'); }, 2400);
  }

  function providerLabel() { return serverConfig.provider === 'codex' ? 'Codex' : 'Claude'; }
  function setProviderLabel(opt) {
    if (claudeBtn && !claudeBtn.disabled) claudeBtn.textContent = opt ? 'Copy ' + providerLabel() + ' prompt' : 'Open in ' + providerLabel();
  }

  // Hand off to a dedicated provider session in the context folder. With Option
  // held (copy=true), copy the seed prompt instead of launching a terminal.
  function openInProvider(copy) {
    if (!sel || !lastAction) { toast('Ask something first.'); return; }
    claudeBtn.disabled = true;
    claudeBtn.textContent = copy ? 'Copying…' : 'Opening…';
    fetch(SERVER + '/open-in-provider', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        folder: folder, action: lastAction, selection: sel.text, context: sel.context,
        question: lastQuestion, answer: lastAnswer, provider: serverConfig.provider, mode: copy ? 'copy' : 'open', token: TOKEN
      })
    }).then(function (r) { return r.json(); }).then(function (d) {
      claudeBtn.disabled = false; setProviderLabel(false);
      if (!d || !d.ok) { toast((d && d.error) || 'Open in ' + providerLabel() + ' failed.'); return; }
      if (copy) {
        if (navigator.clipboard && d.prompt) {
          navigator.clipboard.writeText(d.prompt).then(function () { toast(providerLabel() + ' prompt copied'); }).catch(function () { toast('Copy failed'); });
        } else { toast('Copy failed'); }
      } else {
        toast('Opening a ' + providerLabel() + ' session…');
      }
    }).catch(function () {
      claudeBtn.disabled = false; setProviderLabel(false);
      toast('Could not reach the Ask server.');
    });
  }

  var toolPills = {};
  function updateTool(data) {
    if (data.status === 'calling' && !toolPills[data.tool]) {
      var span = document.createElement('span');
      span.className = 'askw-pillt';
      span.innerHTML = '<span class="askw-dot"></span>' + esc(prettyTool(data.tool));
      toolPills[data.tool] = span;
      panelTools.appendChild(span);
    } else if (data.status === 'complete' && toolPills[data.tool]) {
      panelTools.removeChild(toolPills[data.tool]);
      delete toolPills[data.tool];
    }
  }
  function clearTools() {
    toolPills = {};
    if (!panelTools) return;
    panelTools.innerHTML = '';
    if (panelTools.parentNode) panelTools.parentNode.removeChild(panelTools);
  }
  function prettyTool(t) { return String(t).replace(/_/g, ' '); }

  // ============================================================ cache
  function djb2(s) {
    var h = 5381;
    for (var i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
    return (h >>> 0).toString(36);
  }
  function metaValue(name) {
    var m = document.querySelector('meta[name="' + name + '"]');
    return m ? (m.getAttribute('content') || '').trim() : '';
  }
  function documentSource() { return metaValue('askw-src') || location.href.split('#')[0]; }
  function documentIdentity() {
    return [documentSource(), document.title || '', serverConfig.version, serverConfig.provider, serverConfig.model, serverConfig.reasoning_effort].join('|');
  }
  function cacheKey(action) {
    return 'askw:cache:' + djb2([
      action, folder || '', documentIdentity(), sel ? sel.text : '', sel ? sel.context : '', sel ? sel.page || '' : ''
    ].join('|'));
  }
  function cacheGet(k) {
    try {
      var item = JSON.parse(localStorage.getItem(k) || 'null');
      if (!item || typeof item.answer !== 'string') return null;
      var ttl = Number(serverConfig.cache_ttl_hours || 0) * 3600000;
      if (!ttl || Date.now() - Number(item.created_at || 0) > ttl) { localStorage.removeItem(k); return null; }
      return item;
    } catch (e) { return null; }
  }
  function cacheSet(k, v) {
    try {
      var max = Number(serverConfig.cache_max_entries || 0);
      if (!max) return;
      var index = JSON.parse(localStorage.getItem('askw:cache-index') || '[]');
      index = index.filter(function (item) { return item.key !== k; });
      index.unshift({ key: k, created_at: Date.now() });
      while (index.length > max) { var old = index.pop(); localStorage.removeItem(old.key); }
      localStorage.setItem(k, JSON.stringify({ answer: v, created_at: Date.now(), version: serverConfig.version }));
      localStorage.setItem('askw:cache-index', JSON.stringify(index));
    } catch (e) {}
  }

  // ============================================================ streaming
  // A top-level action (ELI5 / Prove it / Ask) opens the panel and starts a new
  // conversation. Follow-ups reuse the same panel + selection via submitFollowup.
  function start(action, question, options) {
    options = options || {};
    hideMenu();
    if (!sel) return;
    lastAction = action; lastQuestion = question || ''; lastAnswer = '';

    var ck = action !== 'ask' ? cacheKey(action) : null;
    if (ck && !options.bypass_cache) {
      if (activeAction === action) {
        try { localStorage.removeItem(ck); } catch (e) {}   // re-click = refresh
      } else {
        var cached = cacheGet(ck);
        if (cached) {
          openPanel(action);
          resetConversation();
          transcript.push({ role: 'assistant', text: cached.answer });
          lastAnswer = cached.answer;
          renderConversation();
          var cacheNote = document.createElement('div');
          cacheNote.className = 'askw-origin';
          cacheNote.textContent = 'Cached locally · ' + new Date(Number(cached.created_at || Date.now())).toLocaleString() + ' · click the same action again to refresh';
          panelBody.appendChild(cacheNote);
          showFollowup(true);
          return;
        }
      }
    }

    openPanel(action);
    resetConversation();
    if (action === 'ask' && question) transcript.push({ role: 'user', text: question });
    renderConversation();

    var request = {
      action: action,
      selection: sel.text,
      context: sel.context,
      question: question || '',
      document_source: documentSource(),
      document_title: document.title || '',
      document_page: sel.page || null,
      folder: folder,
      token: TOKEN
    };
    if (options.request_mode) request.request_mode = options.request_mode;
    if (options.parent_request_id) request.parent_request_id = options.parent_request_id;
    streamAnswer(request, ck);
  }

  // Ask a follow-up about the same selection, continuing the panel's thread.
  function submitFollowup() {
    if (!sel || !lastAction) return;
    var q = followInput.value.trim();
    if (!q) return;
    hideFollowup();           // clears the box; re-shown (enabled) when the answer lands
    lastAction = 'ask'; lastQuestion = q; lastAnswer = '';

    transcript.push({ role: 'user', text: q });
    renderConversation();

    // Send every completed turn before this question so the selected model has the thread.
    var history = transcript.slice(0, -1).map(function (t) { return { role: t.role, text: t.text }; });
    var request = {
      action: 'ask',
      selection: sel.text,
      context: sel.context,
      question: q,
      history: history,
      document_source: documentSource(),
      document_title: document.title || '',
      document_page: sel.page || null,
      folder: folder,
      token: TOKEN
    };
    if (historyOrigin) {
      request.request_mode = historyOrigin.mode === 'edited' ? 'edited' : 'continue';
      request.parent_request_id = historyOrigin.request_id;
    } else {
      request.request_mode = 'continue';
      if (currentRequestId) request.parent_request_id = currentRequestId;
    }
    streamAnswer(request, null);
  }

  // Shared stream pump: fills the live answer block, commits it to the transcript
  // on completion. `ck` is a cache key to store the result under (top-level only).
  function streamAnswer(reqBody, ck) {
    if (abort) abort.abort();
    abort = new AbortController();
    var myAbort = abort;
    lastRequestBody = JSON.parse(JSON.stringify(reqBody));
    lastCacheKey = ck;
    currentRequestId = null;
    currentRequestMode = reqBody.request_mode || 'generated';
    requestCitations = [];
    requestTrace = [];
    streaming = true;
    panelEl.setAttribute('aria-busy', 'true');
    clearTools();
    appendLive();
    showFollowup(false);
    stopBtn.style.display = 'block';
    retryBtn.style.display = 'none';

    var acc = '', hadError = false, doneMeta = null;

    function handleFrame(frame) {
      var evt = '', data = {};
      frame.split('\n').forEach(function (line) {
        if (line.indexOf('event: ') === 0) evt = line.slice(7).trim();
        else if (line.indexOf('data: ') === 0) {
          try { data = JSON.parse(line.slice(6)); } catch (e) { data = {}; }
        }
      });
      if (evt === 'meta') {
        currentRequestId = data.request_id || null;
        if (data.provider) serverConfig.provider = data.provider;
        if (data.model) serverConfig.model = data.model;
        if (data.effort) serverConfig.reasoning_effort = data.effort;
        if (data.request_mode) currentRequestMode = data.request_mode;
        setProviderLabel(false);
      } else if (evt === 'token') {
        acc += data.text || '';
        renderMarkdown(liveEl, acc);
        autoscroll();
      } else if (evt === 'tool_status') {
        updateTool(data);
      } else if (evt === 'tool_trace') {
        requestTrace.push(data);
      } else if (evt === 'status') {
        if (!acc && liveEl) liveEl.innerHTML = '<div class="askw-think"><span class="askw-dot"></span>' + esc(data.message || providerLabel() + ' is working…') + '</div>';
      } else if (evt === 'citations') {
        requestCitations = Array.isArray(data.items) ? data.items : [];
      } else if (evt === 'error') {
        hadError = true;
        liveError(data.message || 'An error occurred.');
        retryBtn.style.display = data.retryable === false ? 'none' : 'block';
        acc = '';
      } else if (evt === 'done') {
        doneMeta = data;
      }
    }

    fetch(SERVER + '/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(reqBody),
      signal: myAbort.signal
    }).then(function (resp) {
      if (!resp.ok || !resp.body) throw new Error('HTTP ' + resp.status);
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buffer = '';

      function pump() {
        return reader.read().then(function (r) {
          if (r.done) {
            if (buffer.trim()) handleFrame(buffer);
            if (myAbort === abort) finish(acc, ck, hadError, doneMeta);
            return;
          }
          buffer += decoder.decode(r.value, { stream: true });
          var frames = buffer.split(/\n\n/);
          buffer = frames.pop() || '';
          frames.forEach(handleFrame);
          return pump();
        });
      }
      return pump();
    }).catch(function (err) {
      if (err && err.name === 'AbortError') return;
      if (myAbort !== abort) return;
      liveError('Could not reach the Ask server at ' + SERVER + '. Is it running? (' + (err && err.message || err) + ')');
      retryBtn.style.display = 'block';
      finishMeta();
    });
  }

  function finish(acc, ck, hadError, meta) {
    clearTools();
    if (acc) {
      transcript.push({ role: 'assistant', text: acc });
      lastAnswer = acc;
      if (ck) cacheSet(ck, acc);
    } else if (!hadError && liveEl && liveEl.querySelector('.askw-think')) {
      liveEl.innerHTML = '<div class="askw-err">No response received.</div>';
      retryBtn.style.display = 'block';
    }
    if (requestCitations.length) renderCitations(requestCitations);
    if (meta && liveEl) {
      var detail = document.createElement('p'); detail.className = 'askw-request-meta';
      detail.textContent = [historyModeLabel(currentRequestMode), providerLabel(), serverConfig.model, serverConfig.reasoning_effort, meta.elapsed_ms ? (meta.elapsed_ms / 1000).toFixed(1) + 's' : '', currentRequestId ? currentRequestId.slice(0, 8) : ''].filter(Boolean).join(' · ');
      liveEl.appendChild(detail);
    }
    finishMeta();
    loadChats();   // the answer is saved by now, and may be the page's first
  }
  // Re-enable the composer and detach the live element after a turn settles.
  function finishMeta() {
    liveEl = null;
    streaming = false;
    panelEl.setAttribute('aria-busy', 'false');
    stopBtn.style.display = 'none';
    abort = null;
    if (currentRequestId) historyOrigin = { request_id: currentRequestId, mode: 'continue' };
    showFollowup(true);
    if (!userPinned) positionPanel();
    maybeApplyReload();
  }

  function stopRequest() {
    if (!abort) return;
    var active = abort;
    abort = null;
    active.abort();
    clearTools();
    liveError('Stopped.');
    streaming = false;
    panelEl.setAttribute('aria-busy', 'false');
    stopBtn.style.display = 'none';
    retryBtn.style.display = 'block';
    showFollowup(true);
    maybeApplyReload();
  }

  function retryRequest() {
    if (!lastRequestBody) return;
    retryBtn.style.display = 'none';
    streamAnswer(JSON.parse(JSON.stringify(lastRequestBody)), lastCacheKey);
  }

  function renderCitations(items) {
    var box = document.createElement('div'); box.className = 'askw-citations';
    box.innerHTML = '<p class="askw-citations-title">Evidence</p>';
    items.forEach(function (item) {
      var button = document.createElement('button'); button.className = 'askw-citation';
      button.title = item.snippet ? 'Click to preview, then click again to open' : 'Open source';
      button.innerHTML = '<strong>' + esc(item.label || item.path) + '</strong>' + (item.snippet ? '<pre>' + esc(item.snippet) + '</pre>' : '');
      button.addEventListener('click', function () {
        if (item.snippet && !button.classList.contains('expanded')) { button.classList.add('expanded'); return; }
        openEvidence(item);
      });
      box.appendChild(button);
    });
    panelBody.appendChild(box);
  }

  // ============================================================ evidence
  // A cited page or note opens in Onyx's reader at the cited passage, not as source in an editor (code still does).
  // The service says where: the row a vault lists the file as, and the words at the cited line. Evidence on this page
  // lands in place, the answer still open beside it; another page is navigated to and lands as it loads, the target
  // riding along in sessionStorage, so the URL carries no hash for the page's own scripts to trip on.
  var LAND_KEY = 'askw:land';
  var pendingLanding = null;
  var flashTimer = 0, flashEl = null;
  function openEvidence(item) {
    fetch(SERVER + '/api/open-source', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: TOKEN, path: item.path, line: item.line, page: item.page, folder: folder, reader: true })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.ok) { toast(d.error || 'Could not open source'); return; }
      if (!d.view) return;  // code: the editor has it
      // The words kept with the answer come first: a page edited since has moved its lines, not its words. The
      // service's reading of the line as it stands now is for answers saved before evidence kept them.
      var target = { view: d.view, text: item.text || d.text || '', anchor: item.anchor || d.anchor || '', page: d.page || null, at: Date.now() };
      if (item.path === documentSource()) {
        if (!target.text && !target.anchor && !target.page) toast('This is the page you are reading.');
        else if (!landOn(target)) toast('Could not find that passage on this page.');
        return;
      }
      if (location.origin !== SERVER) {
        window.open(SERVER + d.view + (target.anchor ? '#' + encodeURIComponent(target.anchor) : ''), '_blank', 'noopener');
        return;
      }
      try { sessionStorage.setItem(LAND_KEY, JSON.stringify(target)); } catch (e) {}
      location.href = SERVER + d.view;
    }).catch(function () { toast('Could not open source'); });
  }
  function letters(s) { return String(s || '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ''); }
  var UNREAD = { SCRIPT: 1, STYLE: 1, TEMPLATE: 1, NOSCRIPT: 1 };
  // The innermost element holding the cited words, compared on letters and digits alone, so the spaces a tag leaves,
  // curly quotes and a note's **marks** can't split them. Onyx's own panels, which may quote the passage, don't count.
  function findPassage(text) {
    var needle = letters(text).slice(0, 48), el = document.body, found = null;
    if (needle.length < 8) return null;
    for (;;) {
      var next = null;
      for (var c = el.firstElementChild; c && !next; c = c.nextElementSibling) {
        if (!UNREAD[c.tagName] && !c.classList.contains('askw-root') && letters(c.textContent).indexOf(needle) >= 0) next = c;
      }
      if (!next) return found;
      found = el = next;
    }
  }
  // Brought a third of the way down the window, out of any closed <details> (the HTML kit folds its claim table away),
  // and lit for a moment to find it by. A PDF page is its own target; the id above the cited line is the fallback when
  // the words aren't found or are out of sight (a quiz mode hides its sources).
  function landOn(target, quiet) {
    var candidates = [
      target.page ? document.querySelector('.askw-pdf-page[data-askw-page="' + Number(target.page) + '"]') : null,
      findPassage(target.text),
      target.anchor ? document.getElementById(target.anchor) : null
    ];
    for (var i = 0; i < candidates.length; i++) {
      var el = candidates[i];
      if (!el) continue;
      for (var d = el.closest('details'); d; d = d.parentElement && d.parentElement.closest('details')) d.open = true;
      if (!el.getClientRects().length) continue;
      jumpTo(Math.max(0, el.getBoundingClientRect().top + window.scrollY - window.innerHeight / 3));
      var box = el.getBoundingClientRect();
      if (box.bottom < 0 || box.top > window.innerHeight) el.scrollIntoView({ block: 'center' });  // a page that scrolls inside
      if (!quiet) flashEvidence(el);
      return true;
    }
    return false;
  }
  function flashEvidence(el) {
    clearTimeout(flashTimer);
    if (flashEl) flashEl.classList.remove('askw-evidence-hit');
    void el.offsetWidth;  // restarts the highlight when the same passage is landed on again
    el.classList.add('askw-evidence-hit');
    flashEl = el;
    flashTimer = setTimeout(function () { el.classList.remove('askw-evidence-hit'); flashEl = null; }, 1900);
  }
  // A landing the page before this one asked for: taken once, and only by the page it was for while it is fresh, so a
  // reload or a page opened later never jumps.
  function takeLanding() {
    var raw = null, target = null;
    try { raw = sessionStorage.getItem(LAND_KEY); if (raw) sessionStorage.removeItem(LAND_KEY); } catch (e) {}
    try { target = raw ? JSON.parse(raw) : null; } catch (e) {}
    if (!target || !(Date.now() - (target.at || 0) < 15000)) return null;
    var want = new URLSearchParams(String(target.view || '').split('?')[1] || '').get('src');
    return want && want === new URLSearchParams(location.search).get('src') ? target : null;
  }
  // Late images and fonts can move the passage: it is landed on again at load, unless the reader has scrolled since.
  function initLanding() {
    var target = pendingLanding;
    if (!target) return;
    landOn(target);
    var landed = window.scrollY;
    window.addEventListener('load', function () { if (Math.abs(window.scrollY - landed) < 2) landOn(target, true); });
  }

  function loadSelectionHistory() {
    if (!sel) return;
    fetch(SERVER + '/api/history?source=' + encodeURIComponent(documentSource()) +
      (sel.text ? '&selection=' + encodeURIComponent(sel.text) : '&page_only=1') + '&limit=10')
      .then(function (r) { return r.json(); }).then(function (d) {
        var items = d.conversations || [];
        if (!items.length) { toast(sel.text ? 'No saved answers for this passage yet.' : 'No saved page questions yet.'); return; }
        panelTitle.textContent = 'Saved history';
        panelBody.innerHTML = '';
        items.forEach(function (item) {
          var entry = document.createElement('section'); entry.className = 'askw-history-entry';
          var q = document.createElement('div'); q.className = 'askw-q'; q.textContent = item.question || item.action;
          var a = document.createElement('div'); a.className = 'askw-a'; renderMarkdown(a, item.answer || item.error || 'No answer');
          var meta = document.createElement('div'); meta.className = 'askw-history-meta';
          meta.textContent = historyModeLabel(item.request_mode) + ' · ' + (item.provider || 'claude') + ' · ' + item.model + (item.effort ? ' · ' + item.effort : '') + ' · ' + new Date(item.started_at * 1000).toLocaleString();
          var actions = document.createElement('div'); actions.className = 'askw-history-actions';
          var again = document.createElement('button'); again.textContent = 'Ask again'; again.onclick = function () { askAgainHistory(item); };
          var edit = document.createElement('button'); edit.textContent = 'Edit & ask'; edit.onclick = function () { restoreHistory(item, 'edited'); };
          var resume = document.createElement('button'); resume.textContent = 'Continue'; resume.onclick = function () { restoreHistory(item, 'continue'); };
          actions.appendChild(again); actions.appendChild(edit); actions.appendChild(resume);
          entry.appendChild(q); entry.appendChild(a); entry.appendChild(meta); entry.appendChild(actions);
          panelBody.appendChild(entry);
        });
        toast(items.length + ' saved answer' + (items.length === 1 ? '' : 's'));
      }).catch(function () { toast('Could not load history.'); });
  }

  // ============================================================ chats on this page
  // The corner bubble holds saved answers and manual highlights for this page.
  // A chat row continues its conversation; passage marks are opt-in here so
  // authored pages stay visually untouched until the reader asks to see them.
  var CHAT_LABEL = { eli5: 'ELI5', prove: 'Prove it', ask: 'Question' };
  var chatsLoad = 0, highlightsLoad = 0, highlightObserver = null, highlightTimer = 0, overlayFrame = 0;
  function loadSavedHighlights() {
    var mine = ++highlightsLoad;
    fetch(SERVER + '/api/highlights?source=' + encodeURIComponent(documentSource()), { cache: 'no-store' })
      .then(function (r) { return r.json(); }).then(function (d) {
        if (mine !== highlightsLoad || !d || !d.ok) return;
        savedHighlights = d.highlights || [];
        renderHighlights(); renderBubble(); paintHighlights();
      }).catch(function () { /* Saved chats still work if highlights cannot load. */ });
  }
  function renderBubble() {
    var chats = pageChats.length, saved = savedHighlights.length, total = chats + saved;
    var label = (chats ? chats + (chats === 1 ? ' chat' : ' chats') : '') +
      (saved ? (chats ? ' and ' : '') + saved + (saved === 1 ? ' highlight' : ' highlights') : '') + ' on this page';
    var title = chats ? chats + (chats === 1 ? ' chat' : ' chats') : '';
    if (saved) title += (title ? ' · ' : '') + saved + (saved === 1 ? ' highlight' : ' highlights');
    chatsListEl.querySelector('.askw-chats-title').textContent = title + ' on this page';
    chatsEl.hidden = !total;
    chatsCount.textContent = total > 99 ? '99+' : String(total);
    chatsEl.setAttribute('aria-label', label);
    chatsEl.title = title + ' on this page';
    chatsListEl.querySelector('.askw-highlights-switch').hidden =
      !saved && !pageChats.some(function (c) { return !!c.selection; });
    if (!total) closeChats();
  }
  function jumpToHighlight(item) {
    var range = passageRange(passageIndex(), item);
    if (!range) { toast('This passage no longer matches the page.'); return false; }
    for (var d = range.startContainer.parentElement.closest('details'); d; d = d.parentElement && d.parentElement.closest('details')) d.open = true;
    range.startContainer.parentElement.scrollIntoView({ block: 'center' });
    return true;
  }
  function editHighlightNote(item, card) {
    var editor = card.querySelector('.askw-highlight-editor');
    editor.hidden = false;
    var field = editor.querySelector('textarea');
    field.value = item.note || '';
    field.focus();
    function save() {
      fetch(SERVER + '/api/highlights/' + item.id, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: TOKEN, note: field.value })
      }).then(function (r) { return r.json(); }).then(function (d) {
        if (!d.ok) throw new Error(d.error || 'Could not save note.');
        item.note = d.highlight.note;
        renderHighlights();
        toast('Note saved.');
      }).catch(function (e) { toast(e.message || 'Could not save note.'); });
    }
    editor.querySelector('button').onclick = save;
    field.onkeydown = function (e) { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); save(); } };
  }
  function renderHighlights() {
    highlightsRows.innerHTML = '';
    highlightsSection.hidden = !highlightsOn || !savedHighlights.length;
    savedHighlights.forEach(function (item) {
      var card = document.createElement('div');
      card.className = 'askw-highlight-card';
      card.dataset.highlightId = item.id;
      var jump = document.createElement('button'); jump.type = 'button'; jump.className = 'askw-highlight-jump';
      jump.textContent = '“' + normalizedPassage(item.selection) + '”';
      jump.addEventListener('click', function () { jumpToHighlight(item); });
      var note = document.createElement('button'); note.type = 'button'; note.className = 'askw-highlight-note';
      note.textContent = item.note || 'Add note';
      note.addEventListener('click', function () { editHighlightNote(item, card); });
      var remove = document.createElement('button'); remove.type = 'button'; remove.className = 'askw-highlight-remove';
      remove.setAttribute('aria-label', 'Remove highlight'); remove.title = 'Remove highlight'; remove.textContent = '×';
      remove.addEventListener('click', function () {
        fetch(SERVER + '/api/highlights/' + item.id, {
          method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: TOKEN })
        }).then(function (r) { return r.json(); }).then(function (d) {
          if (!d.ok) throw new Error(d.error || 'Could not remove highlight.');
          savedHighlights = savedHighlights.filter(function (h) { return h.id !== item.id; });
          renderHighlights(); renderBubble(); paintHighlights();
        }).catch(function (e) { toast(e.message || 'Could not remove highlight.'); });
      });
      var editor = document.createElement('div'); editor.className = 'askw-highlight-editor'; editor.hidden = true;
      var field = document.createElement('textarea'); field.maxLength = 2000; field.setAttribute('aria-label', 'Highlight note');
      var save = document.createElement('button'); save.type = 'button'; save.textContent = 'Save note';
      editor.appendChild(field); editor.appendChild(save);
      card.appendChild(jump); card.appendChild(note); card.appendChild(remove); card.appendChild(editor);
      highlightsRows.appendChild(card);
    });
  }
  function redrawHighlightOverlay() {
    if (!highlightOverlay) return;
    highlightOverlay.innerHTML = '';
    markedPassages.forEach(function (mark) {
      Array.prototype.forEach.call(mark.range.getClientRects(), function (r) {
        if (!r.width || !r.height) return;
        var line = document.createElement('i');
        line.style.left = r.left + 'px'; line.style.top = r.top + 'px';
        line.style.width = r.width + 'px'; line.style.height = r.height + 'px';
        highlightOverlay.appendChild(line);
      });
    });
  }
  function laterHighlightPaint() {
    if (highlightTimer || !highlightsOn) return;
    highlightTimer = setTimeout(function () { highlightTimer = 0; paintHighlights(); }, 250);
  }
  function paintHighlights() {
    if (window.CSS && window.CSS.highlights) window.CSS.highlights.delete('askw-passages');
    if (highlightOverlay) { highlightOverlay.remove(); highlightOverlay = null; }
    markedPassages = [];
    if (!highlightsOn) {
      if (highlightObserver) { highlightObserver.disconnect(); highlightObserver = null; }
      clearTimeout(highlightTimer); highlightTimer = 0;
      return;
    }
    var index = passageIndex();
    var entries = savedHighlights.map(function (item) { return { kind: 'saved', item: item }; })
      .concat(pageChats.filter(function (item) { return !!item.selection; }).map(function (item) {
        return { kind: 'chat', item: item };
      }));
    entries.slice(0, 200).forEach(function (entry) {
      var range = passageRange(index, entry.item);
      if (range && range.getClientRects().length) markedPassages.push({ range: range, kind: entry.kind, item: entry.item });
    });
    if (window.CSS && window.CSS.highlights && window.Highlight) {
      window.CSS.highlights.set('askw-passages', new window.Highlight(...markedPassages.map(function (m) { return m.range; })));
    } else {
      highlightOverlay = document.createElement('div');
      highlightOverlay.className = 'askw-root askw-highlight-overlay';
      document.body.appendChild(highlightOverlay);
      redrawHighlightOverlay();
    }
    if (!highlightObserver) {
      highlightObserver = new MutationObserver(function (records) {
        var changed = records.some(function (r) {
          if (isOurs(r.target)) return false;
          if (r.type !== 'childList') return true;
          return Array.prototype.some.call(r.addedNodes, function (n) { return !isOurs(n); }) ||
            Array.prototype.some.call(r.removedNodes, function (n) { return !isOurs(n); });
        });
        if (changed) laterHighlightPaint();
      });
      highlightObserver.observe(document.body, { childList: true, subtree: true, characterData: true });
    }
  }
  function markedPassageClick(e) {
    if (!highlightsOn || isOurs(e.target) || e.defaultPrevented || e.target.closest('a,button,input,textarea,select,[contenteditable]')) return;
    var selected = window.getSelection();
    if (selected && !selected.isCollapsed) return;
    for (var i = 0; i < markedPassages.length; i++) {
      var mark = markedPassages[i], rects = mark.range.getClientRects();
      var hit = Array.prototype.some.call(rects, function (r) {
        return e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
      });
      if (!hit) continue;
      if (mark.kind === 'chat') openChat(mark.item);
      else {
        if (!chatsListEl.classList.contains('open')) toggleChats();
        var row = highlightsRows.querySelector('[data-highlight-id="' + mark.item.id + '"] .askw-highlight-jump');
        if (row) row.focus();
      }
      return;
    }
  }
  function loadChats() {
    if (!chatsEl) return;
    var mine = ++chatsLoad;
    fetch(SERVER + '/api/history?source=' + encodeURIComponent(documentSource()) + '&limit=100', { cache: 'no-store' })
      .then(function (r) { return r.json(); }).then(function (d) {
        if (mine !== chatsLoad || !d || !d.ok) return;
        renderChats((d.conversations || []).filter(function (c) { return c.status === 'complete'; }));
      }).catch(function () { /* Keep what the bubble last showed while offline. */ });
  }
  function renderChats(items) {
    pageChats = items;
    chatsRows.innerHTML = '';
    items.forEach(function (item) {
      var row = document.createElement('button'); row.type = 'button'; row.className = 'askw-chats-row';
      row.title = new Date(item.started_at * 1000).toLocaleString();
      var q = document.createElement('span'); q.className = 'askw-chats-q';
      q.textContent = item.question || CHAT_LABEL[item.action] || 'Question';
      var passage = document.createElement('span'); passage.className = 'askw-chats-sel';
      passage.textContent = item.selection ? '“' + String(item.selection).replace(/\s+/g, ' ').trim() + '”' : 'About this page';
      var meta = document.createElement('span'); meta.className = 'askw-chats-meta';
      meta.textContent = [ago(item.started_at), item.provider || 'claude', item.model].filter(Boolean).join(' · ');
      row.appendChild(q); row.appendChild(passage); row.appendChild(meta);
      row.addEventListener('click', function () { openChat(item); });
      chatsRows.appendChild(row);
    });
    renderBubble();
    paintHighlights();
  }
  // How long ago, in the Library home page's words: 5m, 3h, 2d, 3w, then the month.
  function ago(ts) {
    if (!ts) return '';
    var d = Math.max(0, Date.now() / 1000 - ts);
    if (d < 3600) return Math.max(1, Math.floor(d / 60)) + 'm';
    if (d < 86400) return Math.floor(d / 3600) + 'h';
    if (d < 86400 * 14) return Math.floor(d / 86400) + 'd';
    if (d < 86400 * 120) return Math.floor(d / 604800) + 'w';
    return new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', year: '2-digit' });
  }
  function toggleChats(e) {
    if (chatsListEl.classList.contains('open')) { closeChats(); return; }
    chatsListEl.classList.add('open');
    chatsListEl.setAttribute('aria-hidden', 'false');
    chatsEl.setAttribute('aria-expanded', 'true');
    // Opened from the keyboard (a click with no pointer detail), focus moves into
    // the list. A pointer click leaves it be: WebKit would ring the first row as
    // though it had been picked.
    var first = chatsRows.querySelector('.askw-chats-row');
    if (e && e.detail === 0) (first || highlightsToggle).focus();
  }
  function closeChats() {
    if (!chatsListEl) return;
    chatsListEl.classList.remove('open');
    chatsListEl.setAttribute('aria-hidden', 'true');
    chatsEl.setAttribute('aria-expanded', 'false');
  }
  function openChat(item) {
    closeChats();
    if (streaming) stopRequest();   // the pick replaces an answer still arriving
    restoreHistory(item, 'continue');
  }

  // ============================================================ folder
  function commitFolder(f) {
    folder = f;
    try { localStorage.setItem('askw:folder', f); } catch (e) {}
    if (recentFolders.indexOf(f) === -1) recentFolders.unshift(f);
    updatePill();
    renderRecent();
  }
  function setFolder(f) {
    fetch(SERVER + '/api/folder?path=' + encodeURIComponent(f)).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.ok) { toast(d.error || 'Folder is not allowed.'); return; }
      commitFolder(d.path);
      closePicker();
    }).catch(function () { toast('Could not validate that folder.'); });
  }
  function updatePill() {
    if (!pillLabel) return;
    pillLabel.textContent = basename(folder);
    pillEl.setAttribute('aria-label', 'Context folder: ' + (folder || 'none') + '. Activate to change.');
  }
  function renderRecent() {
    var box = pickerEl.querySelector('.askw-recent');
    box.innerHTML = '';
    recentFolders.slice(0, 8).forEach(function (f) {
      var d = document.createElement('button');
      d.type = 'button';
      d.className = 'askw-recent-item';
      d.textContent = f;
      d.title = f;
      d.addEventListener('click', function () { setFolder(f); });
      box.appendChild(d);
    });
  }
  function togglePicker() {
    if (pickerEl.classList.contains('open')) { closePicker(); return; }
    pickerEl.querySelector('.askw-picker-input').value = folder || '';
    renderRecent();
    pickerEl.classList.add('open');
    pickerEl.setAttribute('aria-hidden', 'false');
    pillEl.setAttribute('aria-expanded', 'true');
    pickerEl.querySelector('.askw-picker-input').focus();
  }
  function closePicker() {
    pickerEl.classList.remove('open');
    pickerEl.setAttribute('aria-hidden', 'true');
    pillEl.setAttribute('aria-expanded', 'false');
  }

  function metaFolder() {
    return metaValue('askw-folder');
  }

  function applyAppearance(raw) {
    appearanceTheme = raw === 'dark' || raw === 'light' ? raw : 'system';
    var systemDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    // While the app wears the vault look, the vault's mode stands in for the app theme.
    var effective = vaultLook ? vaultLook.mode
      : appearanceTheme === 'dark' || (appearanceTheme === 'system' && systemDark) ? 'dark' : 'light';
    document.documentElement.setAttribute('data-askw-color', effective);
    applyPageTone();
    var bridge = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.askwAppearance;
    if (bridge) Promise.resolve(bridge.postMessage({ theme: vaultLook ? vaultLook.mode : appearanceTheme })).catch(function () {});
  }

  // Glass chips lie on the page, so they take the page's tone — a dark app over a
  // cream page still gets light glass. The first opaque background wins (body,
  // then html); a transparent page shows the app's pane, so the app theme stands in.
  function pageTone() {
    var layers = [document.body, document.documentElement];
    for (var i = 0; i < layers.length; i++) {
      var m = layers[i] && /^rgba?\(([^)]*)\)/.exec(getComputedStyle(layers[i]).backgroundColor);
      if (!m) continue;
      var c = m[1].split(/[\s,\/]+/).map(Number);
      if (c.length > 3 && c[3] < 0.5) continue;
      return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2] > 140 ? 'light' : 'dark';
    }
    return document.documentElement.getAttribute('data-askw-color') || 'light';
  }
  function applyPageTone() {
    var tone = pageTone();
    if (document.documentElement.getAttribute('data-askw-page') !== tone) document.documentElement.setAttribute('data-askw-page', tone);
  }

  function initFolder() {
    // A /view page seeds the folder via <meta name="askw-folder"> (CSP blocks the
    // old inline-script seed). Meta (explicit per-open intent) wins over storage.
    var seeded = metaFolder();
    if (seeded) {
      folder = seeded;
      try { localStorage.setItem('askw:folder', seeded); } catch (e) {}
    } else {
      try { folder = localStorage.getItem('askw:folder') || folder; } catch (e) {}
    }
    updatePill();

    return fetch(SERVER + '/config').then(function (r) { return r.json(); }).then(function (cfg) {
      serverConfig = cfg || serverConfig;
      setProviderLabel(false);
      applyAppearance(cfg.appearance_theme);
      defaultFolder = cfg.default_folder;
      recentFolders = (cfg.recent_folders || []).slice();
      if (!folder) folder = defaultFolder;
      if (folder && recentFolders.indexOf(folder) === -1) recentFolders.unshift(folder);
      updatePill();
    }).catch(function () { updatePill(); });
  }

  // Markdown appearance changes in place: preserve selection, scroll, and answers.
  function initMarkdownTheme() {
    // Notes, and the other pages Onyx lays out itself (markdown_theme.KINDS); an HTML page keeps its own look —
    // and an Artifact Kit page carries its own dark mode, which follows the window appearance (markdown_theme.KINDS).
    if (['markdown', 'text', 'pdf', 'selection'].indexOf(document.body.getAttribute('data-askw-document-kind')) < 0 || !metaSrc()) return;
    var style = document.getElementById('askw-markdown-theme');
    if (!style) {
      style = document.createElement('style');
      style.id = 'askw-markdown-theme';
      document.head.appendChild(style);
    }
    var pending = false;
    function refresh() {
      if (pending || document.hidden) return;
      pending = true;
      fetch(SERVER + '/api/markdown-theme', { cache: 'no-store' }).then(function (r) {
        if (!r.ok) throw new Error('Theme unavailable');
        return r.json();
      }).then(function (theme) {
        if (typeof theme.css === 'string' && style.textContent !== theme.css) { style.textContent = theme.css; applyPageTone(); }
      }).catch(function () { /* Keep the last good appearance while offline. */ })
        .finally(function () { pending = false; });
    }
    refresh();
    window.setInterval(refresh, 2000);
    document.addEventListener('visibilitychange', refresh);
  }

  // The whole app in the vault's colours (Match vault appearance): the panel's sheet from the service
  // (vault_look.reader_stylesheet), on html[data-askw-look], after this file's own. Kept live like the reading styles.
  function applyVaultLook(look) {
    var css = (look && look.reader_css) || '';
    var style = document.getElementById('askw-vault-look');
    if (!style) {
      style = document.createElement('style');
      style.id = 'askw-vault-look';
      (document.head || document.documentElement).appendChild(style);
    }
    if (style.textContent !== css) style.textContent = css;
    vaultLook = css ? look : null;
    if (vaultLook) document.documentElement.setAttribute('data-askw-look', vaultLook.mode);
    else document.documentElement.removeAttribute('data-askw-look');
    applyAppearance(appearanceTheme);
  }
  // A /view page arrives wearing the vault look (app.py `_first_paint`): take it as the look from the start, so the first
  // appearance applied is the final one, not the app theme for a frame and then the vault's.
  function seedLook() {
    var style = document.getElementById('askw-vault-look'), mode = metaValue('askw-look');
    if (!(style && style.textContent && mode)) return;
    // After ask.js's own sheet (injectStyle, just before this), as a fetched look is: its rules match that sheet's weight
    // and win by coming later.
    (document.head || document.documentElement).appendChild(style);
    vaultLook = { mode: mode, reader_css: style.textContent };
    document.documentElement.setAttribute('data-askw-look', mode);  // set already, unless the page has no <html> tag
  }
  function initVaultLook() {
    var pending = false, revision = metaValue('askw-look-revision') || null;
    function refresh() {
      if (pending || document.hidden) return;
      pending = true;
      fetch(SERVER + '/api/vault-look', { cache: 'no-store' }).then(function (r) {
        if (!r.ok) throw new Error('Look unavailable');
        return r.json();
      }).then(function (look) {
        if (look.revision !== revision) { revision = look.revision; applyVaultLook(look); }
      }).catch(function () { /* Keep the last good look while offline, or none on a page that may not ask. */ })
        .finally(function () { pending = false; });
    }
    refresh();
    window.setInterval(refresh, 3000);
    document.addEventListener('visibilitychange', refresh);
  }

  // ============================================================ live reload
  // Only /view pages seed <meta name="askw-src">; on those, poll the file's stat
  // signature and reload when it settles on a new value, so edits from another
  // editor/agent show up in real time. Reloads are deferred while the widget is
  // busy (a stream in flight, or the answer panel open) so we never yank content
  // mid-answer; the deferred reload fires when things go idle.
  function metaSrc() {
    return metaValue('askw-src');
  }
  function reloadIdle() {
    return !streaming && !editSession && !editOpening && !(panelEl && panelEl.classList.contains('open'));
  }
  function doReload(landing) {
    try { sessionStorage.setItem('askw:reload', JSON.stringify({ src: reloadSrc, y: window.scrollY, landing: landing || null })); } catch (e) {}
    // Back from the editor the page is a different height, and the browser putting back the editor's scrollY after the
    // load undid the landing. It does that after the load event, so the new page leaves restoration off until it is left.
    if (landing) {
      landingReload = true;
      try { history.scrollRestoration = 'manual'; } catch (e) {}
    }
    location.reload();
  }
  function maybeApplyReload() {
    if (reloadPending && reloadIdle()) { reloadPending = false; doReload(); }
  }
  function checkDoc() {
    var docCap = metaValue('askw-doc-token');
    if (!reloadSrc || !docCap || document.hidden) return;
    fetch(SERVER + '/_mtime?src=' + encodeURIComponent(reloadSrc) + '&cap=' + encodeURIComponent(docCap), { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok || !d.sig) return;
        var sig = d.sig;
        if (editSession) { editSession.poll(sig); return; }   // the editor keeps its text in step with the file itself
        if (reloadSig === null) { reloadSig = reloadSeen = sig; return; }  // baseline
        if (sig !== reloadSeen) { reloadSeen = sig; return; }              // still changing — let it settle
        if (sig === reloadSig) return;                                     // unchanged from what's rendered
        reloadSig = sig;                                                   // settled on a new version
        if (reloadIdle()) { doReload(); }
        else if (!reloadPending) { reloadPending = true; toast('Document updated — refreshes when you’re done'); }
      })
      .catch(function () {});
  }
  function initLiveReload() {
    reloadSrc = metaSrc();
    if (!reloadSrc || !metaValue('askw-doc-token')) return;   // not a local /view page → no live reload
    // If this load is the result of a reload we triggered, restore the reading
    // position and confirm the refresh landed.
    try {
      var saved = JSON.parse(sessionStorage.getItem('askw:reload') || 'null');
      if (saved && saved.src === reloadSrc) {
        sessionStorage.removeItem('askw:reload');
        reloadRestoring = true;
        if (saved.landing) {
          // Back from the editor: where it was showing, and no news, since the change was the reader's own.
          landAt(saved.landing);
          window.addEventListener('load', function () { landAt(saved.landing); });
          window.addEventListener('pagehide', function () {
            if (!landingReload) try { history.scrollRestoration = 'auto'; } catch (e) {}
          });
        } else {
          window.addEventListener('load', function () { jumpTo(saved.y || 0); });
          toast('Document updated');
        }
      }
    } catch (e) {}
    checkDoc();   // establish the baseline immediately
    setInterval(checkDoc, 3000);
  }

  // ============================================================ editing (⌘E)
  // ⌘E turns a Markdown page into its source, drawn as Obsidian's Live Preview, and ⌘E again turns it back, re-rendered,
  // at the same place. Everything else about editing — saving, conflicts, links — is the editor's (editor/src/main.ts);
  // this side only opens it, hands it the page's place, and puts the page back.
  function editableNote() {
    return document.body.getAttribute('data-askw-document-kind') === 'markdown' && !!reloadSrc && !!metaValue('askw-doc-token');
  }
  function readingColumn() { return document.querySelector('body[data-askw-document-kind="markdown"] > main'); }
  // The note's top-level blocks as the page shows them, one child of <main> each; the editor counts the same blocks in
  // the source. The Properties box is the frontmatter, which the editor doesn't count.
  function readerBlocks() {
    var main = readingColumn();
    if (!main) return [];
    return Array.prototype.filter.call(main.children, function (el) {
      return !el.classList.contains('askw-properties') && !el.classList.contains('askw-ed-host') && !/^(SCRIPT|STYLE|TEMPLATE)$/.test(el.tagName);
    });
  }
  function readerLanding() {
    var blocks = readerBlocks();
    for (var i = 0; i < blocks.length; i++) {
      var r = blocks[i].getBoundingClientRect();
      if (r.bottom <= 0) continue;
      return r.top >= 0 ? { index: i, count: blocks.length, fraction: 0, offset: r.top }
                        : { index: i, count: blocks.length, fraction: -r.top / Math.max(1, r.height), offset: 0 };
    }
    return null;
  }
  function landAt(landing) {
    var blocks = readerBlocks();
    if (!landing || !blocks.length) return;
    var n = blocks.length, i = landing.count === n || landing.count < 2 ? landing.index
      : Math.round(landing.index * (n - 1) / (landing.count - 1));
    var r = blocks[Math.max(0, Math.min(n - 1, i))].getBoundingClientRect();
    jumpTo(window.scrollY + r.top + landing.fraction * r.height - landing.offset);
  }
  function loadEditor() {
    if (window.OnyxEditor) return Promise.resolve();
    if (!editorScript) {
      editorScript = new Promise(function (resolve, reject) {
        var s = document.createElement('script');
        s.src = SERVER + '/onyx-editor.js';
        s.onload = function () { resolve(); };
        s.onerror = function () { editorScript = null; reject(new Error('Couldn’t load the editor.')); };
        document.head.appendChild(s);
      });
    }
    return editorScript;
  }
  function toggleEdit() {
    if (editOpening) return;
    if (editSession) { editSession.exit(); return; }
    if (!editableNote()) { toast('Only Markdown notes can be edited'); return; }
    editOpening = true;
    reloadPending = false;   // the editor opens on the file as it is now, and a reload would throw it away
    hideMenu(); hideTrigger();
    var landing = readerLanding();
    loadEditor().then(function () {
      return window.OnyxEditor.open({
        container: readingColumn(),
        server: SERVER,
        token: TOKEN,
        src: reloadSrc,
        cap: metaValue('askw-doc-token'),
        landing: landing,
        toast: toast,
        onExit: function (result) {
          editSession = null;
          if (result.changed) { reloadSig = reloadSeen = result.sig; doReload(result.landing); }
          else landAt(result.landing);
        }
      });
    }).then(function (session) { editSession = session; }, function (err) {
      toast((err && err.message) || 'Couldn’t open the editor.');
    }).then(function () { editOpening = false; });
  }
  // The shell's ⌘E, and View ▸ Toggle Editing in the app, reach the page showing through this.
  window.askwToggleEdit = toggleEdit;

  // A task's box on the reading page ticks its line in the note, as in Obsidian's reading view, over the version this
  // page shows: the one /view read (askw-doc-sig), not the one live reload last saw, which runs ahead of the page while
  // a reload waits for the answer panel to close. If the note has moved on, the server refuses, the box goes back, and
  // live reload brings the new version. Its own write is taken as seen, so the page doesn't reload for it.
  function initTasks() {
    if (!editableNote()) return;
    var shown = metaValue('askw-doc-sig');
    // A draft the editor kept as its page went (editor/src/main.ts): news only if the file doesn't already have it,
    // which it does whenever the save sent on the way out landed.
    var draftKey = 'askw:draft:' + reloadSrc, draft = null;
    try { draft = JSON.parse(localStorage.getItem(draftKey) || 'null'); } catch (e) {}
    if (draft && typeof draft.text === 'string') {
      fetch(SERVER + '/api/source?src=' + encodeURIComponent(reloadSrc) + '&cap=' + encodeURIComponent(metaValue('askw-doc-token')), { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d.ok) return;
          if (d.text === draft.text) { try { localStorage.removeItem(draftKey); } catch (e) {} }
          else toast('Edits to this note never reached the file — ⌘E to get them back');
        }).catch(function () {});
    }
    document.addEventListener('click', function (e) {
      var box = e.target && e.target.closest ? e.target.closest('input.askw-task-box') : null;
      if (!box || editSession) return;
      var line = box.getAttribute('data-askw-line'), done = box.checked, item = box.closest('li');
      if (line === null) { e.preventDefault(); return; }
      if (item) item.classList.toggle('is-done', done);
      var undo = function (message) {
        box.checked = !done;
        if (item) item.classList.toggle('is-done', !done);
        toast(message);
      };
      fetch(SERVER + '/api/source/task', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: TOKEN, src: reloadSrc, cap: metaValue('askw-doc-token'), line: Number(line), done: done, base: shown })
      }).then(function (r) {
        return r.json().then(function (d) {
          if (r.status === 409) return undo('This note changed on disk; showing the new version');
          if (!d.ok) return undo(d.error || 'Couldn’t change that task');
          shown = reloadSig = reloadSeen = d.sig;
        });
      }).catch(function () { undo('Onyx isn’t answering'); });
    });
  }

  // A plain HTML page that paints no background of its own used to sit on the
  // window's material, which tinted it. The window is now a raw desktop blur, so
  // such a page would put its text straight on the wallpaper. Give it the page
  // canvas a browser would. Only at the top level — inside the vault shell the
  // pane behind the reader supplies the tint — and never for Onyx's own
  // reading shells, whose translucency is deliberate.
  function guardTransparentCanvas() {
    if (window.top !== window || document.body.hasAttribute('data-askw-document-kind')) return;
    var clear = function (el) {
      var style = getComputedStyle(el);
      return style.backgroundImage === 'none' && /^(transparent|rgba\([^)]*,\s*0\))$/.test(style.backgroundColor);
    };
    if (clear(document.documentElement) && clear(document.body)) {
      document.documentElement.style.backgroundColor = 'Canvas';
    }
  }

  // To a position at once. A page's own `scroll-behavior:smooth` (the HTML kit sets it) would otherwise ride the whole page
  // past from the top on every open.
  function jumpTo(y) {
    var root = document.documentElement, was = root.style.scrollBehavior;
    root.style.scrollBehavior = 'auto';
    window.scrollTo(0, y);
    root.style.scrollBehavior = was;
  }
  // Taken again once the page has loaded, since late images and fonts can move it, unless the reader has scrolled meanwhile.
  function restorePosition(y) {
    if (!(y > 0)) return;
    jumpTo(y);
    var landed = window.scrollY;
    window.addEventListener('load', function () { if (Math.abs(window.scrollY - landed) < 2 && landed < y) jumpTo(y); });
  }
  function initPosition() {
    var source = documentSource();
    if (!source) return;
    // A #fragment (a guide's "#predict" link) is where the reader asked to land, and a live reload puts back its own
    // position: neither is overridden. A /view page brings its remembered position with it (<meta name="askw-scroll">),
    // so it is taken before the first frame rather than jumped to after a fetch.
    // initLiveReload has already taken the marker out of sessionStorage by now, and says so in reloadRestoring.
    var reloading = reloadRestoring;
    try { reloading = reloading || !!sessionStorage.getItem('askw:reload'); } catch (e) {}
    if (!location.hash && !reloading && !pendingLanding) {
      var seeded = metaValue('askw-scroll');
      if (seeded !== '') restorePosition(Number(seeded));
      else fetch(SERVER + '/api/document?source=' + encodeURIComponent(source)).then(function (r) { return r.json(); }).then(function (d) {
        if (d.document) restorePosition(d.document.scroll_y);
      }).catch(function () {});
    }
    var timer = null;
    window.addEventListener('scroll', function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        fetch(SERVER + '/api/position', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ token: TOKEN, source: source, scroll_y: window.scrollY })
        }).catch(function () {});
      }, 700);
    }, { passive: true });
  }

  function initAutoSelection() {
    if (new URLSearchParams(location.search).get('history')) return;
    if (!metaValue('askw-auto-selection')) return;
    var target = document.getElementById('askw-quick-selection');
    if (!target) return;
    var range = document.createRange(); range.selectNodeContents(target);
    var selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
    sel = captureFromSelection();
    if (sel) setTimeout(function () { showMenu(Math.max(20, window.innerWidth / 2 - 90), Math.max(80, target.getBoundingClientRect().bottom + 8)); }, 150);
  }

  function initHistoryReplay() {
    var params = new URLSearchParams(location.search);
    var id = params.get('history');
    if (!id) return;
    var mode = params.get('history_action') || 'continue';
    fetch(SERVER + '/api/conversations/' + encodeURIComponent(id))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok || !d.conversation) throw new Error((d && d.error) || 'Saved answer not found.');
        if (mode === 'rerun') askAgainHistory(d.conversation);
        else restoreHistory(d.conversation, mode === 'edited' ? 'edited' : 'continue');
      })
      .catch(function (e) { toast(e.message || 'Could not restore saved answer.'); });
  }

  // ============================================================ pinned chrome
  // A page's pinned chrome (a sticky contents rail, a sticky bar of switches, a fixed
  // header, Onyx's own pill and panel) stays beside or over the text on screen, but it
  // sits elsewhere in the DOM, usually before all of the text. A selection dragged onto
  // it ran from there to the pointer: the whole page above the passage. So while the
  // button is down, pinned elements that don't hold the press let the pointer through
  // to the text beside or beneath them. A press on one's background (a sticky bar's
  // faded lower edge, which shows the text through it) starts at that text instead,
  // and once it turns into a drag the bar lets it through too; a click stays a click.
  var PASS_ATTR = 'data-askw-passthrough';
  var SVG_NS = 'http://www.w3.org/2000/svg';
  var PRESS_CONTROLS = 'a[href],button,input,select,textarea,label,summary,audio,video,' +
    '[contenteditable]:not([contenteditable="false"]),[tabindex]:not([tabindex="-1"]),' +
    '[role=button],[role=link],[role=tab],[role=checkbox],[role=radio],[role=switch],' +
    '[role=menuitem],[role=option],[role=slider],[role=textbox]';
  var passing = [], pendingPress = null;

  // Sticky and fixed elements, except any that cover the viewport: a full-page layer
  // (a modal's backdrop, an app shell) is the page, not chrome beside it.
  function pinnedChrome() {
    var found = [], stack = document.body ? [document.body] : [];
    var wide = window.innerWidth * 0.9, tall = window.innerHeight * 0.9;
    while (stack.length) {
      for (var el = stack.pop().firstElementChild; el; el = el.nextElementSibling) {
        var style = getComputedStyle(el);
        if (style.display === 'none') continue;
        if (style.position === 'sticky' || style.position === 'fixed') {
          var box = el.getBoundingClientRect();
          if (box.width < wide || box.height < tall) { found.push(el); continue; }
        }
        if (el.namespaceURI !== SVG_NS) stack.push(el);   // nothing inside an <svg> can be pinned
      }
    }
    return found;
  }

  function letThrough(el) {
    el.setAttribute(PASS_ATTR, '');
    passing.push(el);
  }

  function stopLettingThrough() {
    for (var i = 0; i < passing.length; i++) passing[i].removeAttribute(PASS_ATTR);
    passing = [];
    pendingPress = null;
  }

  // Is (x, y) on a glyph, not merely nearest one? caretRangeFromPoint answers with the
  // closest caret even from the empty padding beside a line.
  function pointOnText(x, y) {
    var caret = document.caretRangeFromPoint(x, y);
    if (!caret || caret.startContainer.nodeType !== 3) return false;
    var node = caret.startContainer, probe = document.createRange();
    for (var i = Math.max(0, caret.startOffset - 1); i <= caret.startOffset && i < node.length; i++) {
      probe.setStart(node, i);
      probe.setEnd(node, i + 1);
      var rects = probe.getClientRects();
      for (var k = 0; k < rects.length; k++) {
        var r = rects[k];
        if (x >= r.left - 2 && x <= r.right + 2 && y >= r.top - 2 && y <= r.bottom + 2) return true;
      }
    }
    return false;
  }

  // The caret in the text beneath `home` at (x, y), or null when there is none.
  function caretBeneath(home, x, y) {
    home.forEach(function (el) { el.setAttribute(PASS_ATTR, ''); });
    var caret = document.caretRangeFromPoint(x, y);
    home.forEach(function (el) { el.removeAttribute(PASS_ATTR); });
    if (!caret || caret.startContainer.nodeType !== 3) return null;
    for (var i = 0; i < home.length; i++) if (home[i].contains(caret.startContainer)) return null;
    return caret;
  }

  function watchPinnedChrome() {
    document.addEventListener('mousedown', function (e) {
      stopLettingThrough();
      if (e.button !== 0 || isOurs(e.target)) return;
      var home = [];
      pinnedChrome().forEach(function (el) {
        if (el.contains(e.target)) home.push(el);
        else letThrough(el);
      });
      // A press on the chrome itself: its text and controls work as they always did.
      if (!home.length || e.shiftKey || e.detail > 1 || !document.caretRangeFromPoint) return;
      if (e.target.closest(PRESS_CONTROLS) || pointOnText(e.clientX, e.clientY)) return;
      var caret = caretBeneath(home, e.clientX, e.clientY);
      if (!caret) return;
      pendingPress = { home: home, x: e.clientX, y: e.clientY };
      // Once the engine has put its own caret in the chrome, move it to the text.
      setTimeout(function () { window.getSelection().collapse(caret.startContainer, caret.startOffset); }, 0);
    }, true);
    document.addEventListener('mousemove', function (e) {
      var press = pendingPress;
      if (!press || Math.abs(e.clientX - press.x) + Math.abs(e.clientY - press.y) < 4) return;
      pendingPress = null;
      press.home.forEach(letThrough);
    }, true);
    ['mouseup', 'dragstart', 'dragend', 'pointercancel'].forEach(function (type) {
      document.addEventListener(type, stopLettingThrough, true);
    });
    window.addEventListener('blur', stopLettingThrough);
  }

  // ============================================================ global events
  function wire() {
    var debounce = null;
    watchPinnedChrome();
    document.addEventListener('mouseup', function (e) {
      if (isOurs(e.target)) return;
      if (debounce) clearTimeout(debounce);
      debounce = setTimeout(function () {
        var captured = captureFromSelection();
        if (captured) showTrigger(captured);
        else hideTrigger();
      }, 120);
    });

    document.addEventListener('contextmenu', function (e) {
      if (isOurs(e.target)) { rightClickSelection = null; return; } // native menu inside our UI
      var captured = rightClickSelection === false ? null : captureFromSelection();
      rightClickSelection = null;
      if (!captured) {
        // Keep the browser's controls on authored links, media, and editable fields.
        if (e.target.closest('a,button,input,textarea,select,img,video,audio,iframe,[contenteditable],[role="button"]')) {
          hideMenu(); hideTrigger(); return;
        }
        window.getSelection().removeAllRanges();
        sel = { text: '', context: pageContext(), rect: { left: e.clientX, right: e.clientX,
          top: e.clientY, bottom: e.clientY, width: 0, height: 0 }, page: null };
        e.preventDefault();
        showMenu(e.clientX, e.clientY, true);
        return;
      }
      sel = captured;
      e.preventDefault();
      showMenu(e.clientX, e.clientY);
    });

    document.addEventListener('mousedown', function (e) {
      if (e.button === 2 || (e.button === 0 && e.ctrlKey)) {
        var current = window.getSelection();
        var rects = current && !current.isCollapsed && current.rangeCount ? current.getRangeAt(0).getClientRects() : [];
        rightClickSelection = Array.prototype.some.call(rects, function (r) {
          return e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
        });
      }
      if (isOurs(e.target)) return;
      hideMenu();
      hideTrigger();
      closePicker();
      closeChats();
    });

    document.addEventListener('click', markedPassageClick);
    function moveOverlay() {
      if (!highlightOverlay || overlayFrame) return;
      overlayFrame = requestAnimationFrame(function () { overlayFrame = 0; redrawHighlightOverlay(); });
    }
    window.addEventListener('scroll', moveOverlay, true);
    window.addEventListener('resize', moveOverlay);

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Alt') { setProviderLabel(true); return; }
      if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'e' && (editSession || editableNote())) {
        e.preventDefault();
        toggleEdit();
        return;
      }
      if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === 'a') {
        var captured = captureFromSelection();
        if (captured) {
          e.preventDefault();
          sel = captured;
          showMenu(captured.rect.left + captured.rect.width / 2 - 95, captured.rect.bottom + 8);
        }
        return;
      }
      if (e.key !== 'Escape') return;
      if (menuEl.style.display === 'block') { hideMenu(); }
      else if (triggerEl.style.display === 'flex') { hideTrigger(); }
      else if (pickerEl.classList.contains('open')) { closePicker(); pillEl.focus(); }
      else if (chatsListEl.classList.contains('open')) { closeChats(); chatsEl.focus(); }
      else if (panelEl.classList.contains('open')) { closePanel(); }
    });
    document.addEventListener('keyup', function (e) {
      if (e.key === 'Alt') { setProviderLabel(false); return; }
      if (e.shiftKey || e.key === 'Shift') {
        var captured = captureFromSelection();
        if (captured) showTrigger(captured);
      }
    });
    document.addEventListener('selectionchange', function () {
      var selection = window.getSelection();
      if (!selection || selection.isCollapsed) hideTrigger();
    });
    window.addEventListener('blur', function () { setProviderLabel(false); hideTrigger(); });
    window.addEventListener('scroll', hideTrigger, { passive: true });

    window.addEventListener('resize', function () {
      hideTrigger();
      if (panelEl.classList.contains('open') && !userPinned) positionPanel();
    });
  }

  // A page that starts in Onyx's reader while the shell is in Page Only is marked
  // before anything is drawn, so its buttons never show for a frame. The shell
  // marks and unmarks the pages already open as ⌘B toggles it (vault_ui.py).
  function initPageOnly() {
    try {
      var shell = window.frameElement && window.parent.onyxShell;
      if (shell && shell.pageOnly && shell.pageOnly()) document.documentElement.setAttribute('data-askw-page-only', '');
    } catch (e) {}
  }

  // ============================================================ boot
  function boot() {
    initPageOnly();
    injectStyle();
    guardTransparentCanvas();
    seedLook();
    applyAppearance(metaValue('askw-appearance') || 'system');
    if (window.matchMedia) window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () {
      if (appearanceTheme === 'system') applyAppearance('system');
    });
    window.addEventListener('load', applyPageTone);
    build();
    wire();
    initFolder().then(initHistoryReplay);
    initLiveReload();
    initTasks();
    initMarkdownTheme();
    initVaultLook();
    pendingLanding = takeLanding();
    initPosition();
    initLanding();
    loadChats();
    loadSavedHighlights();
    initAutoSelection();
  }
  if (document.body) boot();
  else document.addEventListener('DOMContentLoaded', boot);
})();
