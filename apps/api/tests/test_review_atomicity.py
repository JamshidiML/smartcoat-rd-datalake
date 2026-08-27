from __future__ import annotations

import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from threading import Barrier


SOURCE = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE))

from domain import (  # noqa: E402
    Actor,
    IngestionService,
    OCRDomainService,
    ReviewService,
    StateConflict,
)
from fakes import MemoryRepository, MemoryStorage  # noqa: E402


JPEG = b"\xff\xd8\xff\xe0" + b"m0-r04-synthetic-review-source" + b"\xff\xd9"
ACTOR = Actor("usr_founder", "SmartCoat Founder")
FAULT_CHECKPOINTS = (
    "after_decision",
    "after_draft_disposition",
    "after_verified_revision",
    "after_review_audit",
    "after_enter_review_state",
    "after_final_state",
)


class ReviewAtomicityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = MemoryRepository()
        result = IngestionService(
            self.repository,
            MemoryStorage(),
            1024 * 1024,
        ).ingest(
            ACTOR,
            "m0-r04-synthetic.jpg",
            JPEG,
            "LAB_NOTE",
            "Synthetic M0-R04 review fixture only.",
            None,
        )
        ocr = OCRDomainService(self.repository)
        run_id = ocr.start(
            result["ingestion_id"],
            "paddleocr",
            "3.7.0",
            {"fixture": "m0-r04"},
        )
        self.draft = ocr.complete(
            result["ingestion_id"],
            run_id,
            "Temperature 23 C",
            [],
            b'{"fixture":"m0-r04"}',
            f"rd/synthetic/{run_id}.json",
        )
        self.ingestion_id = result["ingestion_id"]
        self.service = ReviewService(self.repository, True)

    def approve(self, *, text: str = "Temperature 23 °C") -> dict:
        result = self.service.review(
            self.draft["silver_draft_id"],
            ACTOR,
            text,
            "APPROVED_WITH_CORRECTIONS",
            "Corrected unit symbol",
            True,
        )
        assert result is not None
        return result

    def reject(self) -> None:
        result = self.service.review(
            self.draft["silver_draft_id"],
            ACTOR,
            "",
            "REJECTED_UNREADABLE",
            "Synthetic unreadable decision",
            True,
        )
        self.assertIsNone(result)

    def test_exact_retry_replays_stable_response_without_duplicate_evidence(self) -> None:
        first = self.approve()
        second = self.approve()

        self.assertEqual(first, second)
        self.assertEqual(1, len(self.repository.reviews))
        self.assertEqual(1, len(self.repository.verified))
        self.assertEqual(1, first["silver_revision"])
        fingerprint = self.repository.reviews[0]["review_request_sha256"]
        self.assertRegex(fingerprint, r"^[0-9a-f]{64}$")
        review_audits = [
            event
            for event in self.repository.audit
            if event["event_type"] == "HUMAN_REVIEW_RECORDED"
            and event["details"].get("review_request_sha256") == fingerprint
        ]
        final_audits = [
            event
            for event in self.repository.audit
            if event["event_type"] == "UPLOAD_STATE_CHANGED"
            and event["new_state"] == "VERIFIED"
            and event["details"].get("review_request_sha256") == fingerprint
        ]
        self.assertEqual(1, len(review_audits))
        self.assertEqual(1, len(final_audits))

    def test_conflicting_retry_cannot_replace_effective_decision(self) -> None:
        accepted = self.approve()
        with self.assertRaises(StateConflict):
            self.reject()

        self.assertEqual([accepted["silver_record_id"]], [row["silver_record_id"] for row in self.repository.verified])
        self.assertEqual(1, len(self.repository.reviews))
        self.assertEqual("VERIFIED", self.repository.uploads[self.ingestion_id]["state"])

    def test_exact_rejection_retry_is_idempotent(self) -> None:
        self.reject()
        self.reject()

        self.assertEqual(1, len(self.repository.reviews))
        self.assertEqual(0, len(self.repository.verified))
        self.assertEqual("REVIEWED", self.repository.drafts[self.draft["silver_draft_id"]]["status"])
        self.assertEqual("REVIEW_REJECTED", self.repository.uploads[self.ingestion_id]["state"])

    def test_changed_verified_text_is_a_conflicting_retry(self) -> None:
        accepted = self.approve()
        with self.assertRaises(StateConflict):
            self.approve(text="Temperature 24 °C")

        self.assertEqual(1, len(self.repository.reviews))
        self.assertEqual([accepted["silver_record_id"]], [row["silver_record_id"] for row in self.repository.verified])

    def test_many_concurrent_exact_retries_resolve_to_one_stable_outcome(self) -> None:
        callers = 8
        barrier = Barrier(callers)

        def invoke() -> dict:
            barrier.wait()
            return self.approve()

        with ThreadPoolExecutor(max_workers=callers) as executor:
            results = list(executor.map(lambda _: invoke(), range(callers)))

        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(1, len(self.repository.reviews))
        self.assertEqual(1, len(self.repository.verified))
        self.assertEqual(1, results[0]["silver_revision"])

    def test_concurrent_conflicting_decisions_cannot_both_succeed(self) -> None:
        barrier = Barrier(2)

        def approve() -> tuple[str, object]:
            barrier.wait()
            try:
                return "accepted", self.approve()
            except StateConflict as exc:
                return "conflict", exc

        def reject() -> tuple[str, object]:
            barrier.wait()
            try:
                self.reject()
                return "accepted", None
            except StateConflict as exc:
                return "conflict", exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = [executor.submit(approve), executor.submit(reject)]
            results = [future.result() for future in outcomes]

        self.assertEqual(["accepted", "conflict"], sorted(result[0] for result in results))
        self.assertEqual(1, len(self.repository.reviews))
        self.assertLessEqual(len(self.repository.verified), 1)
        expected_state = "VERIFIED" if self.repository.verified else "REVIEW_REJECTED"
        self.assertEqual(expected_state, self.repository.uploads[self.ingestion_id]["state"])

    def test_fault_at_every_review_checkpoint_leaves_no_partial_evidence(self) -> None:
        for checkpoint in FAULT_CHECKPOINTS:
            with self.subTest(checkpoint=checkpoint):
                self.setUp()
                before = deepcopy(
                    {
                        "uploads": self.repository.uploads,
                        "drafts": self.repository.drafts,
                        "reviews": self.repository.reviews,
                        "verified": self.repository.verified,
                        "audit": self.repository.audit,
                    }
                )
                self.repository.review_fault_checkpoint = checkpoint
                with self.assertRaisesRegex(RuntimeError, checkpoint):
                    self.approve()
                after = {
                    "uploads": self.repository.uploads,
                    "drafts": self.repository.drafts,
                    "reviews": self.repository.reviews,
                    "verified": self.repository.verified,
                    "audit": self.repository.audit,
                }
                self.assertEqual(before, after)

                self.repository.review_fault_checkpoint = None
                recovered = self.approve()
                self.assertEqual(1, recovered["silver_revision"])
                self.assertEqual(1, len(self.repository.reviews))
                self.assertEqual(1, len(self.repository.verified))


if __name__ == "__main__":
    unittest.main()
