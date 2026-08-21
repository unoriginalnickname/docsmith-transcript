"""Frame capture, offline. Never runs a real ffmpeg and never touches YouTube.

What's covered is the part that decides things: whether a `--frame-at` value is
a clock or a phrase, which of the offered formats gets handed to ffmpeg, and
whether a phrase that isn't in the transcript says so. That last one is the
point of the feature, not a nicety, so it is tested as behaviour rather than as
a message: a search that could not succeed has to look different from one that
ran and found nothing.

The ffmpeg tests stub `subprocess.run` and assert on the command line built.
That is worth doing here rather than dismissing as testing-the-mock, because the
two things that cost real time on this feature were both arguments: which URL,
and `-ss` before `-i` rather than after.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_transkrp import ev, stub_video

import frames
import transkrp as tk


@pytest.fixture(autouse=True)
def never_really_run(monkeypatch):
    """Belt and braces: an un-stubbed test fails loudly instead of shelling out."""
    def forbidden(*a, **k):
        raise AssertionError("a test tried to run the real ffmpeg")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(frames.shutil, "which", lambda name: "/usr/bin/ffmpeg")


def para(start_ms, text):
    return {"start_ms": start_ms, "timestamp": tk.stamp(start_ms),
            "turn": 0, "text": text}


def fmt(**over):
    f = {"format_id": "x", "protocol": "https", "vcodec": "avc1", "height": 720,
         "fps": 30, "url": "http://x"}
    f.update(over)
    return f


# --------------------------------------------------------------------------
# is it a clock or a phrase
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value,want", [
    ("279", 279),
    ("0", 0),
    ("4:39", 279),
    ("04:39", 279),
    ("1:10:41", 4241),
    ("0:00", 0),
    ("1:5", 65),
])
def test_timestamps_are_read_as_timestamps(value, want):
    assert frames.seconds_at(value) == want


@pytest.mark.parametrize("value", [
    "Valor",           # the ordinary case: a word to search for
    "Fervor Aegis",
    "",
    "4.5",             # not integer seconds, so not a clock
    "-3",
    "1:2:3:4",         # too many fields to be a runtime
    "1:70",            # 70 seconds in a minute field is malformed, not 130s
    "1:10:99",
    " ",
    "t=412",
])
def test_anything_else_falls_through_to_the_phrase_reading(value):
    """None, not an exception. The same argument legitimately takes a phrase."""
    assert frames.seconds_at(value) is None


def test_a_malformed_clock_is_searched_for_rather_than_guessed_at():
    """`1:70` could mean 130 seconds. Guessing is how a frame gets filed under
    a second nobody asked for, so it goes down the phrase road and reports."""
    asks = frames.resolve(["1:70"], [para(0, "nothing like it here")])
    assert asks[0].seconds == ()
    assert asks[0].matches == 0


# --------------------------------------------------------------------------
# finding the moment in the transcript
# --------------------------------------------------------------------------

PARAS = [
    para(0, "Welcome back to the channel."),
    para(30_000, "The Fervor node is what everyone asks about."),
    para(279_000, "Fervid is a different thing entirely."),
    para(600_000, "and fervor again, lowercase this time"),
]


def test_a_phrase_finds_every_paragraph_that_contains_it():
    assert frames.matching(PARAS, "Fervor") == [30_000, 600_000]


def test_matching_ignores_case():
    assert frames.matching(PARAS, "FERVID") == [279_000]


def test_matching_survives_the_whitespace_a_paragraph_was_built_with():
    """A phrase typed with a plain space still matches text broken across
    lines, which is how paragraphs arrive from a caption feed."""
    paras = [para(0, "the Fervor\n  node")]
    assert frames.matching(paras, "Fervor node") == [0]


def test_a_phrase_nobody_said_matches_nothing():
    assert frames.matching(PARAS, "Aegis") == []


def test_matching_returns_start_ms_not_paragraph_numbers():
    """The moment is what gets captured, so it has to be the timestamp."""
    assert frames.matching(PARAS, "different thing") == [279_000]


# --------------------------------------------------------------------------
# resolve: what the caller is told, and how it tells silence apart
# --------------------------------------------------------------------------

def test_a_timestamp_reports_no_match_count_because_nothing_was_searched():
    ask, = frames.resolve(["4:39"], PARAS)
    assert ask.seconds == (279,) and ask.matches is None and ask.note == ""


def test_a_phrase_that_matched_nothing_is_not_the_same_as_no_frames():
    """The distinction the corpus turns on. A search that could not succeed
    reports zero matches and says so; a timestamp reports None."""
    missing, found = frames.resolve(["Aegis", "Fervid"], PARAS)
    assert missing.matches == 0 and missing.seconds == ()
    assert "no paragraph contains" in missing.note and "Aegis" in missing.note
    assert found.matches == 1 and found.seconds == (279,)


def test_the_note_for_a_missing_phrase_says_how_much_was_searched():
    ask, = frames.resolve(["Aegis"], PARAS)
    assert f"{len(PARAS)} paragraphs searched" in ask.note


def test_seconds_come_out_sorted_and_deduplicated():
    """Two paragraphs inside the same second want one frame, not two."""
    paras = [para(5_100, "Fervor"), para(5_800, "Fervor"), para(1_000, "Fervor")]
    ask, = frames.resolve(["Fervor"], paras)
    assert ask.seconds == (1, 5)


def test_a_common_word_is_capped_rather_than_writing_two_hundred_images():
    paras = [para(i * 10_000, "the thing") for i in range(200)]
    ask, = frames.resolve(["the"], paras)
    assert len(ask.seconds) == frames.MOST_MATCHES
    assert "matched 200 paragraphs" in ask.note
    assert f"taking the first {frames.MOST_MATCHES}" in ask.note


def test_an_uncapped_match_does_not_claim_to_have_taken_the_first_anything():
    ask, = frames.resolve(["Fervor"], PARAS)
    assert ask.note == '"Fervor" matched 2 paragraphs'


def test_resolve_keeps_the_order_the_flags_were_given_in():
    asks = frames.resolve(["Fervid", "0:30"], PARAS)
    assert [a.value for a in asks] == ["Fervid", "0:30"]


# --------------------------------------------------------------------------
# which format ffmpeg is handed
# --------------------------------------------------------------------------

def test_hls_is_taken_over_dash_at_the_same_height():
    """The measured reason for this module's existence: range-requesting the
    DASH URL timed out twice at over four minutes for a twenty second section,
    where the HLS variant took about one second."""
    info = {"formats": [
        fmt(format_id="299", protocol="https", height=1080, url="http://dash"),
        fmt(format_id="312", protocol="m3u8_native", height=1080, url="http://hls"),
    ]}
    assert frames.hls_stream(info) == "http://hls"


def test_selection_is_by_protocol_not_by_format_id():
    """312 was the right id on one video. Ids are YouTube's to change."""
    info = {"formats": [fmt(format_id="99999", protocol="m3u8", height=1080,
                            url="http://hls")]}
    assert frames.hls_stream(info) == "http://hls"


def test_1080_wins_over_a_taller_variant():
    info = {"formats": [
        fmt(protocol="m3u8_native", height=1440, url="http://tall"),
        fmt(protocol="m3u8_native", height=1080, url="http://hls"),
        fmt(protocol="m3u8_native", height=720, url="http://small"),
    ]}
    assert frames.hls_stream(info) == "http://hls"


def test_below_1080_the_tallest_wins_because_the_job_is_reading_text():
    info = {"formats": [
        fmt(protocol="m3u8_native", height=360, url="http://tiny"),
        fmt(protocol="m3u8_native", height=720, url="http://best"),
    ]}
    assert frames.hls_stream(info) == "http://best"


def test_an_audio_only_hls_stream_is_not_a_video_format():
    info = {"formats": [
        fmt(protocol="m3u8_native", vcodec="none", height=None, url="http://audio"),
        fmt(protocol="m3u8_native", height=480, url="http://video"),
    ]}
    assert frames.hls_stream(info) == "http://video"


def test_a_format_with_no_url_is_not_usable():
    info = {"formats": [fmt(protocol="m3u8_native", height=1080, url=None),
                        fmt(protocol="m3u8_native", height=480, url="http://v")]}
    assert frames.hls_stream(info) == "http://v"


def test_no_hls_at_all_says_so_rather_than_falling_back_to_dash():
    """Falling back would work and would then hang for four minutes."""
    info = {"formats": [fmt(protocol="https", height=1080, url="http://dash")]}
    with pytest.raises(LookupError, match="no HLS"):
        frames.hls_stream(info)


def test_a_video_with_no_formats_at_all_is_an_error_not_a_crash():
    with pytest.raises(LookupError):
        frames.hls_stream({})


def test_no_hls_blames_the_extractor_before_the_video():
    """The failure this actually shipped with. A stale yt-dlp returns a formats
    list with no m3u8 in it, which is indistinguishable from a video that has
    none, and the first real run hit exactly that: 2026.07.04 offered 27 DASH
    formats for a video 2026.08.19 offered 47 for, HLS included. So the message
    names the version and says to upgrade, rather than asserting something about
    the video that is probably false."""
    info = {"formats": [fmt(protocol="https") for _ in range(27)]}
    with pytest.raises(LookupError) as caught:
        frames.hls_stream(info)
    said = str(caught.value)
    assert "yt-dlp" in said and "Upgrade" in said
    assert "27 formats" in said          # what it was given, so the report is checkable


def test_no_hls_says_why_dash_is_not_used_instead():
    """The obvious fix is a DASH fallback. It would work and would then hang."""
    with pytest.raises(LookupError, match="throttled"):
        frames.hls_stream({"formats": [fmt(protocol="https")]})


# --------------------------------------------------------------------------
# what the files are called
# --------------------------------------------------------------------------

def test_a_frame_is_named_for_the_video_and_the_second():
    """The id inside the slug is what makes a frame re-pullable: the filename
    alone says which video and which moment to go back to."""
    base = tk.slug("Fervor and Fervid explained", "vid12345678")
    assert frames.filename(base, 279) == \
        "fervor-and-fervid-explained-vid12345678-t279.png"


def test_a_frame_cannot_collide_with_the_transcript_it_sits_beside():
    base = tk.slug("T", "vid12345678")
    assert frames.filename(base, 0) != f"{base}.md"
    assert frames.filename(base, 0).startswith(base + "-t")


def test_a_window_numbers_its_frames_in_order():
    base = tk.slug("T", "vid12345678")
    names = [frames.filename(base, 279, i) for i in (1, 2, 10)]
    assert names == [f"{base}-t279-01.png", f"{base}-t279-02.png",
                     f"{base}-t279-10.png"]
    assert sorted(names) == names  # zero-padded, so a directory listing reads right


def test_two_moments_in_one_video_get_different_names():
    base = tk.slug("T", "vid12345678")
    assert frames.filename(base, 279) != frames.filename(base, 4241)


# --------------------------------------------------------------------------
# the ffmpeg call itself
# --------------------------------------------------------------------------

@pytest.fixture
def ffmpeg(monkeypatch):
    """Record the command line, and write whatever it says it will write."""
    calls = []

    def install(returncode=0, stderr="", writes=True):
        def fake_run(cmd, **kw):
            calls.append({"cmd": cmd, "kw": kw})
            out = Path(cmd[-1])
            if writes and returncode == 0:
                if "%02d" in out.name:
                    for i in (1, 2, 3):
                        out.with_name(out.name.replace("%02d", f"{i:02d}")).write_bytes(b"png")
                else:
                    out.write_bytes(b"png")
            return subprocess.CompletedProcess(cmd, returncode, "", stderr)
        monkeypatch.setattr(subprocess, "run", fake_run)
        return calls
    return install


def test_one_frame_seeks_before_the_input(ffmpeg, tmp_path):
    """`-ss` after `-i` decodes from zero and throws an hour of frames away."""
    calls = ffmpeg()
    frames.capture("http://hls/m3u8", 279, str(tmp_path), "base")
    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("-ss") + 1] == "279"
    assert cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("-i") + 1] == "http://hls/m3u8"
    assert "-frames:v" in cmd and cmd[cmd.index("-frames:v") + 1] == "1"


def test_capture_returns_the_file_it_wrote(ffmpeg, tmp_path):
    ffmpeg()
    made = frames.capture("http://hls", 279, str(tmp_path), "base")
    assert made == [str(tmp_path / "base-t279.png")]
    assert Path(made[0]).exists()


def test_an_existing_frame_is_overwritten_rather_than_prompted_about(ffmpeg, tmp_path):
    """Without -y and a closed stdin, ffmpeg asks on the terminal and a rerun
    of the same command hangs with nothing on screen to say why."""
    calls = ffmpeg()
    frames.capture("http://hls", 1, str(tmp_path), "base")
    assert "-y" in calls[0]["cmd"]
    assert calls[0]["kw"]["stdin"] is subprocess.DEVNULL


def test_a_window_asks_for_a_span_of_frames(ffmpeg, tmp_path):
    calls = ffmpeg()
    made = frames.capture("http://hls", 279, str(tmp_path), "base",
                          window=6, step=2)
    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("-t") + 1] == "6"
    assert cmd[cmd.index("-vf") + 1] == "fps=1/2"
    assert cmd[-1].endswith("base-t279-%02d.png")
    assert [Path(p).name for p in made] == ["base-t279-01.png", "base-t279-02.png",
                                            "base-t279-03.png"]


def test_a_zero_step_cannot_divide_by_zero(ffmpeg, tmp_path):
    calls = ffmpeg()
    frames.capture("http://hls", 1, str(tmp_path), "base", window=4, step=0)
    assert calls[0]["cmd"][calls[0]["cmd"].index("-vf") + 1] == "fps=1/1"


def test_a_proxy_reaches_ffmpeg_through_the_environment(ffmpeg, tmp_path):
    """ffmpeg has no single flag covering http and https both."""
    calls = ffmpeg()
    frames.capture("http://hls", 1, str(tmp_path), "base", proxy="http://p:8080")
    env = calls[0]["kw"]["env"]
    assert env["http_proxy"] == env["https_proxy"] == "http://p:8080"


def test_no_proxy_leaves_the_environment_alone(ffmpeg, tmp_path):
    import os
    calls = ffmpeg()
    frames.capture("http://hls", 1, str(tmp_path), "base")
    assert "http_proxy" not in {k for k in calls[0]["kw"]["env"]} - set(os.environ)


def test_a_failing_ffmpeg_reports_its_last_line(ffmpeg, tmp_path):
    ffmpeg(returncode=1, stderr="Opening failed\nServer returned 403 Forbidden")
    with pytest.raises(LookupError, match="403 Forbidden"):
        frames.capture("http://hls", 1, str(tmp_path), "base")


def test_a_clean_exit_that_wrote_nothing_is_still_a_failure(ffmpeg, tmp_path):
    """A seek past the end of the video exits 0. Silence would read as success."""
    ffmpeg(writes=False)
    with pytest.raises(LookupError, match="past the end"):
        frames.capture("http://hls", 99_999, str(tmp_path), "base")


def test_a_timeout_says_how_long_it_waited(monkeypatch, tmp_path):
    def timed_out(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, frames.TIMEOUT)
    monkeypatch.setattr(subprocess, "run", timed_out)
    with pytest.raises(LookupError, match=str(frames.TIMEOUT)):
        frames.capture("http://hls", 1, str(tmp_path), "base")


def test_a_missing_ffmpeg_says_the_transcript_was_still_written(monkeypatch, tmp_path):
    monkeypatch.setattr(frames.shutil, "which", lambda name: None)
    assert not frames.available()
    with pytest.raises(frames.NotAvailable, match="ffmpeg"):
        frames.capture("http://hls", 1, str(tmp_path), "base")


# --------------------------------------------------------------------------
# the CLI's side. Still offline: the transcript is stubbed the way
# test_transkrp stubs it, and stream_for never gets to probe anything.
# --------------------------------------------------------------------------

@pytest.fixture
def cli_args(monkeypatch):
    """Run main() with a stubbed video, and hand back the parsed arguments."""
    seen = []
    monkeypatch.setattr(tk, "_frames",
                        lambda t, url, args, state: seen.append(args))

    def run(argv):
        stub_video(monkeypatch, [ev(0, 1000, "The Fervor node.")])
        assert tk.main(argv) == 0
        return seen
    return run


def test_frame_at_is_repeatable_and_off_by_default(cli_args, tmp_path):
    assert cli_args(["http://x", "-o", str(tmp_path / "t.md")]) == []


def test_frame_at_collects_every_value_in_order(cli_args, tmp_path):
    args, = cli_args(["http://x", "-o", str(tmp_path / "t.md"),
                      "--frame-at", "4:39", "--frame-at", "Valor"])
    assert args.frame_at == ["4:39", "Valor"]


def test_the_cli_step_default_matches_the_library_one(cli_args, tmp_path):
    """Two places hold this number, because frames is imported lazily inside
    _frames and argparse cannot reach the constant."""
    args, = cli_args(["http://x", "-o", str(tmp_path / "t.md"),
                      "--frame-at", "0:01"])
    assert args.frame_step == frames.STEP
    assert args.frame_window == 0  # one frame unless a window is asked for


@pytest.fixture
def stub_stream(monkeypatch):
    """Neither probe nor ffmpeg. Just the two things _frames calls out to."""
    def install(stream="http://hls"):
        monkeypatch.setattr(frames, "stream_for",
                            lambda url, proxy=None, cookies=None: stream)
    return install


def test_a_frame_lands_beside_its_transcript(monkeypatch, tmp_path, ffmpeg,
                                             stub_stream):
    stub_video(monkeypatch, [ev(0, 1000, "Hello.")], title="My Talk")
    stub_stream()
    ffmpeg()
    monkeypatch.chdir(tmp_path)
    assert tk.main(["http://x", "--frame-at", "4:39"]) == 0
    assert (tmp_path / "my-talk-vid12345678.md").exists()
    assert (tmp_path / "my-talk-vid12345678-t279.png").exists()


def test_a_phrase_that_is_not_in_the_transcript_still_leaves_the_transcript(
        monkeypatch, tmp_path, capsys, stub_stream):
    """The transcript is the product; a frame is an addition. A failed search
    reaches the exit code so a script can tell, and costs nothing else."""
    stub_video(monkeypatch, [ev(0, 1000, "Hello.")], title="My Talk")
    stub_stream()
    monkeypatch.chdir(tmp_path)
    assert tk.main(["http://x", "--frame-at", "Aegis"]) == 1
    assert (tmp_path / "my-talk-vid12345678.md").exists()
    assert "no paragraph contains" in capsys.readouterr().err


def test_a_missing_ffmpeg_costs_the_frames_and_nothing_else(
        monkeypatch, tmp_path, capsys, stub_stream):
    stub_video(monkeypatch, [ev(0, 1000, "Hello.")], title="My Talk")
    stub_stream()
    monkeypatch.setattr(frames.shutil, "which", lambda name: None)
    monkeypatch.chdir(tmp_path)
    assert tk.main(["http://x", "--frame-at", "4:39"]) == 1
    assert (tmp_path / "my-talk-vid12345678.md").exists()
    err = capsys.readouterr().err
    assert "ffmpeg" in err and "the transcript was written" in err


def test_frames_go_to_the_working_directory_when_the_document_goes_to_stdout(
        monkeypatch, tmp_path, capsys, ffmpeg, stub_stream):
    """A PNG cannot be piped out with the markdown, so say where it went."""
    stub_video(monkeypatch, [ev(0, 1000, "Hello.")], title="My Talk")
    stub_stream()
    ffmpeg()
    monkeypatch.chdir(tmp_path)
    assert tk.main(["http://x", "-o", "-", "--frame-at", "0:01"]) == 0
    out = capsys.readouterr()
    assert out.out.startswith("---")  # the document, undisturbed
    assert "frames cannot go to stdout" in out.err
    assert (tmp_path / "my-talk-vid12345678-t1.png").exists()
