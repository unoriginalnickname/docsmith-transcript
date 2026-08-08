# 0019. A recurring one-word name is reported, and a human resolves it

- Status: Accepted
- Amends [0017](0017-three-tiers-of-link-strength.md)
- Date: 2026-08-09

## Context

[ADR 0016](0016-obsidian-is-the-graph-viewer.md) scoped bare given names to their
own recording, because forty transcripts contain several unrelated Maxes and
merging them on the string invents a hub joining things nobody joins. That was
right, and it is a **containment, not a diagnosis**. It is also silent.

[ADR 0017](0017-three-tiers-of-link-strength.md) then reported that tiering by
recurrence was useless on this corpus: "only 4 people appear in more than one
video, and **no edge joins two of them**. A hierarchy built on it would have an
empty top tier."

That measurement was wrong, and wrong in an instructive way. It was not a fact
about the corpus, it was a fact about the containment. The corpus's most-cited
figure writes under a one-word handle, so he was filed as **five strangers**:

```
Swix   author of a blog post that coined the AI engineer role
Swix   person who coined a term Jason uses for his main thread
swix   AI writer who defined AI agents
swix   AI Engineer Summit organizer who invited the speaker
swix   creator of the AI News aggregator newsletter
```

Five dim, unresolved, mutually disconnected nodes. Nothing in the output said so.
A reader saw a corpus where almost nobody recurs, which is exactly what a corpus
where the recurring person is scoped away looks like.

Thirty-five of the hundred and four people were one-word names.

## Decision

**`merge()` emits an `ambiguous` list: folded one-word names appearing in two or
more videos, with their per-video roles.** This is
[ADR 0013](0013-a-degraded-graph-must-not-pass-for-a-finished-one.md) applied to
identity — a degraded graph must not pass for a finished one. It needs no
configuration, and it asserts nothing: these *may* be one person. The roles are
carried because they are what makes the judgement possible; "AI Engineer Summit
organizer" and "creator of the AI News newsletter" are recognisably one man in a
way that two bare instances of "swix" are not.

A one-word name in a *single* video is not reported. "Barry" identifies someone
perfectly well inside his own recording, and listing all thirty would bury the
two collisions that matter.

**A corpus-level `aliases.json` resolves them, and it is hand-written.** There is
no in-corpus evidence that "swix" is one person: `nameparser` ships no list of
given names, and the corpus metadata never spells him out. Telling a handle from
a bare given name takes world knowledge, and this project's answer to world
knowledge ([ADR 0012](0012-validate-the-model-against-a-domain-model.md)) is a
human-supplied domain model enforced deterministically — a file somebody wrote,
not a guess somebody generated.

**Applied at `merge()`, never at `extract()`.** Identity is a corpus-level
decision, and resolving it per-video would bake one run's answer into
`.graph-parts.jsonl`. That file is the expensive half — forty model calls —
while merging is free. Editing an alias and re-running must cost nothing, so the
parts are read and never rewritten.

**An alias clears `local` only if what it maps to is a full name.** `local` is
what keeps someone out of a person note and dim on the graph. A real name earns
its way out of that; mapping one first name to another leaves the same problem
and must not pretend otherwise.

**Edge endpoints are rewritten too.** Otherwise the person gets a node under
their real name and keeps their connections under the handle — a hub with
nothing attached, which is worse than the split it replaced.

**`discarded` carries every dropped edge with its quote.** `rejected` and
`irrelevant` were bare counts, and a count is a number nobody can check; between
them they dropped 21% of candidate edges on the first full run. Whether that
filter is working or eating good data is not answerable from a total.

## Consequences

Measured on the same corpus, adding one line of alias:

| | before | after |
|---|---|---|
| people | 104 | 100 |
| Shawn Wang | 5 separate dim nodes | 1 node, 5 videos, 6 edges |
| most-recurring figure | Dex Horthy, 3 videos | Shawn Wang, 5 videos |
| one-word names | 35 | 30 |
| person notes in the vault | 69 | 70 |

**This supersedes 0017's claim that the recurring tier is empty.** It was empty
because identity was broken, and the `person/recurring` colour group that record
described as an honest-but-weak label now has the corpus's central figure in it.

The report shrinks as it is answered, so what remains is what is open — one name
on this corpus, "Ben", who appears in two videos as "AI Engineering Summit team
member" and "host introducing speakers at the AI Engineer World's Fair" and who
nobody has yet identified. That is the correct end state for an unresolved one:
visible, unmerged, and waiting on a person.

`discarded` only appears for parts extracted after this change. A cached
`.graph-parts.jsonl` predates the field, and re-merging must not need forty model
calls to become valid again — so an absent list means "not recorded", not "none".
