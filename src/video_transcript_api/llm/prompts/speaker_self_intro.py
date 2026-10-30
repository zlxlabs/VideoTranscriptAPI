"""Prompt for the independent self-introduction review call.

Kept out of prompts/__init__.py so that module's line budget stays at zero.
The request body is only {id, name, quote}; first-round inference output
must never appear here.
"""

import json
from typing import Dict, List, Sequence

SPEAKER_SELF_INTRO_SYSTEM_PROMPT = """You classify whether a quoted utterance is the speaker introducing themselves.

You receive only a candidate name and a verbatim quote. Do not use any other context. Do not reuse or invent first-round inference fields such as reasoning, confidence, or evidence_type.

Classify each item as exactly one of:
- self_introduction: the speaker is presenting their own name or identity.
- addressed_by_other: someone else is addressing or naming this person.
- third_party_mention: the quote talks about this name as a third person.
- unclear: the quote is not enough to tell.

Positive examples (self_introduction):
- name="Bob", quote="Bob here"
- name="柠姐", quote="柠姐在这儿"
- name="某某", quote="大家好，这里是某某"
- name="Alice", quote="Hi, I’m Alice"

Negative examples:
- name="John Collison", quote="John Collison wrote about this" → third_party_mention
- name="张三", quote="欢迎张三发言" → addressed_by_other
- name="Bob", quote="I forwarded this to Bob yesterday" → third_party_mention

Return JSON only: {"verdicts": [{"id": "...", "classification": "..."}, ...]}.
Every input id should appear at most once. Use only the four classifications above.
"""


def build_speaker_self_intro_user_prompt(items: Sequence[Dict[str, str]]) -> str:
    """Build the user prompt from program-generated {id, name, quote} items."""
    payload: List[Dict[str, str]] = [
        {
            "id": str(item["id"]),
            "name": str(item["name"]),
            "quote": str(item["quote"]),
        }
        for item in items
    ]
    encoded = json.dumps(payload, ensure_ascii=False)
    return (
        "Classify each item. Use only the quote and the candidate name.\n"
        f"<items>\n{encoded}\n</items>"
    )
