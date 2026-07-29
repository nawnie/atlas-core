from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any

from .memory import CompanyMemory
from .models import (
    AnswerCitation,
    AnswerClaim,
    AnswerSection,
    IntelligenceAnswer,
)

_STOP_WORDS = {
    "and",
    "did",
    "do",
    "for",
    "happen",
    "next",
    "should",
    "that",
    "the",
    "this",
    "what",
    "why",
    "with",
    "change",
    "changed",
}


def _citation_id(evidence_type: str, subject_id: str) -> str:
    digest = hashlib.sha256(f"{evidence_type}:{subject_id}".encode()).hexdigest()
    return f"cite_{digest[:20]}"


def _tokens(query: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            token
            for token in re.findall(r"[a-z0-9][a-z0-9_-]+", query.lower())
            if token not in _STOP_WORDS
        )
    )


def _excerpt(value: object, limit: int = 360) -> str:
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, sort_keys=True, default=str)
    )
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


class IntelligenceRetriever:
    """Deterministic cited answers over Company Memory and Cartographer."""

    def __init__(self, memory: CompanyMemory) -> None:
        self._memory = memory

    def answer(
        self,
        *,
        tenant_id: str,
        query: str,
        limit: int = 5,
    ) -> IntelligenceAnswer:
        tenant_id = tenant_id.strip()
        query = query.strip()
        if not tenant_id or len(tenant_id) > 100:
            raise ValueError(
                "tenant_id is required and must be at most 100 characters."
            )
        if len(query) > 500:
            raise ValueError("query must be at most 500 characters.")
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20.")

        context = self._memory.intelligence_context(tenant_id=tenant_id)
        proposals_by_entity: dict[
            tuple[str, str, str], list[dict[str, Any]]
        ] = defaultdict(list)
        for proposal in context["proposals"]:
            proposals_by_entity[
                (
                    proposal["domain"],
                    proposal["entity_type"],
                    proposal["entity_id"],
                )
            ].append(proposal)
        cards_by_observation: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for card in context["cards"]:
            try:
                observation_id = json.loads(card.get("details", "{}")).get(
                    "observation_id"
                )
            except json.JSONDecodeError:
                observation_id = None
            if observation_id:
                cards_by_observation[str(observation_id)].append(card)

        tokens = _tokens(query)
        ranked: list[tuple[int, str, dict[str, Any]]] = []
        for change in context["changes"]:
            entity_key = (
                change["domain"],
                change["entity_type"],
                change["entity_id"],
            )
            related_proposals = proposals_by_entity.get(entity_key, ())
            related_cards = cards_by_observation.get(change["observation_id"], ())
            searchable = json.dumps(
                {
                    "change": change,
                    "proposals": related_proposals,
                    "cards": related_cards,
                },
                sort_keys=True,
                default=str,
            ).lower()
            score = 1 if not tokens else sum(searchable.count(token) for token in tokens)
            if tokens and not score:
                continue
            if change["entity_id"].lower() in tokens:
                score += 4
            if change["domain"].lower() in tokens:
                score += 2
            ranked.append((score, change["created_at"], change))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        selected = [item[2] for item in ranked[:limit]]

        citations: dict[str, AnswerCitation] = {}
        changed_claims: list[AnswerClaim] = []
        why_claims: list[AnswerClaim] = []
        next_claims: list[AnswerClaim] = []
        proposed_next_steps: set[str] = set()

        for change in selected:
            change_id = change["change_id"]
            observation_id = change["observation_id"]
            entity_key = (
                change["domain"],
                change["entity_type"],
                change["entity_id"],
            )
            change_citation_id = _citation_id("change", change_id)
            observation_citation_id = _citation_id("observation", observation_id)
            source_pointer = (
                f"{change['source_system']}:{change['source_record_id']}:"
                f"{change['source_version'] or 'unversioned'}"
            )
            citations[change_citation_id] = AnswerCitation(
                citation_id=change_citation_id,
                evidence_type="change",
                subject_id=change_id,
                domain=change["domain"],
                entity_type=change["entity_type"],
                entity_id=change["entity_id"],
                source_pointer=f"change:{change_id}",
                occurred_at=change["created_at"],
                excerpt=_excerpt(change["change"]["summary"]),
            )
            citations[observation_citation_id] = AnswerCitation(
                citation_id=observation_citation_id,
                evidence_type="observation",
                subject_id=observation_id,
                domain=change["domain"],
                entity_type=change["entity_type"],
                entity_id=change["entity_id"],
                source_pointer=source_pointer,
                occurred_at=change["observed_at"],
                excerpt=_excerpt(change["attributes"]),
            )
            changed_claims.append(
                AnswerClaim(
                    text=(
                        f"{change['domain']} {change['entity_type']} "
                        f"{change['entity_id']}: {change['change']['summary']}"
                    ),
                    citation_ids=(change_citation_id, observation_citation_id),
                )
            )

            map_citation_ids: list[str] = []
            for card in cards_by_observation.get(observation_id, ())[:1]:
                card_id = str(card.get("card_id", ""))
                if not card_id:
                    continue
                card_citation_id = _citation_id("cartography", card_id)
                citations[card_citation_id] = AnswerCitation(
                    citation_id=card_citation_id,
                    evidence_type="cartography",
                    subject_id=card_id,
                    domain=change["domain"],
                    entity_type=change["entity_type"],
                    entity_id=change["entity_id"],
                    source_pointer=(
                        card.get("source_pointers", [source_pointer])[0]
                        if card.get("source_pointers")
                        else source_pointer
                    ),
                    occurred_at=str(card.get("created_at_utc", "")),
                    excerpt=_excerpt(card.get("summary", "")),
                )
                map_citation_ids.append(card_citation_id)
            why_claims.append(
                AnswerClaim(
                    text=(
                        f"Atlas observed this through {source_pointer} and translated "
                        "it through the registered domain mapping. Stored evidence "
                        "proves provenance, not an unstored human or business cause."
                    ),
                    citation_ids=(
                        observation_citation_id,
                        *map_citation_ids,
                    ),
                )
            )

            related_proposals = proposals_by_entity.get(entity_key, ())
            if related_proposals:
                for proposal in related_proposals[:1]:
                    proposal_id = proposal["id"]
                    if proposal_id in proposed_next_steps:
                        continue
                    proposed_next_steps.add(proposal_id)
                    proposal_citation_id = _citation_id("proposal", proposal_id)
                    citations[proposal_citation_id] = AnswerCitation(
                        citation_id=proposal_citation_id,
                        evidence_type="proposal",
                        subject_id=proposal_id,
                        domain=proposal["domain"],
                        entity_type=proposal["entity_type"],
                        entity_id=proposal["entity_id"],
                        source_pointer=f"proposal:{proposal_id}",
                        occurred_at=proposal["created_at"],
                        excerpt=_excerpt(
                            {
                                "action_type": proposal["action_type"],
                                "reason": proposal["reason"],
                                "status": proposal["status"],
                                "review_tasks": proposal["payload"].get(
                                    "review_tasks",
                                    (),
                                ),
                            }
                        ),
                    )
                    tasks = proposal["payload"].get("review_tasks", ())
                    task_text = "; ".join(str(task) for task in tasks[:3])
                    next_claims.append(
                        AnswerClaim(
                            text=(
                                f"{proposal['action_type']} is {proposal['status']} "
                                f"({proposal['risk']} risk). {proposal['reason']}"
                                + (f" Review: {task_text}." if task_text else "")
                            ),
                            citation_ids=(proposal_citation_id,),
                        )
                    )
            else:
                next_claims.append(
                    AnswerClaim(
                        text=(
                            f"No action proposal is stored for {change['domain']} "
                            f"{change['entity_type']} {change['entity_id']}; review "
                            "the cited evidence before taking any external action."
                        ),
                        citation_ids=(change_citation_id,),
                    )
                )

        if not selected:
            no_result = AnswerClaim(
                text=(
                    "No tenant-scoped Company Memory evidence matched this query. "
                    "Atlas did not generate an unsupported answer."
                ),
                citation_ids=(),
            )
            sections = (
                AnswerSection("What changed", (no_result,)),
                AnswerSection(
                    "Why Atlas believes this changed",
                    (
                        AnswerClaim(
                            "No provenance was retrieved, so no cause is asserted.",
                            (),
                        ),
                    ),
                ),
                AnswerSection(
                    "What should happen next",
                    (
                        AnswerClaim(
                            "Refine the query or import the missing source records.",
                            (),
                        ),
                    ),
                ),
            )
            headline = "No grounded evidence matched the question."
            confidence = 0.0
        else:
            sections = (
                AnswerSection("What changed", tuple(changed_claims)),
                AnswerSection(
                    "Why Atlas believes this changed",
                    tuple(why_claims),
                ),
                AnswerSection("What should happen next", tuple(next_claims)),
            )
            headline = (
                f"Atlas found {len(selected)} evidence-backed change "
                f"{'record' if len(selected) == 1 else 'records'}."
            )
            confidence = 1.0

        referenced = {
            citation_id
            for section in sections
            for claim in section.claims
            for citation_id in claim.citation_ids
        }
        return IntelligenceAnswer(
            tenant_id=tenant_id,
            query=query,
            headline=headline,
            sections=sections,
            citations=tuple(
                citation
                for citation_id, citation in citations.items()
                if citation_id in referenced
            ),
            confidence=confidence,
            limitations=(
                "Answers use deterministic retrieval over stored tenant evidence; no LLM is invoked.",
                "Provenance explains where a change came from, not an unstored root cause or intent.",
                "Recommendations are recorded proposals and still require human review before execution.",
            ),
        )
