"""Independent LLM review of own-sample dialog_address quotes.

One call per infer() packs every quote that only appears in the speaker's
own samples. The first-round inference output is not sent. Missing or
out-of-domain classifications are recorded as unclear; call failures
propagate to the caller.
"""

from typing import Any, Dict, List, Optional, Sequence, Tuple

from ...utils.logging import setup_logger
from ..prompts.schemas.speaker_self_intro import (
    SELF_INTRO_CLASSIFICATIONS,
    SPEAKER_SELF_INTRO_SCHEMA,
)
from ..prompts.speaker_self_intro import (
    SPEAKER_SELF_INTRO_SYSTEM_PROMPT,
    build_speaker_self_intro_user_prompt,
)
from .llm_client import LLMClient

logger = setup_logger(__name__)

SPEAKER_SELF_INTRO_TASK_TYPE = "speaker_self_intro"
SELF_INTRO_PASS = "self_introduction"
SELF_INTRO_UNCLEAR = "unclear"


class SpeakerSelfIntroVerifier:
    """Classify own-sample quotes with one structured LLM call."""

    def __init__(
        self,
        llm_client: LLMClient,
        model: str,
        reasoning_effort: Optional[str] = None,
    ) -> None:
        self.llm_client = llm_client
        self.model = model
        self.reasoning_effort = reasoning_effort

    def verify(self, items: Sequence[Dict[str, str]]) -> Dict[str, str]:
        """Return id -> classification for every requested item.

        Requested ids missing from the response, or mapped to a value
        outside SELF_INTRO_CLASSIFICATIONS, are recorded as unclear.
        """
        requested_ids = [str(item["id"]) for item in items]
        if not requested_ids:
            return {}

        payload_items = [
            {"id": str(item["id"]), "name": str(item["name"]), "quote": str(item["quote"])}
            for item in items
        ]
        user_prompt = build_speaker_self_intro_user_prompt(payload_items)
        response = self.llm_client.call(
            model=self.model,
            system_prompt=SPEAKER_SELF_INTRO_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_schema=SPEAKER_SELF_INTRO_SCHEMA,
            reasoning_effort=self.reasoning_effort,
            task_type=SPEAKER_SELF_INTRO_TASK_TYPE,
        )
        parsed = _parse_verdicts(response.structured_output, requested_ids)
        passed = sum(1 for value in parsed.values() if value == SELF_INTRO_PASS)
        logger.info(
            f"Self-intro verification completed: candidates={len(requested_ids)} passed={passed}"
        )
        return parsed


def apply_self_intro_review(
    llm_client: LLMClient,
    model: str,
    reasoning_effort: Optional[str],
    prepared: Sequence[Tuple],
) -> None:
    """Pack pending own-sample quotes into one review call and attach verdicts.

    Each ``prepared`` row is
    ``(speaker, confidence, candidates, verified, rejected, pending)``.
    ``verified`` and ``rejected`` are mutated in place. No pending rows
    means the LLM is not called.
    """
    pending_items: List[Dict[str, str]] = []
    refs_by_row: List[List[Tuple[str, Dict[str, Any]]]] = []
    for row in prepared:
        refs: List[Tuple[str, Dict[str, Any]]] = []
        for item in row[5]:
            item_id = f"si-{len(pending_items)}"
            pending_items.append(
                {
                    "id": item_id,
                    "name": item["name"],
                    "quote": item["evidence_quote"],
                }
            )
            refs.append((item_id, item))
        refs_by_row.append(refs)
    verdicts: Dict[str, str] = {}
    if pending_items:
        verdicts = SpeakerSelfIntroVerifier(
            llm_client, model, reasoning_effort
        ).verify(pending_items)
    for row, refs in zip(prepared, refs_by_row):
        attach_self_intro_verdicts(refs, verdicts, row[3], row[4])


def attach_self_intro_verdicts(
    pending_refs: Sequence[Tuple[str, Dict[str, Any]]],
    verdicts: Dict[str, str],
    verified: List[Dict[str, Any]],
    rejected: List[str],
) -> None:
    """Apply review classifications onto pending own-sample candidates.

    Mutates the original candidate dict (``source``) with self_intro_verdict.
    Only self_introduction is appended to verified; every other class is
    rejected as name_not_self_intro.
    """
    for item_id, pending_cand in pending_refs:
        classification = verdicts.get(item_id, SELF_INTRO_UNCLEAR)
        if classification not in SELF_INTRO_CLASSIFICATIONS:
            classification = SELF_INTRO_UNCLEAR
        source = pending_cand.get("source")
        if isinstance(source, dict):
            source["self_intro_verdict"] = classification
        if classification == SELF_INTRO_PASS:
            verified.append(
                {
                    "name": pending_cand["name"],
                    "evidence_type": pending_cand["evidence_type"],
                    "evidence_quote": pending_cand["evidence_quote"],
                    "self_intro_verdict": classification,
                }
            )
        else:
            rejected.append("name_not_self_intro")


def _parse_verdicts(payload: Any, requested_ids: Sequence[str]) -> Dict[str, str]:
    """Map each requested id to a domain classification; gaps become unclear."""
    found: Dict[str, str] = {}
    if isinstance(payload, dict):
        entries = payload.get("verdicts")
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                item_id = entry.get("id")
                classification = entry.get("classification")
                if not isinstance(item_id, str) or item_id in found:
                    continue
                if item_id not in requested_ids:
                    continue
                if (
                    isinstance(classification, str)
                    and classification in SELF_INTRO_CLASSIFICATIONS
                ):
                    found[item_id] = classification
                else:
                    found[item_id] = SELF_INTRO_UNCLEAR
    return {
        item_id: found.get(item_id, SELF_INTRO_UNCLEAR) for item_id in requested_ids
    }
