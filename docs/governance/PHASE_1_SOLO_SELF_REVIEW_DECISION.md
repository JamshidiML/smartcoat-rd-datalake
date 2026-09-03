# Phase-1 solo self-review decision

## Decision record

- Status: `RATIFIED`
- Decision date: `2026-09-03`
- Decision owner: Mohsen Jamshidi, Program Owner
- Signed: `/s/ Mohsen Jamshidi`

## Decision

Solo self-review remains a narrowly scoped Phase-1 exception for the single-user
local pilot. It is denied by default. A deployment may enable it only by setting
`ALLOW_PHASE_1_SOLO_SELF_REVIEW=true` as an explicit operator decision.
Missing, empty, or malformed values fail closed to `false`.

Enabling the exception does not remove the human-review boundary. The founder
must still view the immutable source, edit or confirm the transcription, select
a review decision, and explicitly confirm the source comparison. Each effective
self-review remains detectable and append-only evidence records:

- `self_review_detected = true`;
- `solo_exception_applied = true`;
- administrator exception reason `PHASE_1_SOLO_FOUNDER_REVIEW`.

The exception applies only while the Phase-1 pilot has one active uploader and
reviewer. It must be disabled before a second active reviewer is introduced.
It does not authorize bypassing review, creating a verified record from machine
output, changing append-only evidence, or weakening separation of database
credentials.

This decision does not change platform admission status. The platform remains
`BLOCKED`, and real company data remains prohibited until M0-R05 passes.
