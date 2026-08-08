"""The page, held as one string.

A module rather than a data file because the distribution ships `py-modules`
(see `pyproject.toml`) — a `.html` alongside it would need package-data wiring
for no gain, and `pip install transkrp` has to be the whole install story. No
build step, no framework, nothing fetched from anywhere: ADR 0018.

The design has one idea. The console is monospace throughout, because it is
machinery — a queue, states, timecodes. The preview is set in a serif, because
by then it is something a person reads. That switch is the tool's whole job made
visible: YouTube's caption track goes in, prose comes out.
"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>transkrp</title>
<style>
:root {
  --bg: #14161d;
  --surface: #1a1d26;
  --raised: #212634;
  --line: #2b3040;
  --text: #e8e5dd;
  --dim: #878c9d;
  --faint: #5a6072;
  /* The signal colours are the closed-caption palette this tool spends its life
     reading, desaturated to something you can look at for an hour. They mark
     state and nothing else — never a fill, never decoration. */
  --live: #6fdfe8;
  --good: #93d69a;
  --held: #e8c27a;
  --bad:  #ef8585;
  --mono: ui-monospace, "Cascadia Mono", "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;
  --read: "Iowan Old Style", Georgia, "Palatino Linotype", Palatino, serif;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 400 14px/1.55 var(--mono);
  font-variant-ligatures: none;
}
a { color: var(--live); }
::selection { background: #2f4a52; }
:focus-visible { outline: 2px solid var(--live); outline-offset: 2px; }

/* -- the bar ------------------------------------------------------------ */
.bar {
  display: flex; align-items: baseline; gap: .6rem; flex-wrap: wrap;
  padding: .85rem 1.25rem;
  border-bottom: 1px solid var(--line);
  background: var(--surface);
  position: sticky; top: 0; z-index: 5;
}
.mark { letter-spacing: .22em; text-transform: lowercase; }
.bar .sep { color: var(--faint); }
.bar .what { color: var(--dim); }
.dest { margin-left: auto; color: var(--dim); font-size: 12.5px; }
.dest b { color: var(--text); font-weight: 400; }

main { max-width: 62rem; margin: 0 auto; padding: 1.5rem 1.25rem 5rem; }

/* -- composing a run ---------------------------------------------------- */
.compose {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 3px;
}
.compose > * { padding: 0 1rem; }
.lede { color: var(--dim); font-size: 12.5px; padding-top: .9rem; }
textarea {
  display: block; width: 100%; margin: .5rem 0 .9rem;
  padding: .65rem .7rem;
  background: var(--bg); color: var(--text);
  border: 1px solid var(--line); border-radius: 2px;
  font: inherit; resize: vertical; min-height: 4.2rem;
}
textarea::placeholder { color: var(--faint); }
.strip {
  display: flex; flex-wrap: wrap; gap: .5rem .9rem; align-items: center;
  padding-bottom: .9rem;
}
label.tick { display: inline-flex; align-items: center; gap: .4rem; color: var(--dim); cursor: pointer; }
label.tick:hover { color: var(--text); }
input[type=checkbox] { accent-color: var(--live); width: .95rem; height: .95rem; margin: 0; }
select, input[type=text], input[type=number] {
  background: var(--bg); color: var(--text);
  border: 1px solid var(--line); border-radius: 2px;
  font: inherit; padding: .25rem .4rem;
}
.go {
  display: flex; align-items: center; gap: .9rem; flex-wrap: wrap;
  border-top: 1px solid var(--line);
  padding-top: .8rem; padding-bottom: .8rem;
  background: var(--raised);
}
button {
  font: inherit; cursor: pointer; border-radius: 2px;
  border: 1px solid var(--line); background: var(--raised); color: var(--text);
  padding: .4rem .8rem;
}
button:hover:not(:disabled) { border-color: var(--faint); }
button:disabled { opacity: .45; cursor: default; }
button.primary { background: var(--live); border-color: var(--live); color: #0d1013; }
button.primary:hover:not(:disabled) { background: #86e9f1; }
.hint { color: var(--faint); font-size: 12.5px; }
details.more { border-top: 1px solid var(--line); }
details.more summary {
  cursor: pointer; color: var(--dim); padding: .6rem 0; font-size: 12.5px;
  list-style: none;
}
details.more summary::-webkit-details-marker { display: none; }
details.more summary::before { content: "+ "; color: var(--faint); }
details.more[open] summary::before { content: "- "; }
.fields { display: flex; flex-wrap: wrap; gap: .8rem 1.2rem; padding-bottom: .9rem; }
.fields label { color: var(--dim); display: inline-flex; align-items: center; gap: .4rem; }

/* -- notes -------------------------------------------------------------- */
.note {
  border-left: 2px solid var(--held);
  background: color-mix(in srgb, var(--held) 7%, transparent);
  color: var(--text);
  padding: .6rem .8rem; margin: .8rem 0; font-size: 13px;
}
.note.stop { border-color: var(--bad); background: color-mix(in srgb, var(--bad) 8%, transparent); }

/* -- the ledger --------------------------------------------------------- */
.run { margin-top: 2rem; }
.run-head {
  display: flex; align-items: baseline; gap: .8rem; flex-wrap: wrap;
  color: var(--dim); font-size: 12.5px;
  padding-bottom: .5rem; border-bottom: 1px solid var(--line);
}
.run-head .st { color: var(--text); }
.run-head .grow { flex: 1; }

.row {
  display: grid; grid-template-columns: 5.5rem 1fr auto;
  gap: .9rem; align-items: baseline;
  padding: .7rem 0; border-bottom: 1px solid var(--line);
}
.tc {
  color: var(--faint); font-variant-numeric: tabular-nums;
  text-align: right; white-space: nowrap;
}
.row[data-status=running] .tc { color: var(--live); }
.row[data-status=done] .tc { color: var(--good); }
.row[data-status=failed] .tc { color: var(--bad); }
.row[data-status=skipped] .tc { color: var(--held); }
.title { margin: 0; font: inherit; font-weight: 400; overflow-wrap: anywhere; }
.row[data-status=pending] .title { color: var(--dim); }
.state { color: var(--dim); font-size: 12px; letter-spacing: .06em; }
.row[data-status=running] .state { color: var(--live); }
.row[data-status=failed] .state { color: var(--bad); }
.meta { color: var(--dim); font-size: 12.5px; margin-top: .2rem; overflow-wrap: anywhere; }
.meta.err { color: var(--bad); }
.acts { display: flex; gap: .4rem; }
.acts button { padding: .2rem .5rem; font-size: 12.5px; color: var(--dim); }
.acts button:hover { color: var(--text); }

/* The one animated thing on the page, and it carries information: this row is
   the one holding the queue up. */
.dot { display: inline-block; width: .45rem; height: .45rem; border-radius: 50%;
       background: var(--faint); margin-right: .35rem; vertical-align: middle; }
.row[data-status=running] .dot { background: var(--live); animation: pulse 1.4s ease-in-out infinite; }
.row[data-status=done] .dot { background: var(--good); }
.row[data-status=failed] .dot { background: var(--bad); }
.row[data-status=skipped] .dot { background: var(--held); }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .25; } }
@media (prefers-reduced-motion: reduce) {
  .row[data-status=running] .dot { animation: none; }
}

.empty { color: var(--faint); padding: 2.5rem 0; text-align: center; }

/* -- the preview: where the machinery stops and the reading starts ------- */
.preview {
  background: var(--surface); border: 1px solid var(--line); border-top: none;
  padding: 1.4rem 1.5rem 1.6rem;
  margin-bottom: .2rem;
}
.preview .about { color: var(--dim); font-size: 12.5px; margin-bottom: 1.2rem; }
.preview .about b { color: var(--text); font-weight: 400; }
.prose { font: 400 17px/1.7 var(--read); max-width: 34em; }
.prose p { margin: 0 0 1.15em; text-indent: 0; }
.prose a.at {
  font: 400 12px/1 var(--mono); color: var(--faint);
  text-decoration: none; margin-right: .5em; white-space: nowrap;
}
.prose a.at:hover { color: var(--live); text-decoration: underline; }
.prose .who { font-family: var(--mono); font-size: 12px; color: var(--live);
              letter-spacing: .04em; margin-right: .5em; }
/* A low-confidence attribution must not read like a quoted one. */
.prose .who.maybe { color: var(--held); }
.prose .who.maybe::after { content: "?"; }
pre.raw {
  font: 400 12.5px/1.6 var(--mono); color: var(--dim);
  white-space: pre-wrap; overflow-wrap: anywhere; margin: 0;
  max-height: 30rem; overflow-y: auto;
}
.pvbar { display: flex; gap: .5rem; margin-bottom: 1rem; }
.pvbar button.on { border-color: var(--live); color: var(--text); }

@media (max-width: 34rem) {
  .row { grid-template-columns: 4.5rem 1fr; }
  .acts { grid-column: 2; }
  .prose { font-size: 16px; }
}
</style>
</head>
<body>

<header class="bar">
  <span class="mark">transkrp</span>
  <span class="sep">/</span>
  <span class="what">fetch console</span>
  <span class="dest" id="dest"></span>
</header>

<main>
  <section class="compose">
    <p class="lede" id="lede">A video, a playlist, a channel, or a podcast. One per line.</p>
    <textarea id="urls" spellcheck="false" placeholder="https://www.youtube.com/watch?v=..."></textarea>
    <div class="strip">
      <label class="tick">save as
        <select id="format">
          <option value="md">markdown</option>
          <option value="json">json</option>
          <option value="srt">srt</option>
          <option value="vtt">vtt</option>
        </select>
      </label>
      <label class="tick"><input type="checkbox" id="playlist"> whole playlist</label>
      <label class="tick"><input type="checkbox" id="skip_existing" checked> skip what I already have</label>
      <label class="tick"><input type="checkbox" id="strip_sponsors"> strip sponsor reads</label>
      <label class="tick"><input type="checkbox" id="speakers"> name the speakers</label>
    </div>
    <details class="more">
      <summary>less usual settings</summary>
      <div class="fields">
        <label>caption track <input type="text" id="lang" size="8" placeholder="auto"></label>
        <label>paragraph length <input type="number" id="words" min="20" max="400" step="10" size="4"></label>
        <label>podcast episode <input type="text" id="episode" size="18" placeholder="most recent"></label>
        <label>names to expect <input type="text" id="hotwords" size="22" placeholder="only for podcasts"></label>
        <label>whisper model <select id="whisper_model"></select></label>
        <label class="tick"><input type="checkbox" id="force"> refetch anyway</label>
      </div>
    </details>
    <div class="go">
      <button class="primary" id="fetch">Fetch</button>
      <span class="hint" id="hint"></span>
    </div>
  </section>

  <div id="ledger"></div>
</main>

<script>
(function () {
  "use strict";

  // The token is in the URL this page was opened at. Everything else the page
  // needs comes from /api/config, so nothing about the tool is hardcoded here.
  var TOKEN = new URLSearchParams(location.search).get("t") || "";
  var POLL_MS = 600;

  var $ = function (id) { return document.getElementById(id); };
  var el = function (tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  };

  function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ "X-Transkrp-Token": TOKEN }, opts.headers || {});
    return fetch(path, opts).then(function (r) {
      var isJson = (r.headers.get("Content-Type") || "").indexOf("json") >= 0;
      return (isJson ? r.json() : r.text()).then(function (body) {
        if (!r.ok) {
          var msg = (body && body.error && body.error.message) || String(body);
          throw new Error(msg);
        }
        return body;
      });
    });
  }

  // -- words for states. Named for what happened, not for the enum. --------
  var ITEM_WORD = {
    pending: "queued", running: "fetching", done: "done",
    failed: "failed", skipped: "already had"
  };
  var RUN_WORD = {
    queued: "waiting its turn", expanding: "reading the list", running: "fetching",
    done: "finished", stopped: "stopped", cancelled: "cancelled", failed: "failed"
  };
  var ACTIVE = { queued: 1, expanding: 1, running: 1 };

  function clock(ms) {
    var t = Math.max(0, Math.floor(ms / 1000));
    var s = t % 60, m = Math.floor(t / 60) % 60, h = Math.floor(t / 3600);
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    return h ? h + ":" + pad(m) + ":" + pad(s) : m + ":" + pad(s);
  }

  // -- state ---------------------------------------------------------------
  var config = null;
  var started = {};   // "runId:index" -> when this item was first seen running
  var opened = {};    // "runId:index" -> the open preview's mode, if any
  var nodes = {};     // runId -> {root, head, items: {index: {...}}}
  var timer = null, ticker = null;

  // -- composing -----------------------------------------------------------
  function options() {
    return {
      format: $("format").value,
      playlist: $("playlist").checked,
      skip_existing: $("skip_existing").checked,
      strip_sponsors: $("strip_sponsors").checked,
      speakers: $("speakers").checked,
      force: $("force").checked,
      lang: $("lang").value.trim() || null,
      episode: $("episode").value.trim() || null,
      hotwords: $("hotwords").value.trim() || null,
      words: Number($("words").value) || null,
      whisper_model: $("whisper_model").value || null
    };
  }

  function submit() {
    var urls = $("urls").value.split(/\r?\n/).map(function (s) { return s.trim(); })
                              .filter(Boolean);
    if (!urls.length) { say("Paste a link first."); $("urls").focus(); return; }
    $("fetch").disabled = true;
    say("");
    api("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls: urls, options: options() })
    }).then(function () {
      $("urls").value = "";
      poll();
    }).catch(function (e) {
      say(e.message);
    }).then(function () {
      $("fetch").disabled = false;
    });
  }

  function say(msg) { $("hint").textContent = msg || ""; }

  // -- polling. Cheap, reload-proof, and honest about how often anything
  //    actually changes: a caption fetch is seconds, not frames. -----------
  function poll() {
    api("/api/runs").then(function (data) {
      draw(data.runs || []);
      var busy = (data.runs || []).some(function (r) { return ACTIVE[r.status]; });
      clearTimeout(timer);
      if (busy) timer = setTimeout(poll, POLL_MS);
    }).catch(function (e) {
      say(e.message);
      clearTimeout(timer);
      timer = setTimeout(poll, 3000);
    });
  }

  // -- drawing. Nodes are updated in place rather than rebuilt, so an open
  //    preview survives every poll and nothing flickers. --------------------
  function draw(runs) {
    var ledger = $("ledger");
    if (!runs.length) {
      if (!ledger.firstChild) {
        ledger.appendChild(el("p", "empty",
          "Nothing fetched yet. Paste a link above and press Fetch."));
      }
      return;
    }
    var empty = ledger.querySelector(".empty");
    if (empty) empty.remove();

    var seen = {};
    runs.forEach(function (run, i) {
      seen[run.id] = 1;
      var view = nodes[run.id] || makeRun(run);
      // Newest first, matching the order the server hands them back.
      if (ledger.children[i] !== view.root) {
        ledger.insertBefore(view.root, ledger.children[i] || null);
      }
      updateRun(view, run);
    });
    Object.keys(nodes).forEach(function (id) {
      if (!seen[id]) { nodes[id].root.remove(); delete nodes[id]; }
    });
  }

  function makeRun(run) {
    var root = el("section", "run");
    var head = el("div", "run-head");
    var st = el("span", "st");
    var counts = el("span", "counts");
    var what = el("span", "grow");
    var cancel = el("button", "", "Stop");
    cancel.addEventListener("click", function () {
      cancel.disabled = true;
      api("/api/runs/" + run.id + "/cancel", { method: "POST" })
        .then(poll).catch(function (e) { say(e.message); });
    });
    head.append(st, counts, what, cancel);
    var notes = el("div", "notes");
    var list = el("div", "items");
    root.append(head, notes, list);
    var view = { root: root, st: st, counts: counts, what: what, cancel: cancel,
                 notes: notes, list: list, items: {} };
    nodes[run.id] = view;
    return view;
  }

  function updateRun(view, run) {
    view.st.textContent = RUN_WORD[run.status] || run.status;
    var c = run.counts;
    var bits = [];
    if (c.done) bits.push(c.done + " written");
    if (c.skipped) bits.push(c.skipped + " already had");
    if (c.failed) bits.push(c.failed + " failed");
    view.counts.textContent = c.total
      ? (bits.length ? bits.join(", ") + " of " + c.total : c.total + " to fetch")
      : "";
    view.what.textContent = run.requested.length > 1
      ? run.requested.length + " links" : "";
    view.cancel.hidden = !ACTIVE[run.status];
    view.cancel.disabled = false;

    // Notes and the run-level error, both of which exist to stop the page
    // silently picking a reading. Rebuilt wholesale: there are never many.
    var wanted = (run.notes || []).map(function (n) { return { text: n, stop: false }; });
    if (run.error) wanted.push({ text: run.error.message, stop: true });
    if (view.notesKey !== JSON.stringify(wanted)) {
      view.notesKey = JSON.stringify(wanted);
      view.notes.textContent = "";
      wanted.forEach(function (n) {
        view.notes.appendChild(el("div", n.stop ? "note stop" : "note", n.text));
      });
    }

    run.items.forEach(function (item) {
      var iv = view.items[item.index] || makeItem(view, run, item);
      updateItem(iv, run, item);
    });
  }

  function makeItem(view, run, item) {
    var row = el("article", "row");
    var tc = el("div", "tc");
    var body = el("div");
    var line = el("div");
    var dot = el("span", "dot");
    var state = el("span", "state");
    var title = el("h3", "title");
    var meta = el("div", "meta");
    line.append(dot, state, document.createTextNode(" "), title);
    body.append(line, meta);
    var acts = el("div", "acts");
    var read = el("button", "", "Read");
    var copy = el("button", "", "Copy path");
    acts.append(read, copy);
    row.append(tc, body, acts);

    var pv = el("div", "preview");
    pv.hidden = true;
    var key = run.id + ":" + item.index;

    read.addEventListener("click", function () {
      if (!pv.hidden) { pv.hidden = true; delete opened[key]; read.textContent = "Read"; return; }
      opened[key] = opened[key] || "prose";
      read.textContent = "Close";
      pv.hidden = false;
      showPreview(pv, run, item, opened[key], key);
    });
    copy.addEventListener("click", function () {
      navigator.clipboard.writeText(iv.path || "").then(function () {
        copy.textContent = "Copied";
        setTimeout(function () { copy.textContent = "Copy path"; }, 1200);
      });
    });

    view.list.append(row, pv);
    var iv = { row: row, tc: tc, state: state, title: title, meta: meta,
               read: read, copy: copy, pv: pv, path: "" };
    view.items[item.index] = iv;
    return iv;
  }

  function updateItem(iv, run, item) {
    var key = run.id + ":" + item.index;
    iv.row.dataset.status = item.status;
    iv.state.textContent = ITEM_WORD[item.status] || item.status;
    iv.title.textContent = item.title || item.url;
    iv.path = item.path || "";

    if (item.status === "running" && !started[key]) started[key] = Date.now();

    // The timecode column. Never a percentage: this tool cannot know how long a
    // caption fetch will take, and a bar that pretends otherwise is the kind of
    // guess the rest of the project refuses to make.
    if (item.status === "running") {
      iv.tc.textContent = clock(Date.now() - started[key]);
    } else if (item.stats) {
      iv.tc.textContent = clock(item.stats.duration_ms);
    } else {
      iv.tc.textContent = "--:--";
    }

    var meta = "", err = false;
    if (item.error) {
      meta = item.error.message; err = true;
    } else if (item.status === "done" && item.stats) {
      var s = item.stats;
      meta = s[s.unit] + " " + s.unit + " · " + s.lang + " (" + s.source + ")"
           + (s.translated ? " · machine-translated" : "")
           + (item.path ? " · " + item.path : "");
    } else if (item.message) {
      meta = item.message;
    }
    // Nothing else: a queued item has no title yet, so the row already shows
    // its URL where the title goes. Repeating it underneath said it twice.
    iv.meta.textContent = meta;
    iv.meta.classList.toggle("err", err);

    var readable = item.has_document;
    iv.read.hidden = !readable;
    iv.copy.hidden = !item.path;
    if (!readable && !iv.pv.hidden) { iv.pv.hidden = true; delete opened[key]; }
  }

  // -- the preview ---------------------------------------------------------
  function showPreview(pv, run, item, mode, key) {
    pv.textContent = "";
    var bar = el("div", "pvbar");
    var asProse = el("button", mode === "prose" ? "on" : "", "Reading view");
    var asRaw = el("button", mode === "raw" ? "on" : "", "The file itself");
    bar.append(asProse, asRaw);
    var slot = el("div");
    pv.append(bar, slot);
    asProse.addEventListener("click", function () {
      opened[key] = "prose"; showPreview(pv, run, item, "prose", key);
    });
    asRaw.addEventListener("click", function () {
      opened[key] = "raw"; showPreview(pv, run, item, "raw", key);
    });

    slot.appendChild(el("p", "empty", "Loading…"));
    var base = "/api/runs/" + run.id + "/items/" + item.index;

    if (mode === "raw") {
      api(base + "/document").then(function (text) {
        slot.textContent = "";
        slot.appendChild(el("pre", "raw", text));
      }).catch(function (e) {
        slot.textContent = ""; slot.appendChild(el("p", "empty", e.message));
      });
      return;
    }

    api(base + "/preview").then(function (p) {
      slot.textContent = "";
      var about = el("div", "about");
      var who = el("b", null, p.channel || "");
      about.append(who);
      var facts = [];
      if (p.upload_date) facts.push(p.upload_date);
      facts.push(p.lang + " (" + p.source + ")");
      if (p.translated) facts.push("machine-translated");
      if (p.sponsors_removed.length) {
        facts.push(p.sponsors_removed.length + " sponsor read"
                   + (p.sponsors_removed.length > 1 ? "s" : "") + " cut");
      }
      about.appendChild(document.createTextNode(
        (p.channel ? " · " : "") + facts.join(" · ")));
      var prose = el("div", "prose");
      p.paragraphs.forEach(function (para) {
        var node = el("p");
        var at = el("a", "at", para.timestamp);
        at.href = para.anchor;
        at.target = "_blank";
        at.rel = "noreferrer";
        node.appendChild(at);
        if (para.speaker) {
          var spk = el("span",
            "who" + (para.speaker_confidence === "low" ? " maybe" : ""),
            para.speaker);
          if (para.speaker_confidence === "low") {
            spk.title = "The model was unsure about this attribution.";
          }
          node.appendChild(spk);
        }
        node.appendChild(document.createTextNode(para.text));
        prose.appendChild(node);
      });
      slot.append(about, prose);
    }).catch(function (e) {
      slot.textContent = ""; slot.appendChild(el("p", "empty", e.message));
    });
  }

  // -- boot ----------------------------------------------------------------
  function boot() {
    api("/api/config").then(function (c) {
      config = c;
      var dest = $("dest");
      dest.textContent = "writing to ";
      dest.appendChild(el("b", null, c.out_dir));
      var extra = [];
      if (c.proxy) extra.push("through a proxy");
      if (c.cookies) extra.push("with cookies");
      if (extra.length) dest.append(document.createTextNode(" · " + extra.join(", ")));
      $("words").value = c.defaults.words;
      var sel = $("whisper_model");
      c.whisper_models.forEach(function (m) {
        var o = el("option", null, m);
        o.value = m;
        if (m === c.defaults.whisper_model) o.selected = true;
        sel.appendChild(o);
      });
    }).catch(function (e) { say(e.message); });

    poll();
    // Keeps the elapsed column honest between polls without asking the server
    // for something it does not know either.
    ticker = setInterval(function () {
      Object.keys(nodes).forEach(function (id) {
        var view = nodes[id];
        Object.keys(view.items).forEach(function (i) {
          var iv = view.items[i];
          if (iv.row.dataset.status === "running" && started[id + ":" + i]) {
            iv.tc.textContent = clock(Date.now() - started[id + ":" + i]);
          }
        });
      });
    }, 1000);
  }

  $("fetch").addEventListener("click", submit);
  $("urls").addEventListener("keydown", function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submit();
  });
  boot();
})();
</script>
</body>
</html>
"""
