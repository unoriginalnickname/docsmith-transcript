"""The people graph — offline. Never shells out.

The load-bearing test here is the evidence check. Everything else in this module
shapes data; that one decides whether a claim about two real people gets written
down. An edge is a much stronger statement than a transcript line, and it is
inferred from text where speech recognition has already mangled the names, so a
citation that does not resolve is worse than no citation — it looks like proof.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import graph
import ontology as ont
import speakers


@pytest.fixture(autouse=True)
def never_really_run(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("a test tried to run the real claude CLI")
    monkeypatch.setattr(subprocess, "run", forbidden)


@pytest.fixture
def answers(monkeypatch):
    """Queue what the model replies, and capture what it was asked."""
    def install(payload):
        seen = []
        monkeypatch.setattr(speakers, "_run",
                            lambda prompt, model=None: seen.append(prompt) or
                            (payload if isinstance(payload, str) else json.dumps(payload)))
        return seen
    return install


TALK = """\
---
title: Building a Chess Coach — Anant Dole
channel: AI Engineer
about: Anant Dole and Asbjorn Steinskog on chess engines.
url: https://www.youtube.com/watch?v=abcdefghijk
speakers: Anant Dole, Asbjorn Steinskog
---

# Building a Chess Coach

## Intro

[00:14](https://www.youtube.com/watch?v=abcdefghijk&t=14s) **Anant Dole**: this \
is where myself and my colleague Asbjorn currently work at Play Magnus

[01:30](https://www.youtube.com/watch?v=abcdefghijk&t=90s) he also founded a \
company and we joined later on
"""


@pytest.fixture
def talk(tmp_path):
    path = tmp_path / "chess.md"
    path.write_text(TALK, encoding="utf-8")
    return graph.parse_markdown(str(path))


# --------------------------------------------------------------------------
# reading our own markdown back
# --------------------------------------------------------------------------

def test_frontmatter_and_paragraphs_round_trip(talk):
    assert talk["title"] == "Building a Chess Coach — Anant Dole"
    assert talk["channel"] == "AI Engineer"
    assert talk["speakers"] == ["Anant Dole", "Asbjorn Steinskog"]
    assert len(talk["paragraphs"]) == 2
    assert talk["paragraphs"][0]["timestamp"] == "00:14"


def test_the_speaker_label_is_not_read_as_transcript_text(talk):
    """"**Anant Dole**: " is presentation. Leaving it in would let a name be
    "quoted" as evidence for a claim about the person saying it."""
    assert not talk["paragraphs"][0]["text"].startswith("**")
    assert "Anant Dole" not in talk["paragraphs"][0]["text"]


def test_timestamp_links_survive(talk):
    assert talk["paragraphs"][1]["url"].endswith("&t=90s")


def test_a_heading_is_not_a_paragraph(talk):
    assert not any(p["text"].startswith("#") for p in talk["paragraphs"])


# --------------------------------------------------------------------------
# the evidence check
# --------------------------------------------------------------------------

def test_a_real_quote_is_accepted(talk):
    assert graph.quote_is_real("myself and my colleague Asbjorn currently work",
                               talk["text"])


def test_punctuation_and_case_do_not_fail_an_honest_quote(talk):
    assert graph.quote_is_real("Myself and my colleague Asbjorn, currently work!",
                               talk["text"])


def test_an_invented_quote_is_refused(talk):
    """The failure this exists for: a fluent, plausible sentence nobody said."""
    assert not graph.quote_is_real(
        "Anant and Asbjorn founded the company together in 2019", talk["text"])


def test_a_paraphrase_is_refused(talk):
    """Close is not the same. A paraphrase cannot be checked against audio."""
    assert not graph.quote_is_real("myself and my colleague Asbjorn work there now",
                                   talk["text"])


def test_words_must_be_contiguous(talk):
    """Scattered words that all appear somewhere are not a quotation."""
    assert not graph.quote_is_real("chess coach myself company founded later",
                                   talk["text"])


@pytest.mark.parametrize("short", ["work at", "he also", "", "myself"])
def test_a_quote_too_short_to_prove_anything(short, talk):
    assert not graph.quote_is_real(short, talk["text"])


# --------------------------------------------------------------------------
# turning a reply into edges
# --------------------------------------------------------------------------

def edge(**over):
    base = {"from": "Anant Dole", "to": "Asbjorn Steinskog", "kind": "worked_with",
            "evidence": "myself and my colleague Asbjorn currently work",
            "timestamp": "00:14", "confidence": "high"}
    base.update(over)
    return base


def test_a_supported_edge_survives_with_its_provenance(answers, talk):
    answers({"people": [], "edges": [edge()]})
    got = graph.extract(talk)
    assert len(got["edges"]) == 1
    e = got["edges"][0]
    assert (e["from"], e["to"], e["kind"]) == ("Anant Dole", "Asbjorn Steinskog",
                                              "worked_with")
    assert e["url"].endswith("&t=14s")     # resolves to the second of video
    assert got["rejected"] == 0


def test_an_unsupported_edge_is_discarded_not_demoted(answers, talk):
    """Discarded, deliberately. A claim about two real people with a citation
    that does not resolve is worse than no claim: it reads as proof."""
    answers({"people": [], "edges": [edge(evidence="they founded it together")]})
    got = graph.extract(talk)
    assert got["edges"] == [] and got["rejected"] == 1


def test_an_unknown_relationship_kind_is_dropped(answers, talk):
    """A closed set, so the graph doesn't fragment into near-synonyms."""
    answers({"people": [], "edges": [edge(kind="is_associated_with")]})
    assert graph.extract(talk)["edges"] == []


def test_a_self_edge_is_dropped(answers, talk):
    answers({"people": [], "edges": [edge(to="Anant Dole")]})
    assert graph.extract(talk)["edges"] == []


def test_a_possessive_is_stripped_from_a_name(answers, talk):
    """Found in real output: "Magnus Carlsen's" arrived as a person, and would
    have become a second node for someone the graph already had."""
    answers({"people": [{"name": "Magnus Carlsen's", "role": "player"}], "edges": []})
    assert graph.extract(talk)["people"][0]["name"] == "Magnus Carlsen"


def test_a_garbled_reply_yields_an_empty_graph(answers, talk):
    answers("sorry, I can't help with that")
    got = graph.extract(talk)
    assert got["people"] == [] and got["edges"] == []


def test_the_corpus_is_offered_to_the_extractor(answers, talk):
    seen = answers({"people": [], "edges": []})
    graph.extract(talk, corpus={"tom oneill": "Tom O'Neill"})
    assert "Tom O'Neill" in seen[0]


# --------------------------------------------------------------------------
# merging many videos into one graph
# --------------------------------------------------------------------------

def result(people, video_id, edges=()):
    return {"people": [{"name": n, "role": "", "local": not ont.is_full_name(n)}
                       for n in people],
            "edges": list(edges), "rejected": 0, "video_id": video_id}


def test_one_person_is_one_node_across_videos():
    g = graph.merge([result(["Anant Dole"], "vid1"), result(["Anant Dole"], "vid2")])
    assert len(g["people"]) == 1
    assert g["people"][0]["videos"] == 2


def test_weight_counts_videos_not_mentions():
    """Appearing in five talks is what makes someone a recurring figure. Being
    named five times in one talk does not."""
    g = graph.merge([result(["Anant Dole", "Anant Dole", "Anant Dole"], "vid1")])
    assert g["people"][0]["videos"] == 1


def test_bare_given_names_do_not_merge_across_videos():
    """Forty talks contain several unrelated Maxes. Merging them on the string
    would invent a hub connecting things no one person connects."""
    g = graph.merge([result(["Max"], "vid1"), result(["Max"], "vid2")])
    assert len([p for p in g["people"] if p["name"] == "Max"]) == 2


def test_full_names_still_merge_across_videos():
    g = graph.merge([result(["Max Planck"], "vid1"), result(["Max Planck"], "vid2")])
    assert len(g["people"]) == 1


def test_the_fullest_spelling_wins():
    g = graph.merge([result(["Daniel Sheehan"], "v1"),
                     result(["Daniel Peter Sheehan"], "v2")])
    assert g["people"][0]["name"] == "Daniel Peter Sheehan"


def test_people_are_ordered_by_reach():
    g = graph.merge([result(["A Person", "B Person"], "v1"),
                     result(["A Person"], "v2")])
    assert [p["name"] for p in g["people"]] == ["A Person", "B Person"]


def test_rejections_are_totalled_across_the_corpus():
    """A model that starts inventing citations should be visible, not just
    quietly produce a smaller graph."""
    a, b = result([], "v1"), result([], "v2")
    a["rejected"], b["rejected"] = 2, 3
    assert graph.merge([a, b])["rejected"] == 5


def test_an_empty_corpus_is_not_a_crash():
    assert graph.merge([]) == {"people": [], "edges": [], "rejected": 0,
                               "irrelevant": 0, "ambiguous": [], "discarded": [],
                               "failed": []}


# --------------------------------------------------------------------------
# identity: saying when a one-word name is doing the work of several, and
# letting a human settle it
# --------------------------------------------------------------------------

def cast(people, video_id, video="", edges=()):
    """Like `result`, but each person is (name, role)."""
    return {"people": [{"name": n, "role": r, "local": not ont.is_full_name(n)}
                       for n, r in people],
            "edges": list(edges), "rejected": 0,
            "video": video or video_id, "video_id": video_id}


def test_a_one_word_name_in_two_videos_is_reported_with_its_roles():
    """Scoping a bare given name is containment, not diagnosis, and it is
    silent. Nobody writes the alias for a collision they were never told about.
    """
    g = graph.merge([
        cast([("swix", "author of the blog post that coined AI engineer")], "v1", "Talk A"),
        cast([("Swix", "AI Engineer Summit organizer")], "v2", "Talk B"),
    ])
    assert len(g["ambiguous"]) == 1
    report = g["ambiguous"][0]
    assert report["name"] == "Swix"  # the fullest spelling seen
    assert report["videos"] == 2
    assert {s["video"] for s in report["seen"]} == {"Talk A", "Talk B"}
    assert ["author of the blog post that coined AI engineer"] in \
           [s["roles"] for s in report["seen"]]


def test_a_one_word_name_in_one_video_is_not_ambiguous():
    """"Barry" identifies someone perfectly well inside his own recording.
    Reporting him would make the list unreadable and the real collisions
    invisible."""
    assert graph.merge([cast([("Barry", "in the audience")], "v1")])["ambiguous"] == []


def test_a_full_name_across_videos_is_not_ambiguous():
    """It merged correctly. There is nothing for a human to decide."""
    g = graph.merge([cast([("Dex Horthy", "")], "v1"), cast([("Dex Horthy", "")], "v2")])
    assert g["people"][0]["videos"] == 2 and g["ambiguous"] == []


def test_the_report_ranks_the_worst_collision_first():
    parts = [cast([("swix", "")], f"v{i}") for i in range(4)]
    parts += [cast([("Ben", "")], f"w{i}") for i in range(2)]
    names = [a["name"] for a in graph.merge(parts)["ambiguous"]]
    assert names == ["swix", "Ben"]


def test_an_alias_collapses_the_scattered_nodes_into_one():
    """The payoff: five strangers become the corpus's most-cited figure."""
    parts = [cast([("swix", f"role {i}")], f"v{i}") for i in range(5)]
    g = graph.merge(parts, {"swix": "Shawn Wang"})

    assert [p["name"] for p in g["people"]] == ["Shawn Wang"]
    assert g["people"][0]["videos"] == 5
    assert len(g["people"][0]["roles"]) == 5


def test_an_aliased_person_stops_being_local():
    """`local` is what keeps someone out of a note and dim on the graph. A real
    name identifies them across the corpus, which is the whole point."""
    g = graph.merge([cast([("swix", "")], "v1")], {"swix": "Shawn Wang"})
    assert g["people"][0]["local"] is False


def test_an_alias_to_another_bare_name_stays_local():
    """An alias supplies world knowledge; it does not get to invent identity.
    Mapping one first name to another leaves the same problem."""
    g = graph.merge([cast([("swix", "")], "v1")], {"swix": "Shawn"})
    assert g["people"][0]["local"] is True


def test_an_alias_rewrites_the_edges_too():
    """Otherwise the person gets a node under their real name and keeps their
    connections under the handle, which is a hub with nothing attached."""
    part = cast([("swix", "")], "v1",
                edges=[{"from": "Barry Zhang", "to": "swix", "kind": "cites"}])
    g = graph.merge([part], {"swix": "Shawn Wang"})
    assert g["edges"][0]["to"] == "Shawn Wang"
    assert g["edges"][0]["from"] == "Barry Zhang"


def test_aliasing_is_insensitive_to_the_spelling_that_was_captured():
    g = graph.merge([cast([("Swix", "")], "v1"), cast([("swix", "")], "v2")],
                    {"SWIX": "Shawn Wang"})
    assert len(g["people"]) == 1 and g["people"][0]["videos"] == 2


def test_an_alias_takes_the_name_off_the_ambiguous_list():
    """The report shrinks as it is answered, so what is left is what is open."""
    parts = [cast([("swix", "")], "v1"), cast([("swix", "")], "v2"),
             cast([("Ben", "")], "v3"), cast([("Ben", "")], "v4")]
    g = graph.merge(parts, {"swix": "Shawn Wang"})
    assert [a["name"] for a in g["ambiguous"]] == ["Ben"]


def test_merging_never_rewrites_the_parts_it_was_given():
    """`.graph-parts.jsonl` is the expensive half — forty model calls. Merging
    is free precisely because it does not touch it, which is why aliases are
    applied here and not in extract()."""
    part = cast([("swix", "")], "v1",
                edges=[{"from": "swix", "to": "Barry Zhang", "kind": "cites"}])
    before = json.dumps(part, sort_keys=True)
    graph.merge([part], {"swix": "Shawn Wang"})
    assert json.dumps(part, sort_keys=True) == before


def test_no_aliases_file_changes_nothing():
    parts = [cast([("swix", "")], "v1"), cast([("swix", "")], "v2")]
    assert graph.merge(parts) == graph.merge(parts, {})


def test_an_empty_alias_target_is_ignored_rather_than_erasing_a_name():
    g = graph.merge([cast([("swix", "")], "v1")], {"swix": "  "})
    assert g["people"][0]["name"] == "swix"


# --------------------------------------------------------------------------
# what the filters threw away
# --------------------------------------------------------------------------

def test_discards_are_collected_across_the_corpus():
    """`rejected` and `irrelevant` are bare counts, and a count is a number
    nobody can check. Between them they dropped 21% of the candidate edges on
    the first full run."""
    a, b = result([], "v1"), result([], "v2")
    a["discarded"] = [{"from": "X", "to": "Y", "kind": "cites", "evidence": "q",
                       "why": "quote does not occur in the transcript",
                       "video": "Talk A"}]
    b["discarded"] = [{"from": "P", "to": "Q", "kind": "cites", "evidence": "r",
                       "why": "quote names neither party", "video": "Talk B"}]
    g = graph.merge([a, b])
    assert len(g["discarded"]) == 2
    assert {d["why"] for d in g["discarded"]} == {
        "quote does not occur in the transcript", "quote names neither party"}


def test_parts_written_before_discards_were_recorded_still_merge():
    """A cached `.graph-parts.jsonl` predates this field. Re-merging must not
    need forty model calls to become valid again."""
    assert graph.merge([result([], "v1")])["discarded"] == []


def test_a_discarded_edge_carries_the_quote_that_failed(answers, talk):
    """The quote is the whole evidence. A discard log without it says only that
    something was thrown away, not whether throwing it away was right."""
    answers({"people": [{"name": "Anant Dole", "role": "speaker"}],
             "edges": [edge(**{"from": "Anant Dole", "to": "Asbjorn Steinskog",
                               "evidence": "a sentence that is not in the talk"})]})
    out = graph.extract(talk)
    assert out["edges"] == []
    assert len(out["discarded"]) == 1
    assert out["discarded"][0]["evidence"] == "a sentence that is not in the talk"
    assert out["discarded"][0]["why"] == "quote does not occur in the transcript"


# --------------------------------------------------------------------------
# a genuine quote attached to the wrong claim
# --------------------------------------------------------------------------

def test_a_quote_naming_neither_party_is_refused(answers, talk):
    """The limitation quote_is_real does not cover, found in real output:
    `Ali Howard --interviewed--> Dex Horthy` cited by a true sentence about
    Dax Raad. Genuine provenance, irrelevant to the claim."""
    answers({"people": [], "edges": [edge(
        **{"from": "Ali Howard", "to": "Dex Horthy",
           "evidence": "he also founded a company and we joined later on"})]})
    got = graph.extract(talk)
    assert got["edges"] == [] and got["irrelevant"] == 1
    assert got["rejected"] == 0        # the quote was real; it just wasn't about them


def test_a_quote_naming_one_party_is_enough(answers, talk):
    """The other side is often a pronoun — "Dex covered this in his talk" —
    so demanding both names would reject sound edges."""
    answers({"people": [], "edges": [edge(evidence="my colleague Asbjorn currently work at")]})
    assert len(graph.extract(talk)["edges"]) == 1


def test_a_misheard_name_still_counts_as_named():
    """Transcripts say "Dex Horty" for "Dex Horthy"; that is still a mention."""
    assert graph.quote_supports("thanks to uh to Dex Horty", "Sam Bhagwat", "Dex Horthy")


def test_the_two_kinds_of_bad_evidence_are_counted_apart():
    """A model inventing citations and a model misfiling real ones are different
    problems with different fixes, so they are not summed together."""
    assert graph.quote_supports("nothing about anyone here", "A Person", "B Person") is False


# --------------------------------------------------------------------------
# the channel is an organisation, not a person
# --------------------------------------------------------------------------

def test_the_channel_does_not_become_a_person(answers, talk):
    """Left in, it was the graph's biggest hub — 14 of 107 edges hung off "AI
    Engineer", asserting a conference had *interviewed* its own speakers."""
    talk["channel"] = "AI Engineer"
    answers({"people": [{"name": "AI Engineer", "role": "conference"},
                        {"name": "Anant Dole", "role": "speaker"}], "edges": []})
    names = [p["name"] for p in graph.extract(talk)["people"]]
    assert names == ["Anant Dole"]


def test_edges_to_the_channel_are_dropped(answers, talk):
    talk["channel"] = "AI Engineer"
    answers({"people": [], "edges": [edge(**{"from": "AI Engineer"})]})
    assert graph.extract(talk)["edges"] == []


# --------------------------------------------------------------------------
# a degraded run must not look like a completed one
# --------------------------------------------------------------------------

def test_a_cli_failure_is_marked_not_swallowed(answers, talk, monkeypatch):
    """The bug this caught on a real 40-video run: ten consecutive transient
    failures were reported as ten findings of "no people in this talk". An
    error and an empty result are not the same thing and must not look alike.
    """
    monkeypatch.setattr(speakers, "_run",
                        lambda *a, **k: (_ for _ in ()).throw(LookupError("claude exited 1")))
    got = graph.extract(talk)
    assert got["failed"]
    assert got["people"] == []


def test_no_people_in_a_talk_that_names_a_speaker_is_a_failure(answers, talk):
    """A talk whose own frontmatter names a speaker cannot truthfully contain
    nobody. Empty here is a failure wearing a result's clothes."""
    answers({"people": [], "edges": []})
    assert graph.extract(talk)["failed"]


def test_no_people_in_a_talk_that_names_nobody_is_just_empty(answers, talk):
    talk["speakers"] = []
    answers({"people": [], "edges": []})
    assert "failed" not in graph.extract(talk)


def test_failures_are_surfaced_in_the_merged_graph():
    """A graph missing a quarter of its corpus should say so, not quietly be
    smaller."""
    bad = result([], "v1")
    bad["failed"] = "claude exited 1"
    bad["video"] = "A Talk"
    g = graph.merge([result(["A Person"], "v2"), bad])
    assert g["failed"] == [{"video": "A Talk", "why": "claude exited 1"}]


def test_the_graph_is_json_safe(tmp_path):
    g = graph.merge([result(["A Person"], "v1", [edge()])])
    graph.save(g, str(tmp_path / "g.json"))
    assert json.loads((tmp_path / "g.json").read_text(encoding="utf-8"))["people"]
