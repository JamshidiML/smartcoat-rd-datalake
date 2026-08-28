from __future__ import annotations

import sys
import unittest
from pathlib import Path


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


STATES = (
    "RECEIVED",
    "BRONZE_COMMITTED",
    "OCR_QUEUED",
    "OCR_COMPLETED",
    "SILVER_DRAFT_READY",
    "UNDER_HUMAN_REVIEW",
    "VERIFIED",
    "REJECTED",
    "OCR_FAILED",
    "REVIEW_REJECTED",
)
LEGAL_TRANSITIONS = {
    ("RECEIVED", "BRONZE_COMMITTED"),
    ("RECEIVED", "REJECTED"),
    ("BRONZE_COMMITTED", "OCR_QUEUED"),
    ("OCR_QUEUED", "OCR_COMPLETED"),
    ("OCR_QUEUED", "OCR_FAILED"),
    ("OCR_COMPLETED", "SILVER_DRAFT_READY"),
    ("SILVER_DRAFT_READY", "UNDER_HUMAN_REVIEW"),
    ("UNDER_HUMAN_REVIEW", "VERIFIED"),
    ("UNDER_HUMAN_REVIEW", "REVIEW_REJECTED"),
    ("VERIFIED", "UNDER_HUMAN_REVIEW"),
}
JPEG = b"\xff\xd8\xff\xe0" + b"m0-r03-synthetic-jpeg" + b"\xff\xd9"


class GraphEnforcingMemoryRepository(MemoryRepository):
    """Test double mirroring the database trigger's externally visible result."""

    def transition(
        self,
        ingestion_id: str,
        previous_state: str,
        new_state: str,
        actor: str,
        details: dict | None = None,
    ) -> None:
        upload = self.uploads[ingestion_id]
        if upload["state"] != previous_state:
            raise StateConflict(f"expected {previous_state}, got {upload['state']}")
        if (upload["state"], new_state) not in LEGAL_TRANSITIONS:
            raise StateConflict(f"illegal transition: {upload['state']} -> {new_state}")
        super().transition(ingestion_id, previous_state, new_state, actor, details)


class StateTransitionServiceBoundaryTests(unittest.TestCase):
    def make_repository(self, state: str) -> GraphEnforcingMemoryRepository:
        repository = GraphEnforcingMemoryRepository()
        repository.uploads["00000000-0000-0000-0000-000000000001"] = {
            "ingestion_id": "00000000-0000-0000-0000-000000000001",
            "state": state,
        }
        return repository

    def test_every_legal_edge_is_accepted_by_the_service_repository_contract(self) -> None:
        for previous, following in sorted(LEGAL_TRANSITIONS):
            with self.subTest(previous=previous, following=following):
                repository = self.make_repository(previous)
                repository.transition(
                    "00000000-0000-0000-0000-000000000001",
                    previous,
                    following,
                    "system",
                )
                self.assertEqual(
                    following,
                    repository.uploads[
                        "00000000-0000-0000-0000-000000000001"
                    ]["state"],
                )

    def test_ingestion_ocr_review_and_revision_use_only_legal_edges(self) -> None:
        repository = GraphEnforcingMemoryRepository()
        actor = Actor("usr_m0r03", "M0 R03 Synthetic Reviewer")
        ingestion = IngestionService(repository, MemoryStorage(), 1024 * 1024)
        upload = ingestion.ingest(
            actor,
            "m0-r03-synthetic.jpg",
            JPEG,
            "LAB_NOTE",
            "Synthetic M0-R03 service-boundary acceptance fixture.",
            None,
        )
        ingestion_id = upload["ingestion_id"]

        ocr = OCRDomainService(repository)
        run_id = ocr.start(ingestion_id, "paddleocr", "synthetic", {})
        draft = ocr.complete(
            ingestion_id,
            run_id,
            "Synthetic OCR output",
            [],
            b"{}",
            "m0r03/synthetic-ocr.json",
        )
        review = ReviewService(repository, allow_phase_1_solo_self_review=True)
        first = review.review(
            draft["silver_draft_id"],
            actor,
            "Synthetic verified output",
            "APPROVED_WITH_CORRECTIONS",
            "Synthetic correction.",
            True,
        )
        self.assertIsNotNone(first)

        revision = review.edit_verified(
            ingestion_id,
            actor,
            "Synthetic revised output",
        )
        second = review.review(
            revision["silver_draft_id"],
            actor,
            "Synthetic revised output",
            "APPROVED_NO_CHANGES",
            "No further correction.",
            True,
        )
        self.assertIsNotNone(second)
        self.assertEqual("VERIFIED", repository.uploads[ingestion_id]["state"])
        emitted_edges = {
            (event["previous_state"], event["new_state"])
            for event in repository.audit
            if event["event_type"] == "UPLOAD_STATE_CHANGED"
        }
        self.assertTrue(emitted_edges)
        self.assertLessEqual(emitted_edges, LEGAL_TRANSITIONS)

    def test_every_illegal_changed_edge_is_rejected(self) -> None:
        for previous in STATES:
            for following in STATES:
                if previous == following or (previous, following) in LEGAL_TRANSITIONS:
                    continue
                with self.subTest(previous=previous, following=following):
                    repository = self.make_repository(previous)
                    with self.assertRaises(StateConflict):
                        repository.transition(
                            "00000000-0000-0000-0000-000000000001",
                            previous,
                            following,
                            "system",
                        )
                    self.assertEqual(
                        previous,
                        repository.uploads["00000000-0000-0000-0000-000000000001"]["state"],
                    )

    def test_stale_retry_cannot_apply_a_second_outcome(self) -> None:
        repository = self.make_repository("UNDER_HUMAN_REVIEW")
        ingestion_id = "00000000-0000-0000-0000-000000000001"
        repository.transition(
            ingestion_id,
            "UNDER_HUMAN_REVIEW",
            "VERIFIED",
            "usr_reviewer",
        )
        with self.assertRaises(StateConflict):
            repository.transition(
                ingestion_id,
                "UNDER_HUMAN_REVIEW",
                "REVIEW_REJECTED",
                "usr_reviewer",
            )
        self.assertEqual("VERIFIED", repository.uploads[ingestion_id]["state"])

    def test_terminal_states_reject_all_follow_up_calls(self) -> None:
        ingestion_id = "00000000-0000-0000-0000-000000000001"
        for terminal in ("REJECTED", "OCR_FAILED", "REVIEW_REJECTED"):
            for following in STATES:
                if following == terminal:
                    continue
                with self.subTest(terminal=terminal, following=following):
                    repository = self.make_repository(terminal)
                    with self.assertRaises(StateConflict):
                        repository.transition(
                            ingestion_id,
                            terminal,
                            following,
                            "system",
                        )


if __name__ == "__main__":
    unittest.main()
