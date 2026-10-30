"""Self-introduction review JSON schema.

Independent of the first-round speaker inference schema: this call only
classifies already-checked own-sample quotes.
"""

SELF_INTRO_CLASSIFICATIONS: tuple[str, ...] = (
    "self_introduction",
    "addressed_by_other",
    "third_party_mention",
    "unclear",
)

SPEAKER_SELF_INTRO_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "description": "One classification per reviewed quote",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "classification": {
                        "type": "string",
                        "enum": list(SELF_INTRO_CLASSIFICATIONS),
                    },
                },
                "required": ["id", "classification"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}
