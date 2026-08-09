"""The run engine: order, failure, and telling the truth about both.

Two properties this file exists to protect.

**Fetches stay serial.** The limit that shapes this whole tool is a few hundred
caption pulls an hour *per IP*, so two runs must queue behind each other rather
than race. A UI is exactly the thing that invites someone to start four playlists
at once, and the moment the engine lets them it becomes worse than the CLI.

**A stopped run says so, and says how to resume.** ADR 0009's bargain — stop on
a rate limit instead of hammering, because `--skip-existing` makes picking it
back up free — has to survive the move away from a terminal, where the person
can no longer see the stderr line that explained it.

Everything is injected, so none of this touches the network. ADR 0010.
"""

import time

import pytest

import jobs
import transkrp


def make_transcript(url="https://www.youtube.com/watch?v=abcdefghijk",
                    title="A talk", vid="abcdefghijk", paras=3, **extra):
    """The shape `transcript()` returns, with only what the engine reads."""
    t = {
        "channel": "A channel", "upload_date": "2026-01-01", "description": "",
        "chapters": [], "title": title, "video_id": vid, "url": url,
        "source": "manual", "lang": "en", "translated": False,
        "punctuated": True, "duration_ms": 754000, "captions_end_ms": 754000,
        "turns": 1,
        "paragraphs": [
            {"start_ms": i * 60000, "timestamp": transkrp.stamp(i * 60000),
             "turn": 0, "text": f"Paragraph {i}."} for i in range(paras)
        ],
        "text": " ".join(f"Paragraph {i}." for i in range(paras)),
    }
    t.update(extra)
    return t


class Harness:
    """A Runner with every side effect replaced by something observable."""

    def __init__(self, tmp_path, **over):
        self.fetched = []       # every URL actually fetched, in order
        self.written = {}       # path -> document
        self.slept = 0
        self.raise_for = {}     # url -> exception to raise instead of fetching
        self.expansions = {}    # url -> the URLs it becomes
        self.have = set()       # URLs pretending to be already written
        self.before_fetch = None
        self.runner = jobs.Runner(
            str(tmp_path),
            fetch=self._fetch, expand=self._expand, write=self._write,
            existing=self._existing, sleep=self._sleep,
            attribute=lambda t, m, p, prog: prog("speakers: fake"),
            **over)

    def _fetch(self, url, lang, proxy, words, cookies, progress=None, **kw):
        if self.before_fetch:
            self.before_fetch(url)
        self.fetched.append(url)
        if progress:
            progress("fetching the caption track")
        if url in self.raise_for:
            raise self.raise_for[url]
        t = make_transcript(url=url, title=f"Talk {len(self.fetched)}",
                            vid=(transkrp.video_id(url) or "x" * 11))
        if kw.get("segments_too"):
            t["segments"] = [{"start_ms": 0, "end_ms": 900, "text": "Hello."},
                             {"start_ms": 900, "end_ms": 1800, "text": "Again."}]
        return t

    def _expand(self, url, proxy, cookies, force_playlist):
        if url in self.expansions:
            return self.expansions[url]
        if isinstance(self.expansions.get("_raise"), Exception):
            raise self.expansions["_raise"]
        return [url]

    def _write(self, path, doc):
        self.written[path] = doc

    def _existing(self, url, out_dir, explicit, ext):
        return f"/already/{transkrp.video_id(url)}.{ext}" if url in self.have else None

    def _sleep(self, seconds):
        self.slept += 1

    def run(self, urls, **options):
        return self.runner.submit(urls, options)

    def wait(self, run, timeout=5.0):
        """Block until the run reaches a state it will not leave on its own."""
        done = ("done", "failed", "cancelled", "stopped")
        deadline = time.time() + timeout
        while time.time() < deadline:
            snap = self.runner.snapshot(run.id)
            if snap["status"] in done:
                return snap
            time.sleep(0.01)
        raise AssertionError(f"run stuck in {self.runner.snapshot(run.id)['status']}")


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


# -- the ordinary path ------------------------------------------------------

def test_a_run_fetches_writes_and_reports(h, tmp_path):
    snap = h.wait(h.run(["https://www.youtube.com/watch?v=abcdefghijk"]))

    assert snap["status"] == "done"
    assert snap["counts"] == {"total": 1, "pending": 0, "running": 0,
                              "done": 1, "failed": 0, "skipped": 0}
    item = snap["items"][0]
    assert item["status"] == "done"
    assert item["title"] == "Talk 1"
    assert item["stats"]["paragraphs"] == 3
    assert item["stats"]["unit"] == "paragraphs"
    assert item["path"].endswith("talk-1-abcdefghijk.md")
    assert h.written[item["path"]].startswith("---")


def test_progress_is_visible_while_the_fetch_is_still_running(tmp_path):
    """A whisper run is minutes of CPU with nothing to show for it. Whatever
    `transcript()` reports has to be readable *during* the call, not after."""
    caught = {}
    harness = Harness(tmp_path)

    def slow(url, lang, proxy, words, cookies, progress=None, **kw):
        progress("transcribing 12:30 of audio")
        seen = harness.runner.snapshot(run.id)["items"][0]
        caught.update(message=seen["message"], status=seen["status"])
        return make_transcript(url=url)

    harness.runner._fetch = slow
    run = harness.run(["https://www.youtube.com/watch?v=abcdefghijk"])
    snap = harness.wait(run)

    assert caught == {"message": "transcribing 12:30 of audio", "status": "running"}
    # And cleared once it's done: a finished row shows what it produced, not the
    # last thing it was doing.
    assert snap["items"][0]["message"] == ""


def test_a_playlist_becomes_its_entries_in_order(h):
    h.expansions["playlist"] = [f"https://youtu.be/{c * 11}" for c in "abc"]
    snap = h.wait(h.run(["playlist"]))

    assert [i["index"] for i in snap["items"]] == [0, 1, 2]
    assert h.fetched == h.expansions["playlist"]
    assert snap["requested"] == ["playlist"]


def test_the_format_decides_what_gets_counted(h):
    """A .srt holds cues, not paragraphs. Reporting paragraphs would be a lie."""
    snap = h.wait(h.run(["https://youtu.be/abcdefghijk"], format="srt"))
    stats = snap["items"][0]["stats"]
    assert stats["unit"] == "cues" and stats["cues"] == 2
    assert snap["items"][0]["path"].endswith(".srt")


# -- serial, because the limiter is per-IP ----------------------------------

def test_two_runs_queue_rather_than_race(h):
    """The property the whole engine exists for."""
    first = h.run([f"https://youtu.be/{c * 11}" for c in "ab"])
    second = h.run([f"https://youtu.be/{c * 11}" for c in "cd"])
    h.wait(first)
    h.wait(second)
    assert h.fetched == ["https://youtu.be/aaaaaaaaaaa", "https://youtu.be/bbbbbbbbbbb",
                         "https://youtu.be/ccccccccccc", "https://youtu.be/ddddddddddd"]


def test_fetches_are_paced_but_skips_are_not(h):
    """Pace the requests, not the work being stepped over. ADR 0009."""
    h.have = {"https://youtu.be/bbbbbbbbbbb"}
    h.wait(h.run([f"https://youtu.be/{c * 11}" for c in "abc"], skip_existing=True))
    # Three items, two fetched, and only the second fetch waits.
    assert len(h.fetched) == 2
    assert h.slept == 1


# -- failure ----------------------------------------------------------------

def test_a_rate_limit_stops_the_run_and_says_how_to_resume(h):
    urls = [f"https://youtu.be/{c * 11}" for c in "abc"]
    h.raise_for[urls[1]] = transkrp.RateLimited("YouTube is rate-limiting this IP")
    snap = h.wait(h.run(urls))

    assert snap["status"] == "stopped"
    assert [i["status"] for i in snap["items"]] == ["done", "failed", "pending"]
    assert snap["items"][1]["error"]["kind"] == "rate_limited"
    assert h.fetched == urls[:2]  # the third was never attempted
    note = " ".join(snap["notes"])
    assert "1 of 3 not fetched" in note
    assert "resumes for free" in note


def test_one_bad_video_does_not_stop_the_rest(h):
    urls = [f"https://youtu.be/{c * 11}" for c in "abc"]
    h.raise_for[urls[0]] = transkrp.Unavailable("video is private")
    h.raise_for[urls[1]] = transkrp.NoCaptions("no English captions")
    snap = h.wait(h.run(urls))

    assert snap["status"] == "done"
    assert [i["error"]["kind"] if i["error"] else None for i in snap["items"]] == [
        "unavailable", "no_captions", None]
    assert snap["counts"]["failed"] == 2 and snap["counts"]["done"] == 1


def test_a_failed_write_is_its_own_kind(h):
    def refuse(path, doc):
        raise OSError(13, "Permission denied")

    h.runner._write = refuse
    snap = h.wait(h.run(["https://youtu.be/abcdefghijk"]))
    assert snap["items"][0]["error"]["kind"] == "write_failed"


def test_a_playlist_that_cannot_be_read_fails_the_run(h):
    h.expansions["_raise"] = transkrp.Unavailable("playlist is private")
    snap = h.wait(h.run(["https://www.youtube.com/playlist?list=X"]))
    assert snap["status"] == "failed"
    assert snap["error"]["kind"] == "unavailable"
    assert snap["items"] == []


def test_a_bug_in_the_worker_does_not_take_the_worker_down(h):
    """One run exploding must not silently stop every run after it."""
    def boom(url, out_dir, explicit, ext):
        raise RuntimeError("something unexpected")

    h.runner._existing = boom
    broken = h.wait(h.run(["https://youtu.be/aaaaaaaaaaa"], skip_existing=True))
    assert broken["status"] == "failed" and broken["error"]["kind"] == "internal"

    h.runner._existing = h._existing
    assert h.wait(h.run(["https://youtu.be/bbbbbbbbbbb"]))["status"] == "done"


# -- skipping and stopping ---------------------------------------------------

def test_skip_existing_costs_no_request(h):
    """Matched on the id in the filename, before spending the call that would
    be rate-limited."""
    h.have = {"https://youtu.be/abcdefghijk"}
    snap = h.wait(h.run(["https://youtu.be/abcdefghijk"], skip_existing=True))
    assert snap["items"][0]["status"] == "skipped"
    assert "already have" in snap["items"][0]["message"]
    # And how to get it anyway, naming the control that is actually on screen.
    # A skip and a refusal look identical from the outside, and someone who has
    # just ticked a new option meant the latter.
    assert "untick 'skip what I already have'" in snap["items"][0]["message"]
    assert h.fetched == []


def test_force_refetches_what_skip_existing_would_have_kept(h):
    h.have = {"https://youtu.be/abcdefghijk"}
    h.wait(h.run(["https://youtu.be/abcdefghijk"], skip_existing=True, force=True))
    assert h.fetched == ["https://youtu.be/abcdefghijk"]


def test_cancelling_a_queued_run_fetches_nothing(h):
    blocker = h.run([f"https://youtu.be/{c * 11}" for c in "ab"])
    queued = h.run(["https://youtu.be/zzzzzzzzzzz"])
    h.runner.cancel(queued.id)
    h.wait(blocker)
    assert h.runner.snapshot(queued.id)["status"] == "cancelled"
    assert "https://youtu.be/zzzzzzzzzzz" not in h.fetched


def test_cancelling_a_running_run_stops_the_queue_not_the_fetch(tmp_path):
    """Honest about which: there is no way to interrupt a whisper run mid-call."""
    harness = Harness(tmp_path)
    urls = [f"https://youtu.be/{c * 11}" for c in "abc"]
    run = harness.run(urls)
    harness.before_fetch = lambda url: harness.runner.cancel(run.id)

    snap = harness.wait(run)
    assert snap["status"] == "cancelled"
    # The one in flight when cancel arrived still finished; nothing after it ran.
    assert harness.fetched == urls[:1]
    assert [i["status"] for i in snap["items"]] == ["done", "pending", "pending"]


# -- what the page is told ---------------------------------------------------

def test_an_ambiguous_url_says_which_reading_it_took(h):
    """ADR 0008's rule, carried off stderr: asking for 40 and getting 1 without
    finding out why is the failure this prevents."""
    snap = h.wait(h.run(["https://www.youtube.com/watch?v=abcdefghijk&list=PL123"]))
    assert any("one video inside a playlist" in n for n in snap["notes"])


def test_asking_for_the_playlist_drops_the_note(h):
    h.expansions["https://www.youtube.com/watch?v=abcdefghijk&list=PL123"] = [
        "https://youtu.be/aaaaaaaaaaa"]
    snap = h.wait(h.run(["https://www.youtube.com/watch?v=abcdefghijk&list=PL123"],
                        playlist=True))
    assert snap["notes"] == []


def test_the_snapshot_has_every_field_on_every_item(h):
    """A TypeScript client is coming. Absent-means-pending would push the null
    checks into the consumer. ADR 0018."""
    run = h.run([f"https://youtu.be/{c * 11}" for c in "ab"])
    h.wait(run)
    for item in h.runner.snapshot(run.id)["items"]:
        assert set(item) == {"index", "url", "status", "title", "video_id",
                             "message", "path", "error", "stats", "has_document"}
    assert set(h.runner.snapshot(run.id)) == {
        "id", "status", "created_at", "out_dir", "options", "requested",
        "notes", "error", "counts", "items"}


def test_the_transcript_itself_never_rides_in_the_snapshot(h):
    """It is the one field that could be a megabyte, and it has its own route."""
    run = h.run(["https://youtu.be/abcdefghijk"])
    h.wait(run)
    item = h.runner.snapshot(run.id)["items"][0]
    assert item["has_document"] is True
    assert "transcript" not in item and "paragraphs" not in item


# -- reading a result back ---------------------------------------------------

def test_preview_anchors_resolve_to_the_second(h):
    """The link in the page is the link in the file. A citation that doesn't
    resolve is the one thing this project is most against."""
    run = h.run(["https://www.youtube.com/watch?v=abcdefghijk"])
    h.wait(run)
    preview = h.runner.preview(run.id, 0)
    assert [p["anchor"] for p in preview["paragraphs"]] == [
        "https://www.youtube.com/watch?v=abcdefghijk&t=0s",
        "https://www.youtube.com/watch?v=abcdefghijk&t=60s",
        "https://www.youtube.com/watch?v=abcdefghijk&t=120s"]
    assert preview["paragraphs"][0]["timestamp"] == "00:00"


def test_preview_carries_how_sure_an_attribution_was(tmp_path):
    """A guess must not render like a quote. ADR 0012, at the presentation layer."""
    harness = Harness(tmp_path)
    labelled = make_transcript()
    labelled["paragraphs"][0]["speaker"] = "Dex Horthy"
    labelled["paragraphs"][0]["speaker_confidence"] = "low"
    harness.runner._fetch = lambda url, *a, **kw: labelled

    run = harness.run(["https://www.youtube.com/watch?v=abcdefghijk"])
    harness.wait(run)
    paras = harness.runner.preview(run.id, 0)["paragraphs"]
    assert paras[0]["speaker"] == "Dex Horthy"
    assert paras[0]["speaker_confidence"] == "low"
    assert paras[1]["speaker"] == "" and paras[1]["speaker_confidence"] == ""


def test_document_renders_the_format_that_was_asked_for(h):
    run = h.run(["https://youtu.be/abcdefghijk"])
    h.wait(run)
    assert h.runner.document(run.id, 0).startswith("---")
    assert h.runner.document(run.id, 0, "json").startswith("{")
    # Fetched as prose, so the cues were never asked for and cannot be invented.
    assert h.runner.document(run.id, 0, "srt") is None


def test_nothing_is_readable_before_it_is_fetched(h):
    run = h.run([f"https://youtu.be/{c * 11}" for c in "ab"])
    assert h.runner.document(run.id, 99) is None
    assert h.runner.preview("no-such-run", 0) is None
    h.wait(run)


# -- options come off the wire, so they are not trusted ----------------------

def test_unknown_options_are_dropped_not_forwarded():
    """These become keyword arguments to a function that fetches URLs."""
    opts = jobs._clean_options({"format": "md", "proxy": "http://evil",
                                "cookies": "chrome", "out_dir": "/etc"})
    assert set(opts) == set(jobs.DEFAULTS)


def test_an_unknown_format_falls_back_rather_than_reaching_render():
    assert jobs._clean_options({"format": "../../etc/passwd"})["format"] == "md"


def test_a_whisper_model_must_be_one_of_the_published_sizes():
    """faster-whisper takes a *path* here too, which is the problem."""
    assert jobs._clean_options({"whisper_model": "/etc"})["whisper_model"] == "small"
    assert jobs._clean_options({"whisper_model": "large-v3"})["whisper_model"] == "large-v3"


def test_option_types_are_coerced_not_trusted():
    opts = jobs._clean_options({"playlist": "yes", "words": "250", "lang": 7})
    assert opts["playlist"] is True and opts["words"] == 250 and opts["lang"] == "7"
    assert jobs._clean_options({"words": "not a number"})["words"] == 110
    assert jobs._clean_options({"words": -5})["words"] == 1
