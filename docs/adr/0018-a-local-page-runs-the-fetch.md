# 0018. A local page runs the fetch, and the engine underneath it is framework-free

- Status: Accepted
- Date: 2026-08-08

## Context

[ADR 0016](0016-obsidian-is-the-graph-viewer.md) refused to build a viewer,
because Obsidian already draws graphs and already had the corpus open. That
record is sometimes read as "this project doesn't do UI". It isn't — the
argument was narrower than that, and applying it here gives the opposite answer.

The test 0016 actually applied was *is there an incumbent that has already won?*
For the fetching half there isn't. Checked before designing anything:

- **The self-hosted yt-dlp UIs** — MeTube, `yt-dlp-web-ui`, `yt-dlp-web` — are
  downloaders. They fetch media, and at best hand you the raw `.vtt` that
  [ADR 0002](0002-json3-not-vtt.md) exists to avoid. None of them produce the
  paragraphed, timestamp-anchored document that is this tool's entire output.
- **The hosted transcript sites** (Tactiq and the dozens like it) are closer, and
  structurally cannot do the things this tool is for: they run on *their* IP with
  *their* (absent) cookies, so age-restricted videos, `--cookies`, playlist runs
  and `--speakers` are all off the table, and the file lands in a download folder
  rather than in your vault.

So the gap is real. What forces the design is not the fetching but the rate
limiter: a few hundred caption pulls an hour per IP, which is why `main()`
fetches serially with a 1s pace and aborts the whole run on `RateLimited`
instead of retrying into a block. A UI that invited four playlists at once would
be *worse* than the CLI, not better.

The other new fact is that this tool is going to be absorbed into a larger
research application later, whose frontend will be React/TypeScript and whose
integration route — imported as a library, run as a service, shelled out to — is
genuinely undecided. That argues against committing to any of the three now, and
strongly against a decision that only stays cheap if one of them wins.

## Decision

**A local page, served by `transkrp --serve`, that runs fetches and shows them
happening. It is a runner, not a reader.**

**The job engine is framework-free and lives on its own** (`jobs.py`). It owns
the queue, the single worker, progress capture and the error taxonomy, and it
knows nothing about HTTP. `server.py` is a thin adapter over it. This is the
whole answer to the undecided integration: importing the engine, wrapping it in
FastAPI, or leaving it behind a CLI are all rewrites of the ~150-line adapter
rather than of the thing that took the thinking. Nothing about the merge has to
be decided today.

**One worker, serial across every run, because the constraint is per-IP and not
per-job.** Two runs queue behind each other rather than racing; the same 1s pace
and the same abort-on-`RateLimited` as
[ADR 0009](0009-batch-runs-resume.md), with the page saying what the CLI says —
how many were left, and that re-running resumes for free. Concurrency here would
buy a spinner that fills up faster and a block that arrives sooner.

**The page is stdlib-only, one file, no build step.** `pip install transkrp` has
to be the whole install story; a page that needed `npm run build` first, or that
shipped a committed `dist/`, would bolt a second toolchain onto a tool whose
selling point in `pyproject.toml` is that its core has one runtime dependency.
That the larger tool will be React is not a reason to make *this* page React —
it stays a standalone tool after the merge, and it has to keep working with
nothing installed but Python.

**What the React answer does change is the JSON.** The wire shapes are written
for a TypeScript client that doesn't exist yet: an explicit `status` field
discriminating each union, errors as `{kind, message}` objects rather than
prose to be regex'd, no sentinel values, no field that changes type. That costs
nothing now and is the difference between the eventual integration being a
`.d.ts` and being an archaeology exercise.

**The preview renders from `paragraphs`, not from the markdown.** `transcript()`
already returns `[{start_ms, timestamp, turn, text}]` and `_at()` already builds
the seek URL, so the preview is a loop with a clickable timestamp on every
paragraph — and a raw toggle showing the actual file text. Re-parsing our own
markdown in the browser would mean shipping a markdown parser to recover
structure we threw away one function earlier.

**The output directory is fixed at launch and cannot be typed into the page.**
`transkrp --serve -o ./notes/` decides where files go; the browser addresses
documents by run id and item index, never by path. A localhost server that
accepts a filesystem path from a web page is one stray fetch away from being a
file-write primitive. For the same reason the server binds `127.0.0.1`, pins the
`Host` header to localhost so a DNS-rebinding page can't reach it, checks
`Origin` on writes, and requires a token minted at launch and carried in the URL
it opens.

## Consequences

`--serve` is the first thing here that is a *process* rather than a command, and
that brings a class of problem the CLI never had: state that outlives a request,
a worker that can be mid-whisper-run when you close the tab, and cancellation
that cannot preempt an in-flight fetch. Cancel is therefore honest about being
between-items — it stops the queue, not the transcription already running.

Polling, not SSE or websockets. The events this reports are seconds apart at
best — a caption fetch, a paragraph pass, a whisper chunk — so a 500ms poll of a
JSON snapshot costs nothing, survives a page reload with no reconnect logic, and
doesn't hold a thread per open tab in a `ThreadingHTTPServer`.

The page joins `obsidian.py` as a generated-artifact surface with a hand-written
HTML file behind it, and the same caveat applies: it is not a place to put work
you want to keep. Everything it produces is the same file the CLI would have
written, in the same directory, which is what keeps the Obsidian workflow of
0016 intact — you point `--serve` at the vault and the notes land in it.
