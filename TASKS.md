# Tasks

Everything outstanding is here. `CHANGELOG.md` records what has **landed**; this file
records what has **not**. Rules and procedure are in `CLAUDE.md` and `CONTRIBUTING.md`
and are not restated.

## Where this is

A large series making **PMID a first-class identifier** is on local `main` and is
**not pushed**. `git log afe806c..HEAD` is the whole of it.

Three adversarial review lenses ran over every round — retraction, false alarms, and a
sabotage pass that reverts each new line to see whether any test goes red. Every finding
was real and reproduced against live NCBI, Crossref and the Retraction Watch export.

**The bar for pushing is not that the lenses find nothing.** Lenses told to find fault
always find something, so the rule is narrower: nothing may remain that **fails a correct
bibliography, passes a wrong one, or states something untrue**. What is left below is
recorded rather than blocking, and the one entry that fails a correct bibliography is
there because no rule separates the two readings — not because it was judged unimportant.

Where the series got to, measured live rather than asserted: an invented co-author
appended to a byline went from never firing to 1,177 of 1,177; a dropped first author
from 104 missed in 2,551 to 824 of 824; a forename substitution from never caught to
98.9%, at a measured false-alarm cost of 0.077%; mis-decoded creator values from 5 of 8
repaired to 8 of 8 with no false repair in 364,925; and the false-alarm rate on correct
PMID-path bibliographies from 13.7% to zero on the last 1,177-entry sweep.

## Open

- [ ] **URGENT, before anything else — audit `CLAUDE.md`, `CONTRIBUTING.md` and
      `.claude/`.** These are what an agent reads before it touches the verdict path, so
      a wrong or duplicated rule there produces wrong work everywhere else. They grew by
      accretion across the PMID series and no longer hold together.

      **One fact, two homes, already drifted.** `CONTRIBUTING.md`'s "The three rules, in
      short" restates `CLAUDE.md`'s "The three rules that shape every decision", and its
      "Adding a check" restates `CLAUDE.md`'s — with three requirements `CLAUDE.md` does
      not carry. Compared verbatim, `CONTRIBUTING.md` alone demands the
      `docs/registry-artifacts.md` write-up, a test proving the *true* positive still
      fires, and `docs/verdicts.md` beside `README.md` for a new verdict. A contributor
      following `CLAUDE.md` — the file the repository presents as the rules it is held
      to — ships a rule with no write-up and one-directional tests, and
      `tests/test_benign.py::TestRuleScoping` then fails for a reason neither list
      explains. The write-up requirement is the one that matters most, because
      `CLAUDE.md` elsewhere calls that file "the reader's only way to challenge a
      `REGISTRY-ARTIFACT` line".

      **Sections added one at a time without asking where they belong.** The fixture
      provenance rules went into `CLAUDE.md`'s Tests section, the isolated-tree
      procedure and the definition of done into `CONTRIBUTING.md`, the comment rule into
      `CLAUDE.md` beside Git — each defensible alone, none placed against a stated split
      between the two files. Decide what that split is: `CLAUDE.md` is loaded every turn
      and should hold only what must hold every turn; `CONTRIBUTING.md` is read once by
      somebody about to change something.

      **`.claude/settings.json` allows five commands** — `uv run`, `uv sync`, `uv tool`,
      `python3 -m pytest`, and three read-only `git` verbs. Everything else an agent does
      here, including `git add`, `git commit`, `grep`, `find` and scratch work under
      `/private/tmp`, falls through to a prompt each time. Widen it to what
      `CONTRIBUTING.md` already prescribes, or say why not.

      **What is asserted and what is checked.** `tests/test_fixture_provenance.py` and
      `TestRuleScoping` gate two of these rules; the rest are prose that rotted
      undetected until a human audit found it. Decide which of the remaining claims can
      be pinned mechanically, and pin those.

- [ ] **A comma-less MEDLINE `FAU` is read as the abbreviated form, and five real
      bylines fail a correct bibliography because of it.** `_parse_au_fallback` takes
      the last token as the initials block, so `<LastName>`-only values — which NLM
      writes when the publisher deposited no forename and no initials — lose their last
      word to an invented forename. Measured over 36,568 `FAU` values: 23 are
      comma-less, and `de LAVERGNE`, `Xiaodong Lv`, `Hung Nguyen`, `Editorial Board Of
      Radiology` and `The Lancet Child Adolescent Health` all become
      `authors/mismatch` at error severity against bibliographies that spell them
      right. Reproduces on `10.1016/j.rxeng.2025.101663`, `10.1016/s2352-4642(23)00169-4`
      and `10.1016/j.plaphy.2024.109034`.

      **No shape rule separates the two readings, and one was tried and reverted.**
      Both occur and both are real: `Okano J` is Okano, J.; `Ho Yi` is Ho, Y.I.
      (`"Ho Yi"[au]` answers 14 citations); `S DMTS` is surname `S` with initials
      `DMTS`; `Xiaodong Lv` and `Hung Nguyen` are given-then-surname. `Yi` and `Lv`
      are both two characters and both title-case and need opposite readings, so
      "short and capitalised" refuses `Ho Yi` and `S DMTS` while accepting the five.
      Tuning the bound to fit is how the wrong reading shipped in the first place.

      The discriminator is whether NLM's XML carried a `<ForeName>`, and
      `efetch rettype=medline` does not expose it. So this is a decision, not an edit:
      fetch `rettype=xml` for the comma-less values, or treat a comma-less `FAU` as
      an uncertain reading and decline to report an author mismatch on it. The docstring
      at `_parse_fau` and `docs/registry-artifacts.md` both argue from `AU` agreement
      that the abbreviated reading is "the same string's own reading" — that premise is
      true of all five failing values too, so the inference has to go with the fix.
- [ ] **`Reference.arxiv` is declared and populated by no adapter**, though
      `identifier` ranks it. An arXiv preprint's identifier is silently dropped. Wire it
      or remove it; do not leave it in the middle.
- [ ] **Push to `main`.** The secret scan is done and clean — no keys, no credential
      files, no private paths, and nothing personal in the diff. The only email
      addresses are corresponding authors inside MEDLINE fixtures, which are verbatim
      `efetch` output and already public in PubMed.

## The rule this series keeps breaking

**Every behaviour needs a test that bites.** Round after round shipped correct code
whose test stayed green when the code was reverted — the `_CORRECTION_KINDS` set, the
`_LEADING_ARTICLE` anchor, the whole `_alternate_containers` restructure, and the
comma-less `FAU` reading above, which shipped with tests covering only the four values
it happened to get right. Before a commit, revert each line added and confirm something
goes red.

**And the shape of the mistake underneath it.** The container-title rules were fixed one
shape at a time over four rounds — leading article, then society expansion, then the
parenthetical qualifier, then the same qualifier on the abbreviation — and the byline
rules over five. Each fix was real and measured; the scope was reactive. When a review
finds a defect in a rule, the question to answer before writing anything is what the
complete set of cases is, not what the reported case needs.

## Decisions taken, not to be reopened

- **The parenthetical qualifier is stripped from the registry side only**, with an exact
  fold-equal on the remainder. It is safe because the work is already pinned by DOI,
  PMID, or `confirm_without_id`'s title-author-year bar before `container` is compared,
  so only one serial is ever in play. The "dropping it would merge two serials" argument
  is a catalogue concern that does not apply here.
- **A rule must be expressible without naming a single journal.** A journal list in the
  code is the failure this test exists to catch.
- **Severity, not recency, decides among Retraction Watch rows.** A later Correction
  never downgrades an earlier Retraction.
