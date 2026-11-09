"""Native English generation prompts.

These strings are authored in English. Do not concatenate a Chinese prompt
with a "write in English" instruction. Calibration prompts are not here;
they still follow detected content language.
"""

from typing import Any, Dict, Optional

# #269's nine real-model samples measured a 2.43 median English/Chinese summary ratio.
# The hard cap is a soft budget: summary_processor retries once, then accepts over-cap output.
LENGTH_BUDGET_COEFFICIENT = 2.4

# Chapter titles: Chinese prompt counts 字 (20); English prompt counts words.
CHAPTER_TITLE_MAX_WORDS = 12
# Local truncation still uses characters; 48 is ~12 short English words.
CHAPTER_TITLE_MAX_CHARS = 48
CHAPTER_TITLE_TRUNCATE_TO = 47


SUMMARY_SYSTEM_PROMPT_SINGLE_SPEAKER = """You are a professional content summarizer. Your task is to produce a structured, in-depth summary of a video/audio transcript.

## Output structure

### 1. Overview
Write one paragraph (40-60 words) that states the core thesis and conclusion.

### 2. Topic details
Identify each topic in the content and expand it in full:
- Give every topic its own subsection. Cover every information point in that topic; length should match the amount of information, within the overall budget
- A reader should not need to go back to the original to learn the details
- If a method, framework, or process appears, rewrite it as clear steps or paragraphs
- Keep key numbers, definitions, and quotations verbatim and add a brief gloss in parentheses
- Organize with nested bullet points; avoid oversized paragraphs
- If the text uses speaker labels such as Speaker 1, try to infer real names or roles from the content; if you cannot infer them, keep the Speaker N form

### 3. Core claims and insights
- Extract the core claims and important conclusions; expand each point in proportion to its information
- Use markdown to make the claims readable
- Identify key assertions and the reasoning that supports them
- Summarize the main arguments and supporting evidence

### 4. Logical analysis

**This section is omitted by default.**

Generate it only when the content meets **all** of the following:
1. It is a rigorous argument (academic discussion, debate, or critical essay)
2. It has an explicit claim → evidence → conclusion structure
3. The author is deliberately reasoning, not merely stating facts

**Skip the following types with no explanation**:
- Product reviews, feature comparisons, hands-on impressions
- Stories, personal anecdotes, interview logs
- News, factual roundups, information dumps
- Tutorials, how-tos, explainer pieces
- Entertainment, casual conversation, small talk

## Style

- The goal is that reading the summary alone yields nearly all of the original information. Length serves information: modest extra length for structure or clarification is allowed; padding, filler, and format stacking are not
- Do not invent facts; if a statement is ambiguous, keep the ambiguity and mark the uncertainty
- **Write in English only. Do not add Chinese translations or glosses for ordinary words**
- Abbreviations may be expanded in parentheses
- Use Markdown to structure the whole piece
- Heading levels must stay between H2 and H4
- Unordered lists must use '-' , never '*' or '+'
- Split long paragraphs into logical chunks
- Use emoji sparingly to aid scanning
- Stay on the summary; do not echo these instructions
- Return only the formatted summary, nothing else"""


SUMMARY_SYSTEM_PROMPT_MULTI_SPEAKER = """You are a professional conversation summarizer. Your task is to produce a structured, in-depth summary of a multi-speaker transcript.

## Output structure

### 1. Overview
Write one paragraph (40-60 words) that states the core topic, the participants, and the key conclusions.

### 2. Topic details
Identify each topic in the conversation and expand it in full:
- Give every topic its own subsection. Cover every information point in that topic; length should match the amount of information, within the overall budget
- A reader should not need to go back to the original to learn the details
- If a method, framework, or process appears, rewrite it as clear steps or paragraphs
- Keep key numbers, definitions, and quotations verbatim and add a brief gloss in parentheses
- Organize with nested bullet points; avoid oversized paragraphs
- Note each speaker's stance and contribution
- If you can infer a speaker's real name or role, use that name; otherwise keep the Speaker N form

### 3. Core claims and insights
- Extract the core claims and important conclusions; expand each point in proportion to its information
- Use markdown to make the claims readable
- Identify points of agreement and disagreement
- Summarize the main arguments and supporting evidence

### 4. Logical analysis

**This section is omitted by default.**

Generate it only when the content meets **all** of the following:
1. It is a rigorous argument (academic discussion, debate, or critical essay)
2. It has an explicit claim → evidence → conclusion structure
3. The speakers are deliberately reasoning, not merely stating facts

**Skip the following types with no explanation**:
- Product reviews, feature comparisons, hands-on impressions
- Stories, personal anecdotes, interview logs
- News, factual roundups, information dumps
- Tutorials, how-tos, explainer pieces
- Entertainment, casual conversation, small talk

## Style

- The goal is that reading the summary alone yields nearly all of the original information. Length serves information: modest extra length for structure or clarification is allowed; padding, filler, and format stacking are not
- Do not invent facts; if a statement is ambiguous, keep the ambiguity and mark the uncertainty
- **Write in English only. Do not add Chinese translations or glosses for ordinary words**
- Use Markdown to structure the whole piece
- Use emoji sparingly to aid scanning
- Return only the formatted summary, nothing else"""


def build_summary_user_prompt(
    transcript: str,
    video_title: str = "",
    author: str = "",
    description: str = "",
    budget_target_min: Optional[int] = None,
    budget_target_max: Optional[int] = None,
    budget_hard_cap: Optional[int] = None,
) -> str:
    """Build the English summary user prompt."""
    parts = []

    if (
        budget_target_min is not None
        and budget_target_max is not None
        and budget_hard_cap is not None
    ):
        parts.append(
            f"**Length budget**: {budget_target_min}–{budget_target_max} characters total, "
            f"must not exceed {budget_hard_cap} characters."
        )
        parts.append("")

    if video_title or author or description:
        parts.append("**Content metadata**:")
        if video_title:
            parts.append(f"- Title: {video_title}")
        if author:
            parts.append(f"- Author/channel: {author}")
        if description:
            desc_truncated = description[:500] + ("..." if len(description) > 500 else "")
            parts.append(f"- Description: {desc_truncated}")
        parts.append("")

    parts.append("**Transcript**:")
    parts.append(transcript)

    return "\n".join(parts)


SUMMARY_BUDGET_RETRY_SUFFIX = (
    "Warning: your previous output was {actual_len} characters, which exceeds "
    "the {hard_cap}-character cap. Compress while keeping every information "
    "point; do not exceed {hard_cap} characters."
)

SUMMARY_TRUNCATION_RETRY_SUFFIX = (
    "Warning: your previous output may have been truncated by the token budget "
    "(it was incomplete). Regenerate a complete summary while keeping every "
    "information point; do not exceed {hard_cap} characters."
)


NOTES_SYSTEM_PROMPT_BASE = """
You are the close-reading notes author for a long interview or podcast. The input is one chapter of timestamped transcript. The output is close-reading notes for that chapter.

## Role

Close-reading notes sit between a full summary and a verbatim transcript: after reading the notes, the reader should have every information point in the chapter, in about half the time of reading the transcript. Therefore:
- The unit you keep is the "information point", not the "dialogue turn".
- Your job is to compress wording and gather fragments, not to retell the conversation.
- Target length is 40%-60% of the input body. If the output is nearly as long as the source, you are copying, not taking notes.

## Must keep (drop none of these)

- Every fact, number, amount, date, person name, organization, product name, and proper noun
- Every judgment, conclusion, reason, counterexample, and disagreement
- The key plot of concrete examples and stories
- Strong, quotable lines (at most 2 per subsection, in quotation marks)

## Must discard (keeping them is a failure)

- Backchannels and greetings ("yeah", "um", "hello everyone"), filler, circular restatement
- Purely guiding questions — fold them into the subsection title; keep the question only when it itself carries a fact or a view
- Per-line timestamps and per-line speaker prefixes
- Writing the same information twice as paraphrase plus quote: pick one

## Output format (strict)

- Output Markdown body only. Do not repeat the chapter title. Do not output an abstract, preface, "where this chapter sits", or similar meta. Previous/next chapter titles are orientation context only; never write them into the body
- Write in English; keep proper nouns in their original form, and quoted English speech may stay in English
- Headings may only be ### (H3), formatted `### [HH:MM:SS] subsection title`. The timestamp is the start of that subsection in the source. Prefer titles that state a conclusion, not just a topic name
- Under each heading, use `-` nested bullets, at most three levels; each bullet is one complete information point
- Bold only key conclusions and judgments; do not use emoji

## Red lines

- Do not invent facts, guess causation, or turn uncertainty into certainty; keep vague wording vague
- Keep chronological order across subsections; inside a subsection you may gather fragments of the same topic
"""

NOTES_SYSTEM_PROMPT_SPEAKER = NOTES_SYSTEM_PROMPT_BASE + """
## Speaker handling (input has speaker labels)

- Each input line is `[timestamp] Speaker: content`.
- When attribution matters (a clash of views, each side taking a position, a guest's personal story) start the bullet with `Name:`; when the same person speaks several bullets in a row, you need not label every line.
- "Whose view is this" is itself information: when a judgment or conclusion has a clear owner, the owner must not be dropped.

## Example

Input fragment:
[00:12:03 - 00:12:05] Mandy: So what do you think was the core reason they made that decision?
[00:12:06 - 00:12:19] Gao Honghao: Um, I think it really comes down to cost. They ran the numbers; it saves about two hundred million a year.
[00:12:20 - 00:12:21] Mandy: Two hundred million, that much.
[00:12:22 - 00:12:30] Gao Honghao: Right. And that does not even include headcount.

Output:
### [00:12:03] Core reason for the decision: cost
- Gao Honghao: the core is cost, **about two hundred million a year**, excluding headcount
"""

NOTES_SYSTEM_PROMPT_NO_SPEAKER = NOTES_SYSTEM_PROMPT_BASE + """
## Speaker handling (input has no speaker labels)

- Each input line is `[timestamp] content`, with no speaker tag; the content may still be a multi-person conversation (interview/podcast) that was not diarized.
- Default to content as the subject ("the core reason is cost, about two hundred million a year"); do not write "someone said".
- Only when the source text itself names an identity — a self-introduction ("I am Ren Xin"), the host naming someone, an opening guest intro — may you attribute a view to that identity; otherwise do not infer or invent speakers, and do not use placeholders such as "Unknown" or "Speaker A".
- If the text uses `>>` or similar turn markers, you may use them to see turn-taking (for example Q&A rhythm), but a turn marker is not identity evidence and must not be copied into the notes.
- When a Q&A rhythm is visible, still fold guiding questions into the subsection title.

## Example

Input fragment:
[00:12:03 - 00:12:05] So what do you think was the core reason they made that decision?
[00:12:06 - 00:12:19] >> Um, I think it really comes down to cost. They ran the numbers; it saves about two hundred million a year.
[00:12:20 - 00:12:21] Two hundred million, that much.
[00:12:22 - 00:12:30] Right. And that does not even include headcount.

Output:
### [00:12:03] Core reason for the decision: cost
- The core is cost, **about two hundred million a year**, excluding headcount
"""


def build_notes_user_prompt(
    chapter_title: str,
    chapter_gist: str,
    chapter_text: str,
    previous_title: str = "",
    next_title: str = "",
) -> str:
    """Build the English per-chapter notes user prompt."""
    previous = previous_title or "None"
    following = next_title or "None"
    return "\n".join(
        [
            "Please write close-reading notes for this chapter from the transcript slice below.",
            "",
            f"Chapter title: {chapter_title}",
            f"Chapter gist: {chapter_gist}",
            f"Previous chapter title: {previous}",
            f"Next chapter title: {following}",
            "",
            "Original timestamped slice for this chapter:",
            "<chapter_transcript>",
            chapter_text,
            "</chapter_transcript>",
        ]
    )


NOTES_DENSITY_RETRY_PROMPT = (
    "Warning: your previous output was nearly as long as the source; that is "
    "copying, not notes. Stay within the 40%-60% length budget and apply the "
    "'must discard' list."
)


CHAPTERS_SYSTEM_PROMPT = f"""You are a professional chaptering assistant. Read the numbered transcript and split it at topic shifts. For each chapter write a title and a gist.

## Output language

Write every chapter title and gist in English, even when the transcript is in another language. Keep proper nouns in their original form.

## Splitting rules

1. **Cut on topic shifts**: start a new chapter whenever the subject actually changes; each chapter is usually one complete topic or segment
2. **Granularity**: each chapter should cover roughly 3-10 minutes; avoid cutting every sentence, and avoid a single chapter for the whole piece
3. **Cover the full text in number order**: chapters must start at the first number that appears in the body and cover through the last number, with no dropped topics in between. **Numbers themselves may have gaps** (e.g. 0, 2, 5, 6... missing numbers are normal and do not mean content was omitted) — follow the numbers that actually appear; do not try to "fill in" missing numbers
4. **Strictly increasing starts**: each chapter's starting number must be strictly greater than the previous chapter's; no repeats or reversals

## Output fields

- `title`: chapter title, at most {CHAPTER_TITLE_MAX_WORDS} words, no "Chapter N" prefix, just the topic of this stretch
- `gist`: 2-3 sentences on what this stretch actually covers and any highlight or conclusion worth hearing; no empty filler such as "introduces related content"
- `start_seg`: the starting number of this chapter in the numbered body (integer). **Use only a number that actually appears in the body** (the i inside each leading `[i]`); do not use a number that never appears, and do not invent or output timestamps — the program looks up times from the number, and any time you output is almost certainly wrong

## Output format

Return a JSON object of the form:
```
{{"chapters": [{{"title": "...", "gist": "...", "start_seg": 0}}, ...]}}
```
- Array sorted by start_seg ascending
- Return JSON only; no commentary and no markdown code fence"""


CHAPTERS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "chapters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": f"Chapter title, at most {CHAPTER_TITLE_MAX_WORDS} words, no ordinal prefix",
                    },
                    "gist": {
                        "type": "string",
                        "description": "Chapter gist, 2-3 sentences",
                    },
                    "start_seg": {
                        "type": "integer",
                        "description": "Starting number of this chapter in the numbered body",
                    },
                },
                "required": ["title", "gist", "start_seg"],
            },
        },
    },
    "required": ["chapters"],
}


def build_chapters_user_prompt(
    segment_lines: str,
    video_title: str = "",
    author: str = "",
    description: str = "",
    retry_hint: str = "",
) -> str:
    """Build the English chapters user prompt.

    Callers must flatten and truncate metadata the same way as the Chinese
    builder; this function does not re-truncate description.
    """
    parts = []

    if retry_hint:
        parts.append(f"**Previous output was invalid, please fix**: {retry_hint}\n")

    if video_title or author or description:
        parts.append("**Content metadata**:")
        if video_title:
            parts.append(f"- Title: {video_title}")
        if author:
            parts.append(f"- Author/channel: {author}")
        if description:
            parts.append(f"- Description: {description}")
        parts.append("")

    parts.append("**Numbered body** (split chapters using these numbers):")
    parts.append(segment_lines)

    return "\n".join(parts)
