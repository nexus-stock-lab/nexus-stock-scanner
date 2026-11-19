# Automated official-source interpretation

The owner requested continuous semantic review on 2026-11-20. Existing Actions
now call `automated_review.py` through `review_workflow.connected_run`, including
before future cycle planning. No schedule, entry rule, earnings rule or ledger
method is changed. Prior-session research is reference-only and cannot promote.

The existing Gemini model and free-project confirmation gate are retained. Only
verified public SEC/registered issuer IR texts are sent. No broker credentials,
ledger or personal information is supplied. A missing key, missing free-project
confirmation, quota, malformed response or incomplete source bundle is a hold.
There is no paid upgrade or alternative provider. The shared ten-request daily
budget counts every call before sending. Candidate rotation, complete responses,
partial primary/verifier responses and proof receipts persist across Actions.

The primary and skeptical verifier make two separate source-only semantic
interpretations of the same exact bundle. Neither is shown the other's answer.
Agreement is not a guarantee of correctness. Both must provide evidence for all
four assertions; disagreement/unknown is insufficient evidence. Matching text
alone never generates a positive assertion. Confirmed contradiction is FAIL.
The existing exact-date AI response schema cannot establish the unannounced-date
exception: such cases stay UNKNOWN unless explicitly reviewed via the unchanged
existing review route. No missing calendar information is treated as absence.

Machine assessments truthfully use `gemini-delegated`, never `codex-delegated`.
Delegation is `owner_20261120_official_paper_automation`. Successful existing
intake writes the LINC-compatible `DELEGATED_SOURCE_REVIEW` plus an
`automated_evidence_sha256` receipt pointer. Intake independently rechecks both
responses, source bytes/issuer/time, response hashes, financial binding, session
and assertion equality. Existing disclosure, earnings and plan checks still run.
This is an auditable automated interpretation, not a fabricated human signature.
Explicit human/Work assessments are preserved. Raw SHADOW records remain SHADOW.

The service never writes plans, quotes or a ledger. Existing `cycle` and
`paper_engine` remain responsible for future plans, SIP references and R:R.
Research generated after a past checkpoint cannot create a past entry.

Operational proof is `output/automated_review_status.json`, the source-review
persistence readback, saved assessment/receipt files and subsequent cycle logs.
Tests use fictional sources in temporary directories, not real approvals.
