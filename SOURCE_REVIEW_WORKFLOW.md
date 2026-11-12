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
