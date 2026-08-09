"""Runs, queued and paced, so something other than a terminal can watch one.

The engine behind `transkrp --serve`, and deliberately ignorant of HTTP: it
knows about a queue, a worker, progress and failure, and nothing about requests.
That separation is the point — this tool is going to be absorbed into a larger
research application whose integration route is undecided, and every route
(import the engine, wrap it in a framework, shell out to the CLI) is a rewrite
of the adapter rather than of this file. ADR 0018.

The shape of the work is set by the rate limiter, not by the fetching. YouTube
allows a few hundred caption pulls an hour per IP, so there is **one worker**,
serial across every run, pacing itself the way `main()` does. Two runs queue
behind each other rather than racing. Concurrency here would buy a progress bar
that fills faster and a block that arrives sooner.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from collections import deque

import podcast
import transkrp

# Same pause `main()` takes between fetches, for the same reason: a playlist is
# exactly the traffic shape that trips a per-IP limiter. Paced on fetches, not
# on skips — a resume shouldn't crawl past work it isn't doing.
PACE_SECONDS = 1.0

# Formats whose output is cues rather than paragraphs, and so need the segments
# that `transcript()` leaves out by default.
CUE_FORMATS = ("srt", "vtt")


def _error(exc: BaseException) -> dict:
    """A failure a caller can branch on, rather than a string to regex.

    The kinds are the distinctions the tool already makes and that a UI has to
    render differently: a rate limit stops the run, a missing video is skipped
    past, an absent caption track is a fact about the video rather than a fault.
    ADR 0007 gave every failure a next step; this keeps the step attached to a
    machine-readable name.
    """
    kinds = [
        (transkrp.RateLimited, "rate_limited"),
        (transkrp.NoCaptions, "no_captions"),
        (transkrp.Unavailable, "unavailable"),
        (podcast.NotFound, "not_found"),
        (OSError, "write_failed"),
    ]
    kind = next((k for cls, k in kinds if isinstance(exc, cls)), "other")
    return {"kind": kind, "message": str(exc)}


class Item:
    """One video, and everything known about it so far."""

    def __init__(self, index: int, url: str):
        self.index = index
        self.url = url
        self.status = "pending"  # pending | running | done | failed | skipped
        self.title = ""
        self.video_id = transkrp.video_id(url) or ""
        self.message = ""  # the latest progress line, verbatim
        self.path = ""
        self.error: dict | None = None
        self.stats: dict | None = None
        # The fetched transcript, kept so the page can preview it without
        # re-reading (or re-fetching) anything. Never serialised wholesale.
        self.transcript: dict | None = None

    def snapshot(self) -> dict:
        """The wire shape. Explicitly not `self.__dict__`.

        A TypeScript client is going to be written against this eventually, so
        every field is present with a stable type on every item — absent-means-
        pending would push the null checks into the consumer, and the one field
        that could be huge (the transcript) is fetched on its own endpoint.
        """
        return {
            "index": self.index,
            "url": self.url,
            "status": self.status,
            "title": self.title,
            "video_id": self.video_id,
            "message": self.message,
            "path": self.path,
            "error": self.error,
            "stats": self.stats,
            "has_document": self.transcript is not None,
        }


class Run:
    """A set of URLs, fetched in order, with somewhere to put the results."""

    def __init__(self, run_id: str, urls: list[str], options: dict, out_dir: str):
        self.id = run_id
        self.requested = list(urls)  # what was typed, before playlists expand
        self.options = options
        self.out_dir = out_dir
        self.status = "queued"  # queued|expanding|running|done|stopped|cancelled|failed
        self.created_at = time.time()
        self.items: list[Item] = []
        self.error: dict | None = None
        # Which reading of an ambiguous input was taken, and anything else the
        # CLI would have said on stderr. ADR 0008 and ADR 0015 both turn on
        # saying this out loud; a UI that dropped it would reintroduce exactly
        # the "asked for 40, got 1, never found out why" failure.
        self.notes: list[str] = []
        self.cancelling = False

    def counts(self) -> dict:
        by = {"pending": 0, "running": 0, "done": 0, "failed": 0, "skipped": 0}
        for it in self.items:
            by[it.status] += 1
        return {"total": len(self.items), **by}

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "created_at": self.created_at,
            "out_dir": self.out_dir,
            "options": self.options,
            "requested": self.requested,
            "notes": self.notes,
            "error": self.error,
            "counts": self.counts(),
            "items": [it.snapshot() for it in self.items],
        }


class Runner:
    """The queue, the worker, and the runs it has seen.

    Every side effect is injected so the tests can run offline, which ADR 0010
    requires of everything in CI: the defaults are the real functions and the
    suite passes fakes.
    """

    def __init__(self, out_dir: str = ".", proxy: str | None = None,
                 cookies: str | None = None, *, fetch=None, expand=None,
                 write=None, existing=None, sleep=None, attribute=None):
        self.out_dir = out_dir
        # Launch-time, not per-run: a page that could name a proxy or ask the
        # server to read a browser's cookie jar is a different security question
        # than a page that can fetch a video. ADR 0018.
        self.proxy = proxy
        self.cookies = cookies
        self._fetch = fetch or transkrp.transcript
        self._expand = expand or transkrp.expand
        self._write = write or transkrp._write
        self._existing = existing or transkrp._already_written
        self._sleep = sleep or time.sleep
        self._attribute = attribute or _attribute
        self._runs: dict[str, Run] = {}
        self._queue: deque[str] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._worker: threading.Thread | None = None
        self._paced = False  # has anything actually been fetched yet
        # Identities learned across runs, the same dotfile the CLI keeps beside
        # its output so a corpus remembers who it has already met.
        self._people = transkrp._load_people(out_dir)

    # -- public surface -----------------------------------------------------

    def submit(self, urls: list[str], options: dict | None = None) -> Run:
        run = Run(secrets.token_urlsafe(8), urls, _clean_options(options or {}),
                  self.out_dir)
        for u in run.requested:
            _note_reading(run, u)
        with self._lock:
            self._runs[run.id] = run
            self._queue.append(run.id)
            self._start()
            self._wake.notify()
        return run

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            return self._runs.get(run_id)

    def snapshot(self, run_id: str) -> dict | None:
        with self._lock:
            run = self._runs.get(run_id)
            return run.snapshot() if run else None

    def all_snapshots(self) -> list[dict]:
        with self._lock:
            return [r.snapshot() for r in
                    sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)]

    def cancel(self, run_id: str) -> Run | None:
        """Stop the queue. Not the fetch already in flight.

        Honest about which: a whisper run is minutes of CPU inside one call and
        there is nothing to interrupt it with, so cancel means "nothing further"
        rather than "stop now". A queued run is cancelled outright.
        """
        with self._lock:
            run = self._runs.get(run_id)
            if not run or run.status in ("done", "failed", "cancelled", "stopped"):
                return run
            run.cancelling = True
            if run.status == "queued":
                run.status = "cancelled"
                if run_id in self._queue:
                    self._queue.remove(run_id)
            return run

    def document(self, run_id: str, index: int, fmt: str | None = None) -> str | None:
        """A finished item's document, rendered on demand.

        Addressed by run and index rather than by path: the browser never names
        a file, so there is no filesystem reachable through this. ADR 0018.
        """
        with self._lock:
            run = self._runs.get(run_id)
            if not run or not 0 <= index < len(run.items):
                return None
            item = run.items[index]
            t = item.transcript
            if t is None:
                return None
            fmt = fmt or run.options["format"]
        # Rendering outside the lock: a 33,000-word interview is not something
        # to hold the worker's mutex through.
        if fmt in CUE_FORMATS and "segments" not in t:
            return None  # fetched as prose; the cues were never asked for
        return transkrp.render(t, fmt)

    def preview(self, run_id: str, index: int) -> dict | None:
        """The document as structure, with a resolving link on every paragraph.

        The page renders from this rather than parsing the markdown we just
        generated: `transcript()` already returns paragraphs, and re-deriving
        them in the browser would mean shipping a markdown parser to recover
        what we threw away one function earlier. The anchors come from `_at()`
        so the link a reader clicks in the page is the one they'd click in the
        file — a citation that resolves, in both places. ADR 0018.
        """
        with self._lock:
            run = self._runs.get(run_id)
            if not run or not 0 <= index < len(run.items):
                return None
            item = run.items[index]
            t = item.transcript
            if t is None:
                return None
        return {
            "title": t["title"],
            "channel": t["channel"],
            "upload_date": t["upload_date"],
            "url": t["url"],
            "source": t["source"],
            "lang": t["lang"],
            "translated": t["translated"],
            "duration_ms": t["duration_ms"],
            "chapters": t["chapters"],
            "sponsors_removed": t.get("sponsors_removed") or [],
            "paragraphs": [
                {"timestamp": p["timestamp"], "text": p["text"], "turn": p["turn"],
                 "speaker": p.get("speaker") or "",
                 # Carried so the page can mark a shaky attribution as shaky.
                 # A guess that renders identically to a quote is the failure
                 # ADR 0012 and ADR 0017 both exist to prevent.
                 "speaker_confidence": p.get("speaker_confidence") or "",
                 "anchor": transkrp._at(t["url"], p["start_ms"])}
                for p in t["paragraphs"]
            ],
        }

    # -- the worker ---------------------------------------------------------

    def _start(self) -> None:
        """Started on first submit, not at construction: a server nobody has
        used yet shouldn't have a thread sitting in it."""
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._loop, daemon=True,
                                            name="transkrp-worker")
            self._worker.start()

    def _loop(self) -> None:
        while True:
            with self._wake:
                while not self._queue:
                    self._wake.wait()
                run = self._runs[self._queue.popleft()]
            try:
                self._run(run)
            except Exception as e:  # a bug here must not take the worker down
                with self._lock:
                    run.status = "failed"
                    run.error = {"kind": "internal", "message": f"{type(e).__name__}: {e}"}

    def _run(self, run: Run) -> None:
        if not self._expand_into(run):
            return
        for item in run.items:
            with self._lock:
                if run.cancelling:
                    run.status = "cancelled"
                    return
                item.status = "running"
            if self._skip(run, item):
                continue
            if not self._fetch_one(run, item):
                return  # rate limited; the run is over
        with self._lock:
            run.status = "done"

    def _expand_into(self, run: Run) -> bool:
        """Playlists and feeds become their entries, on the worker.

        Flat extraction is still a network call, so it belongs here rather than
        in the request that submitted the run — a channel with 200 videos would
        otherwise hold the POST open.
        """
        with self._lock:
            run.status = "expanding"
        urls: list[str] = []
        for raw in run.requested:
            try:
                urls.extend(self._expand(raw, self.proxy, self.cookies,
                                         run.options["playlist"]))
            except LookupError as e:
                with self._lock:
                    run.status = "failed"
                    run.error = _error(e)
                return False
        with self._lock:
            run.items = [Item(i, u) for i, u in enumerate(urls)]
            run.status = "running"
        return True

    def _skip(self, run: Run, item: Item) -> bool:
        """Has this one already been written? Answered without spending a request.

        The id is in the filename, so a resume can tell what it has before
        making the call that would be rate-limited. ADR 0009.
        """
        opts = run.options
        if not opts["skip_existing"] or opts["force"]:
            return False
        have = self._existing(item.url, run.out_dir, None, opts["format"])
        if not have:
            return False
        with self._lock:
            item.status = "skipped"
            item.path = have
            # Say how to override it. Skipping is right for a resumed playlist
            # and wrong for someone who just ticked a new option and expected
            # the document to be rebuilt with it — and from the outside those
            # two look identical. ADR 0007's rule, that a failure carries its
            # next step, applies just as well to a refusal to act.
            #
            # Names the checkbox that did this rather than `--force`, which
            # would also work: one is in front of you and the other is folded
            # away under the less usual settings, and an instruction is only
            # useful if it points at something you can see.
            item.message = (f"already have {os.path.basename(have)} — untick "
                            f"'skip what I already have' to fetch it again")
        return True

    def _fetch_one(self, run: Run, item: Item) -> bool:
        """Fetch, attribute, write. False only when the whole run must stop."""
        opts = run.options
        if self._paced:
            self._sleep(PACE_SECONDS)
        self._paced = True

        def progress(msg: str) -> None:
            with self._lock:
                item.message = msg

        try:
            t = self._fetch(item.url, opts["lang"], self.proxy, opts["words"],
                            self.cookies,
                            segments_too=opts["format"] in CUE_FORMATS,
                            strip_sponsors=opts["strip_sponsors"],
                            episode=opts["episode"],
                            whisper_model=opts["whisper_model"],
                            hotwords=opts["hotwords"],
                            progress=progress)
        except transkrp.RateLimited as e:
            # Every remaining item fails the same way and asking makes the block
            # worse. Stop, and say how to pick the run back up — the same
            # bargain `main()` strikes, ADR 0009.
            with self._lock:
                item.status = "failed"
                item.error = _error(e)
                left = sum(1 for i in run.items if i.status == "pending")
                run.status = "stopped"
                run.notes.append(
                    f"Rate limited with {left} of {len(run.items)} not fetched. "
                    f"YouTube allows a few hundred caption pulls an hour per IP. "
                    f"Run this again later with 'skip what I already have' on and "
                    f"it resumes for free.")
            return False
        except LookupError as e:
            with self._lock:
                item.status = "failed"
                item.error = _error(e)
            return True

        with self._lock:
            item.transcript = t
            item.title = t["title"]
            item.video_id = t["video_id"]

        if opts["speakers"]:
            self._attribute(t, opts["model"], self._people, progress)
            transkrp._save_people(run.out_dir, self._people)

        try:
            path = self._write_item(run, item, t)
        except OSError as e:
            with self._lock:
                item.status = "failed"
                item.error = _error(e)
            return True

        with self._lock:
            item.status = "done"
            item.path = path
            # The unit the file actually holds: a .srt has cues, not paragraphs,
            # and reporting paragraphs for it would be a lie.
            item.stats = {
                "paragraphs": len(t["paragraphs"]),
                "cues": len(t.get("segments") or []),
                "unit": "cues" if opts["format"] in CUE_FORMATS else "paragraphs",
                "lang": t["lang"],
                "source": t["source"],
                "translated": t["translated"],
                "duration_ms": t["duration_ms"],
                "turns": t["turns"],
            }
            item.message = ""
        return True

    def _write_item(self, run: Run, item: Item, t: dict) -> str:
        ext = run.options["format"]
        name = f"{transkrp.slug(t['title'], t['video_id'])}.{ext}"
        path = os.path.join(run.out_dir, name)
        self._write(path, transkrp.render(t, ext))
        return path


def _attribute(t: dict, model: str | None, people: dict, progress) -> None:
    """Speaker names, via the `claude` CLI, reported into the item.

    A thin re-do of the CLI's `_attribute` rather than a call to it: that one
    takes an argparse namespace and reports by printing to stderr, neither of
    which is available here. The actual work is `speakers` and `ontology`, both
    imported lazily so an install without the [speakers] extras still serves.
    """
    try:
        import ontology
        import speakers
    except ImportError:
        progress("speakers: needs pip install '.[speakers]'")
        return
    try:
        result = speakers.attribute(t, model, corpus=people)
    except LookupError as e:
        progress(f"speakers: {e}")
        return
    speakers.apply(t, result)
    people.update(ontology.confirmed(t, result, people))
    progress(f"speakers: {', '.join(result['speakers']) or 'none identified'} "
             f"({result['attributed']} of {len(t['paragraphs'])} paragraphs)")


def _note_reading(run: Run, url: str) -> None:
    """Say which reading of an ambiguous URL was taken.

    Both readings are defensible and the tool cannot know which was meant, so
    silently picking one is how somebody asks for 40 videos, gets 1, and never
    finds out why. The CLI prints these; the page shows them.
    """
    if run.options["playlist"]:
        return
    if transkrp.is_ambiguous(url):
        run.notes.append(
            "That link names one video inside a playlist, so it fetched just "
            "that video. Tick 'whole playlist' to take the list.")
    elif podcast.is_podcast(url) and not url.startswith(podcast.REF):
        run.notes.append(
            "That names a show, so it took the most recent episode. Every "
            "episode is a separate transcription run — tick 'whole playlist' "
            "to take them all.")


# The options a page is allowed to set. Everything else — where files go, the
# proxy, the cookie jar — is fixed when the server is launched.
DEFAULTS = {
    "format": "md",
    "lang": None,
    "words": transkrp.TARGET_WORDS,
    "playlist": False,
    "skip_existing": True,
    "force": False,
    "strip_sponsors": False,
    "speakers": False,
    "model": None,
    "episode": None,
    "whisper_model": podcast.MODEL,
    "hotwords": "",
}


def _clean_options(raw: dict) -> dict:
    """Coerce what arrived over the wire into what `transcript()` expects.

    Unknown keys are dropped rather than passed through: these end up as keyword
    arguments to a function that fetches URLs, and "whatever JSON the client
    sent" is not an argument list.
    """
    opts = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        if key not in raw or raw[key] is None:
            continue
        value = raw[key]
        if isinstance(default, bool):
            opts[key] = bool(value)
        elif isinstance(default, int):
            try:
                opts[key] = max(1, int(value))
            except (TypeError, ValueError):
                pass
        else:
            opts[key] = str(value)
    if opts["format"] not in ("md", "json", "srt", "vtt"):
        opts["format"] = "md"
    # A whisper model name is also accepted as a *path* by faster-whisper, which
    # makes an unchecked one a way to point the server at an arbitrary directory.
    # Only the published sizes get through.
    if opts["whisper_model"] not in WHISPER_MODELS:
        opts["whisper_model"] = DEFAULTS["whisper_model"]
    # Supplied hotwords go ahead of the feed's own and share whisper's prompt
    # window, so an unbounded string here would crowd out the metadata entirely.
    opts["hotwords"] = opts["hotwords"][:podcast.HOTWORD_CHARS]
    return opts


WHISPER_MODELS = (
    "tiny", "tiny.en", "base", "base.en", "small", "small.en",
    "medium", "medium.en", "large-v1", "large-v2", "large-v3", "large",
    "distil-small.en", "distil-medium.en", "distil-large-v3", "turbo",
)
