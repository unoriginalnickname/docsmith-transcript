"""Optional: pull still frames out of the video, for text the captions can't carry.

Captions are speech. What a video *draws on screen* is not in them at all, and
the words that matter most are the ones speech recognition handles worst. One
real transcript rendered "Fervor" as `furvore`, "Fervid" as `vervid` and
`furvid`, "Aegis" as `ais`, "affixes" as `aixes`. Fervor and Fervid are two
different things and the captions collapsed both into the same noise. A single
frame showing the game's own UI settled all four. For a corpus that requires
verbatim capture, a frame is the only honest source for on-screen text.

Two things about *which* URL ffmpeg is handed, both measured rather than
guessed:

**Take the HLS variant, not DASH.** Both were 1080p60 on the video this was
worked out on. Range-requesting the DASH URL (format 299, protocol `https`) was
throttled and timed out twice at over four minutes for a twenty second section.
The HLS variant (format 312, protocol `m3u8_native`) returned the same twenty
seconds in about one second, because the requested section maps onto whole
segments. Selection here is by *protocol*: 312 was the right id on that video,
and format ids belong to YouTube rather than to us.

**ffmpeg reads the HLS manifest directly**, so there is no intermediate clip
file, nothing to clean up, and nothing left half-written by a Ctrl-C.

Whether HLS is offered at all depends on the yt-dlp version, which is worth
knowing before you conclude a video has none: 2026.07.04 returned 27 formats
with no HLS among them for a video that 2026.08.19 returned 47 for, HLS
included. See `_no_hls`.

This can never cost you a transcript. Everything here raises and the caller
reports and carries on: a missing ffmpeg, a video with no HLS variant, and a
phrase that isn't in the transcript all leave the document intact.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import NamedTuple

# Seconds between frames inside a --frame-window. The CLI carries the same
# default, and a test asserts the two agree.
STEP = 2

# A phrase like "the" matches every paragraph in the video. The cap is not a
# performance guard: 200 near-identical PNGs in an output directory is a mess
# someone has to clear by hand, and the twelfth frame has never been the one
# that answered the question.
MOST_MATCHES = 12

# ffmpeg over HLS took about a second for a twenty second section when measured.
# Anything past this is either the DASH-style throttle happening to HLS too or a
# dead socket, and waiting longer has not helped in either case.
TIMEOUT = 180


class NotAvailable(LookupError):
    """ffmpeg isn't on PATH. Nothing else here can work without it."""


class Ask(NamedTuple):
    """One `--frame-at` value, after being read as a time or as a phrase.

    `matches` is None when the value parsed as a timestamp, because nothing was
    searched. Zero means the search ran and found nothing, which is a different
    answer, and the caller has to be able to tell the two apart.
    """
    value: str
    seconds: tuple[int, ...]
    matches: int | None
    note: str


def available() -> bool:
    return shutil.which("ffmpeg") is not None


def seconds_at(value: str) -> int | None:
    """Read a `--frame-at` value as a timestamp: `279`, `4:39`, `1:10:41`.

    Returns None for anything else, which is how the caller learns to try the
    phrase reading instead. None rather than an exception, because "not a
    timestamp" is an ordinary expected answer for this argument, not a fault.

    A field after the first must be under 60, so `1:70` is not quietly read as
    130 seconds: it falls through, gets searched for as text, and the caller
    then reports that it matched nothing. Guessing what a malformed clock meant
    is how a frame ends up saved under a second nobody asked for.

    The timestamp reading wins, so a bare number cannot be searched for as a
    phrase. That is the right way round: `279` is overwhelmingly a moment.
    """
    parts = (value or "").strip().split(":")
    if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    if any(int(p) >= 60 for p in parts[1:]):
        return None
    total = 0
    for p in parts:
        total = total * 60 + int(p)
    return total


def matching(paragraphs: list[dict], phrase: str) -> list[int]:
    """`start_ms` of every paragraph containing `phrase`, case-insensitively.

    Whitespace is collapsed on both sides before comparing, so a two-word phrase
    still matches across whatever line break a paragraph happened to be built
    with. This is the workflow the flag exists for: grep the transcript, then go
    and look at that moment.
    """
    needle = re.sub(r"\s+", " ", (phrase or "")).strip().lower()
    if not needle:
        return []
    hits = []
    for p in paragraphs or []:
        text = re.sub(r"\s+", " ", str(p.get("text") or "")).lower()
        if needle in text:
            hits.append(int(p.get("start_ms") or 0))
    return hits


def resolve(values: list[str], paragraphs: list[dict],
            limit: int = MOST_MATCHES) -> list[Ask]:
    """Turn each `--frame-at` value into the seconds to capture, plus a note.

    The note is written for stderr and is empty when there is nothing to say. A
    phrase that matched nothing gets one saying so in those words: otherwise a
    search that could not succeed and a search that legitimately found nothing
    return the same silence, and telling those two apart is the reason the
    consumer of this tool is a corpus rather than a notes folder.
    """
    asks = []
    for value in values or []:
        at = seconds_at(value)
        if at is not None:
            asks.append(Ask(value, (at,), None, ""))
            continue
        hits = matching(paragraphs, value)
        if not hits:
            asks.append(Ask(value, (), 0,
                            f'no paragraph contains "{value}", so it named no '
                            f'moment and no frame was taken '
                            f'({len(paragraphs or [])} paragraphs searched)'))
            continue
        taken = sorted({ms // 1000 for ms in hits})[:limit]
        note = f'"{value}" matched {len(hits)} paragraphs'
        if len(hits) > len(taken):
            note += f", taking the first {len(taken)}"
        asks.append(Ask(value, tuple(taken), len(hits), note))
    return asks


def hls_stream(info: dict) -> str:
    """The manifest URL of the best HLS video variant in a yt-dlp info dict.

    By protocol, never by format id: see the module docstring for what the DASH
    alternative costs. 1080 is preferred outright, and below it the tallest on
    offer wins. Height is the whole point here, since the job is reading text
    off the screen and a 360p frame cannot be read.
    """
    hls = [f for f in (info.get("formats") or [])
           if isinstance(f, dict)
           and str(f.get("protocol") or "").startswith("m3u8")
           and (f.get("vcodec") or "none") != "none"
           and f.get("url")]
    if not hls:
        raise LookupError(_no_hls(info))
    best = max(hls, key=lambda f: ((f.get("height") or 0) == 1080,
                                   f.get("height") or 0,
                                   f.get("fps") or 0))
    return best["url"]


def _no_hls(info: dict) -> str:
    """Why no HLS came back, which is usually not "this video has none".

    A stale yt-dlp is the likeliest cause by a distance, and it presents
    identically to a video that genuinely has none: a formats list with nothing
    m3u8 in it. Measured on one video, same machine, same day, yt-dlp 2026.07.04
    returned 27 formats and not one HLS among them where 2026.08.19 returned 47
    including the 1080p60 variant. YouTube reaches HLS through player clients an
    out-of-date extractor stops asking for, so this will recur, and the message
    says the version rather than making someone bisect it a second time. The
    `yt-dlp -F` line is there because the CLI and the library are separate
    installs that drift to different versions on one machine, which is exactly
    how this was found.
    """
    try:
        import yt_dlp

        version = yt_dlp.version.__version__
    except Exception:                    # a yt-dlp too broken to ask is its own answer
        version = "unknown"
    n = len(info.get("formats") or [])
    return (f"no HLS format came back for this video: yt-dlp {version} offered "
            f"{n} formats and none of them m3u8. Upgrade yt-dlp before believing "
            f"the video has none, and check `yt-dlp -F` against the version this "
            f"is importing. DASH is not used instead: range-requesting it was "
            f"throttled and timed out twice at over four minutes for a twenty "
            f"second section, which is the reason this module exists")


def stream_for(url: str, proxy: str | None = None,
               cookies: str | None = None) -> str:
    """Probe the video and pick its HLS manifest.

    transkrp is imported here rather than at the top so the two modules can go
    on importing each other lazily. Cookies matter on the probe for the same
    reason they do for captions, an age gate refuses before there is anything to
    fetch at all; the stream URL that comes back is already signed, so ffmpeg
    needs neither them nor the proxy that fetched them.
    """
    import transkrp

    return hls_stream(transkrp.probe(url, proxy, cookies))


def filename(base: str, second: int, index: int | None = None) -> str:
    """`<base>-t<seconds>.png`, numbered `-01`, `-02` inside a window.

    `base` is the transcript's own `slug(title, video_id)`, so the video id sits
    in the frame's name too. That is what makes a frame re-pullable: the name
    alone says which video and which second to go back to, the same property a
    paragraph gets from its timestamp. The `-t` keeps it clear of the
    transcript's own filename.
    """
    if index is None:
        return f"{base}-t{second}.png"
    return f"{base}-t{second}-{index:02d}.png"


def _pattern(base: str, second: int) -> str:
    """The same name as an ffmpeg output pattern, for a window of frames."""
    return f"{base}-t{second}-%02d.png"


def capture(stream: str, second: int, out_dir: str, base: str,
            window: int = 0, step: int = STEP,
            proxy: str | None = None) -> list[str]:
    """Write one frame at `second`, or a span of them across `window` seconds.

    Returns the paths that exist afterwards. Inside a window, `-01` is at
    `second` and each next one is `step` later. A window can come back a frame
    short of the arithmetic, at the end of a video or from where the filter
    lands: the frames that exist are the frames there were, and that is not an
    error. Measured on a 20 second clip, a 6 second window at step 2 gives three.

    `-ss` goes before `-i` so ffmpeg seeks the input rather than decoding from
    zero and throwing an hour of frames away.
    """
    if not available():
        raise NotAvailable(
            "--frame-at needs ffmpeg on PATH (https://ffmpeg.org/download.html); "
            "the transcript was written, the frames were not")
    step = max(1, int(step))
    window = max(0, int(window))
    second = int(second)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           # -y, plus the closed stdin below: without both, an existing file has
           # ffmpeg ask about overwriting on the terminal, and rerunning the same
           # command then hangs with nothing on screen to say why.
           "-y", "-ss", str(second), "-i", stream]
    if window:
        # Two spare indices: fps rounding can land one frame past the
        # arithmetic, and a path that was never written is dropped below.
        wanted = [os.path.join(out_dir, filename(base, second, i))
                  for i in range(1, window // step + 3)]
        cmd += ["-t", str(window), "-vf", f"fps=1/{step}", "-q:v", "2",
                os.path.join(out_dir, _pattern(base, second))]
    else:
        wanted = [os.path.join(out_dir, filename(base, second))]
        cmd += ["-frames:v", "1", "-q:v", "2", wanted[0]]

    env = dict(os.environ)
    if proxy:
        # ffmpeg has no single flag covering http and https both; its protocols
        # read these, and YouTube blocks datacenter IPs for media the same way it
        # does for metadata.
        env["http_proxy"] = env["https_proxy"] = proxy
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT,
                              encoding="utf-8", errors="replace",
                              env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        raise LookupError(f"ffmpeg gave up after {TIMEOUT}s at t={second}") from e
    except OSError as e:
        raise NotAvailable(f"could not run ffmpeg: {e}") from e
    if done.returncode != 0:
        err = (done.stderr or "").strip().splitlines()
        raise LookupError(f"ffmpeg exited {done.returncode} at t={second}: "
                          f"{err[-1] if err else 'no output'}")

    made = [p for p in wanted if os.path.exists(p)]
    if not made:
        # A clean exit that wrote nothing means the seek landed past the end of
        # the video. Silence here would read as success.
        raise LookupError(f"ffmpeg wrote no frame at t={second}; "
                          f"is that past the end of the video?")
    return made
