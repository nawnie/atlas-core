from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from atlas_core import AtlasCore
from atlas_core.adapters import HotelAdapter


class IntelligenceRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.engine = AtlasCore(Path(self.temp.name) / "answers.db", (HotelAdapter(),))
        self.base = {
            "reservation_id": "ASK-1",
            "version": "1",
            "updated_at": "2026-07-24T12:00:00Z",
            "arrival_date": "2026-08-10",
            "departure_date": "2026-08-13",
            "guest_count": 2,
            "room_type": "king",
            "status": "confirmed",
            "total": "729",
            "currency": "USD",
        }
        self._ingest(self.base)
        self._ingest(
            {
                **self.base,
                "version": "2",
                "updated_at": "2026-07-24T12:05:00Z",
                "status": "cancelled",
                "total": "0",
            }
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _ingest(self, record: dict[str, object], tenant_id: str = "hotel-ask"):
        return self.engine.ingest(
            "hotel",
            record,
            tenant_id=tenant_id,
            source_system="fixture-pms",
        )

    def test_answer_links_every_claim_to_tenant_evidence(self) -> None:
        answer = self.engine.answer(
            tenant_id="hotel-ask",
            query="Why was reservation ASK-1 cancelled and what should happen next?",
        )
        self.assertEqual(
            tuple(section.title for section in answer.sections),
            (
                "What changed",
                "Why Atlas believes this changed",
                "What should happen next",
            ),
        )
        self.assertEqual(answer.confidence, 1.0)
        self.assertTrue(answer.citations)
        citation_ids = {citation.citation_id for citation in answer.citations}
        referenced = {
            citation_id
            for section in answer.sections
            for claim in section.claims
            for citation_id in claim.citation_ids
        }
        self.assertEqual(citation_ids, referenced)
        self.assertTrue(
            all(citation.entity_id == "ASK-1" for citation in answer.citations)
        )
        evidence_types = {citation.evidence_type for citation in answer.citations}
        self.assertTrue({"change", "observation", "cartography", "proposal"} <= evidence_types)
        self.assertEqual(len(answer.sections[2].claims), 1)
        self.assertIn(
            "fixture-pms",
            next(
                citation.source_pointer
                for citation in answer.citations
                if citation.evidence_type == "observation"
            ),
        )
        self.assertTrue(any("unstored root cause" in item for item in answer.limitations))

    def test_answer_is_deterministic_and_tenant_scoped(self) -> None:
        first = self.engine.answer(tenant_id="hotel-ask", query="cancelled")
        second = self.engine.answer(tenant_id="hotel-ask", query="cancelled")
        self.assertEqual(
            tuple(citation.citation_id for citation in first.citations),
            tuple(citation.citation_id for citation in second.citations),
        )
        other = self.engine.answer(tenant_id="other-company", query="cancelled")
        self.assertEqual(other.confidence, 0.0)
        self.assertEqual(other.citations, ())

    def test_no_match_and_invalid_bounds_do_not_generate_unsupported_content(self) -> None:
        answer = self.engine.answer(
            tenant_id="hotel-ask",
            query="quantum turbine maintenance",
        )
        self.assertEqual(answer.confidence, 0.0)
        self.assertEqual(answer.citations, ())
        self.assertIn("did not generate", answer.sections[0].claims[0].text)
        with self.assertRaisesRegex(ValueError, "500"):
            self.engine.answer(tenant_id="hotel-ask", query="x" * 501)
        with self.assertRaisesRegex(ValueError, "between 1 and 20"):
            self.engine.answer(tenant_id="hotel-ask", query="", limit=21)


if __name__ == "__main__":
    unittest.main()
