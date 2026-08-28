from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MIGRATION = (
    ROOT
    / "infra/postgres/migrations/0003__enforce_upload_state_transitions.sql"
)
INIT_SQL = ROOT / "infra/postgres/init.sql"

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


def installed_edges(sql: str) -> set[tuple[str, str]]:
    return {
        (previous, following)
        for previous, following, _name in re.findall(
            r"\('([A-Z_]+)', '([A-Z_]+)', '([a-z_]+)'\)", sql
        )
    }


class StateTransitionMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_migration_installs_the_exact_frozen_graph(self) -> None:
        self.assertEqual(LEGAL_TRANSITIONS, installed_edges(self.sql))
        self.assertEqual(10, len(LEGAL_TRANSITIONS))
        self.assertEqual(set(STATES), {state for edge in LEGAL_TRANSITIONS for state in edge})

    def test_every_changed_state_pair_is_unambiguously_legal_or_illegal(self) -> None:
        changed_pairs = {
            (previous, following)
            for previous in STATES
            for following in STATES
            if previous != following
        }
        self.assertEqual(90, len(changed_pairs))
        self.assertEqual(10, len(changed_pairs & LEGAL_TRANSITIONS))
        self.assertEqual(80, len(changed_pairs - LEGAL_TRANSITIONS))

    def test_terminal_states_have_no_outgoing_edges(self) -> None:
        outgoing = {previous for previous, _following in LEGAL_TRANSITIONS}
        self.assertTrue({"REJECTED", "OCR_FAILED", "REVIEW_REJECTED"}.isdisjoint(outgoing))

    def test_verified_has_only_the_revision_edge(self) -> None:
        self.assertEqual(
            {("VERIFIED", "UNDER_HUMAN_REVIEW")},
            {edge for edge in LEGAL_TRANSITIONS if edge[0] == "VERIFIED"},
        )

    def test_machine_extraction_cannot_transition_directly_to_verified(self) -> None:
        self.assertNotIn(("OCR_QUEUED", "VERIFIED"), LEGAL_TRANSITIONS)
        self.assertNotIn(("OCR_COMPLETED", "VERIFIED"), LEGAL_TRANSITIONS)
        self.assertNotIn(("SILVER_DRAFT_READY", "VERIFIED"), LEGAL_TRANSITIONS)

    def test_database_row_state_not_caller_input_authorizes_the_edge(self) -> None:
        self.assertIn("legal.previous_state = OLD.state", self.sql)
        self.assertIn("legal.next_state = NEW.state", self.sql)
        self.assertIn("BEFORE UPDATE OF state ON public.uploads", self.sql)
        self.assertIn("CONSTRAINT = 'uploads_legal_state_transition'", self.sql)

    def test_only_received_is_a_legal_initial_persisted_state(self) -> None:
        self.assertIn("IF NEW.state <> 'RECEIVED'", self.sql)
        self.assertIn("BEFORE INSERT ON public.uploads", self.sql)
        self.assertIn("CONSTRAINT = 'uploads_legal_initial_state'", self.sql)

    def test_graph_and_enforcement_contract_are_not_runtime_mutable(self) -> None:
        self.assertIn("legal_upload_transitions_immutable", self.sql)
        self.assertIn("BEFORE UPDATE OR DELETE", self.sql)
        self.assertIn(
            "REVOKE ALL ON TABLE smartcoat_state.legal_upload_transitions FROM PUBLIC",
            self.sql,
        )
        self.assertIn("SECURITY DEFINER", self.sql)
        self.assertIn("SET search_path = pg_catalog, smartcoat_state", self.sql)

    def test_existing_volume_state_vocabulary_is_preserved(self) -> None:
        init_sql = INIT_SQL.read_text(encoding="utf-8")
        state_check = re.search(
            r"state text NOT NULL CHECK \(state IN \((.*?)\n\s*\)\)",
            init_sql,
            re.DOTALL,
        )
        self.assertIsNotNone(state_check)
        assert state_check is not None
        init_states = set(re.findall(r"'([A-Z_]+)'", state_check.group(1)))
        self.assertEqual(set(STATES), init_states)
        self.assertNotIn("ALTER TABLE public.uploads", self.sql)
        self.assertNotIn("UPDATE public.uploads", self.sql)


if __name__ == "__main__":
    unittest.main()
