# Official source review inventory

`review_workflow.py` inventories every `financials[symbol].quality.pass == true`
in a hash-verified research snapshot. It does not filter by R:R, row priority,
AI request budget or the presence of a corresponding CSV row.

Run `python review_workflow.py input/research/YYYY-MM-DD.json` for a report.
Add `--promote` only to persist explicitly reviewed PASS packets after the
existing review intake and earnings policy validate them. `prepare` and `cycle`
also run this inventory in Actions. Existing schedules and entry rules are unchanged.

## Evidence and decisions

An authorized reviewer must read current SEC and issuer IR sources and record
each of the four existing assertions. This program does not turn keyword
absence, archived-but-unread documents or Gemini shadow output into approval.

Store a `SOURCE_ASSESSMENT` at
`input/source_assessments/US-DATE/SYMBOL.json`, or a symbol-keyed batch at
`input/source_assessments/US-DATE.json`. Fields: `kind`, `symbol`,
`rules_version`, `research_sha256`, `financial_evidence_sha256`, `reviewed_at`,
`reviewer_id=codex-delegated`, `delegation_id=owner_20261107_paper_only`,
and `assertions`. Each assertion uses the existing evidence fields and a
truthful common `checked_at`. Unknown uses `value: null` and an explicit
`rationale`; no evidence hash or date is invented. False requires an actual
archived official quote with matching issuer, hash, publication and retrieval
times. True requires the same evidence and the existing intake validation.

- `PASS`: all four explicitly supported and existing intake/earnings checks pass.
- `FAIL`: at least one explicitly false assertion with verified official evidence.
- `INSUFFICIENT_EVIDENCE`: unknown, invalid, absent, stale, or unsupported evidence.

One issuer's failure does not stop later issuers. A broken research snapshot
is rejected as a whole because its identity cannot be trusted. Findings for
past sessions can be recorded, but new retrospective approvals are refused.
There is no implicit carry-forward to a new research hash or session.

Reports are saved in `input/review_reports/US-DATE.json`. PASS packets use
the existing LINC schema in `input/review_inbox/US-DATE/SYMBOL.json`.
Writes are atomic and locally read back. `remote_verified_count` remains zero:
local success is not GitHub persistence. After publishing, independently fetch
the exact commit/file from GitHub and compare content hashes. Use the signed-in
Work cloud browser if the connected GitHub API rejects writes with 403.

## Limits

This is a complete inventory and validation interface, not a fully autonomous
semantic researcher. Unreviewed issuers remain visibly incomplete. It cannot
correct upstream revenue-tag extraction (for example, FLOC sales vs total
revenue), register a trade, modify investment rules, or claim a remote save.
FLOC stays pending until its financial evidence is resolved. Financial PASS
does not mean qualitative PASS, and qualitative PASS does not mean entry.

Tests: `python -m unittest discover -s tests -v`.

## Connected execution (2026-11-12)

`python review_workflow.py --collect --promote` selects the current decision's
research file rather than silently reusing yesterday. Missing current research
is an explicit `CURRENT_RESEARCH_MISSING` hold. With a supplied path, historical
reports remain possible; retrospective collection/approval is refused.

For **every** financial PASS, `collect_assessments` separately attempts SEC and
registered official IR acquisition, preserves receipts in `source_review_runs`,
and creates a four-item `SOURCE_ASSESSMENT` with null values when no explicit
assessment exists. A generated pending record has `reviewer_id=unassigned` and
`reviewed_at=null`: it is not a semantic review or an authorization signature.
Unsupported IR issuers remain queued; the registry does not narrow the universe.
Collectors and assessment failures are isolated by issuer. Full financial
research stays immutable, and generated source receipts do not rebind it.

The Work reviewer must read the source texts, resolve all four assertions and
record actual sources, quotes and review times. Only the existing authorized
attestation schema clears intake. Gemini SHADOW results stay SHADOW; quote
matching alone cannot prove a financial interpretation. Pending templates are
not automatically converted to `codex-delegated` attestations. There is still no
unattended semantic-review service embedded in Python.

During prepare, collection/intake now runs before plan preparation. Every plan
preparation also intakes newly supplied explicit assessments first. Investment
rules, checkpoint calculation, prices and the ledger code are unchanged.

The dedicated **NEXUS Official Source Review** Action supports manual execution
and runs on assessment pushes. It executes the same collector/intake without
calling the daily trading runner. Its persistence allowlist contains only
review sources/assessments/reports/inbox. It fetches the pushed GitHub commit,
compares exact bytes of every saved file and checks protected ledger/rule blobs.
`source_review_persistence.json` is a separate remote-proof artifact; local
readback counters are never relabeled as GitHub verification. Missing research,
missing sources and missing semantic decisions are reported separately.
