# Phase 1 acceptance report

**Run date:** <br>
**Operator/reviewer:** <br>
**Implementation SHA:** <br>
**PaddleOCR / PaddlePaddle:** 3.7.0 / 3.3.1 <br>
**Tesseract:** 5.3.0 <br>
**Capture device:** Company-issued iPhone 17

## Authorization and data handling

- [ ] Dated management/GDPR authorization record in `SILVER_REVIEW_POLICY.md` reviewed.
- [ ] All real sources were authorized for the pilot and stayed in local MinIO.
- [ ] No real source was placed in Git or sent to an external OCR/vision API.
- [ ] Batch includes at least one screen glare/reflection case despite standardized capture hardware.

## Automated controls

| ID | Result | Evidence / notes |
| --- | --- | --- |
| AT-01 |  | Exactly one original and manifest |
| AT-02 |  | Required manifest metadata |
| AT-03 |  | Recomputed source SHA-256 |
| AT-04 |  | Duplicate retained and linked |
| AT-05 |  | Unsupported/corrupt/protected rejection audit |
| AT-06 |  | OCR ordering guard |
| AT-07 |  | Engine/config/raw/source checksums |
| AT-08 |  | Draft-only OCR output |
| AT-09 |  | Human identity/time/confirmation/decision/source |
| AT-10 |  | Self-review detected + Phase-1 solo exception audited |
| AT-11 |  | Post-verification edit creates reviewed next revision |
| AT-12 |  | Application identity cannot delete/overwrite Bronze |
| AT-13 |  | Isolated local restore drill |
| AT-14 |  | Tracked secret/real-data scan |

## Real batch composition

Use 5–10 files: at least four phone photos; at least one skew/glare/blur/framing challenge; at least one German/English technical source; at least one source with tables, numbers, units, or formulations; optionally PDF and Excel. Do not exclude failures.

| File ID | Type / difficulty | Bronze + manifest valid | Paddle word accuracy | Tesseract word accuracy | Numeric/units accuracy | Correction rate | Review time | Usability | Failure mode | Final status |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |
| 01 |  |  |  |  |  |  |  |  |  |  |
| 02 |  |  |  |  |  |  |  |  |  |  |
| 03 |  |  |  |  |  |  |  |  |  |  |
| 04 |  |  |  |  |  |  |  |  |  |  |
| 05 |  |  |  |  |  |  |  |  |  |  |

Word accuracy is `(reference words − word errors) / reference words`. Numeric/units accuracy explicitly covers numbers, decimal separators, temperatures, weights, dates, units, and material codes. Correction rate is changed words divided by OCR words. Usability is `usable`, `usable with substantial correction`, or `not usable`.

## Aggregate and failures

**Median reviewer correction rate:** <br>
**Files judged usable:** <br>
**Critical numbers/units corrected before approval:** <br>
**Paddle vs Tesseract summary:** <br>
**All failures and rejected files (mandatory):**

## Backup/restore

**Backup path and timestamp:** <br>
**Manifest/original/database provenance restored:** <br>
**Restore-drill result:**

## Decision

- **GO:** median correction rate ≤10%, all critical numbers/units corrected, and at least 80% usable.
- **CONDITIONAL:** all records validate, but correction rate >10% or usability <80%; improve and repeat a 10-file batch.
- **BLOCKED:** provenance failure, unverified content marked factual, no human sign-off, or correction is not reasonably usable.

**Decision:** GO / CONDITIONAL / BLOCKED <br>
**Rationale:**

## Human sign-off

I reviewed the raw results, corrections, failures, Bronze integrity evidence, and restore drill. Every Silver result is human-verified or explicitly rejected.

**Reviewer name:** <br>
**Signature:** <br>
**Date:**
