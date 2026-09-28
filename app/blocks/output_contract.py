"""
app.blocks.output_contract — shared LLM response-contract helpers

The JSON-output roles append a canonical response contract to their resolved
system prompt (see each role's build_output_contract wrapper): a required
JSON object, optionally preceded by a Markdown lead section separated by an
hr. The helpers here render that contract and extract the JSON object back
out of the model's response.

Report blocks (report_writer.py) use extract_json_object to split the JSON
from the markdown lead so reports stay pure markdown.
"""

import json
from typing import Any


def extract_json_object(raw: str) -> dict | None:
    """Strip markdown fences / preamble and return the first JSON object, or None."""
    text = raw.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        if "\n" not in text:
            return None
        first_newline = text.index("\n")
        text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    # Try direct parse first
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    # Try to find the first { ... } block
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass

    return None


def build_output_contract(
    system_prompt: str, schema: dict[str, Any], *, markdown_example: str | None = None
) -> str:
    """Return a response contract to append to a system prompt.

    Any JSON object embedded in the prompt is merged over the canonical
    schema: official key names always remain (they can't be removed or
    renamed), while user values may redefine them and extra user keys
    pass through.

    When markdown_example is given, the contract asks for a Markdown section
    (a human-readable lead post) followed by an hr and the JSON object;
    otherwise it asks for the JSON object alone.
    """
    user_schema = extract_json_object(system_prompt) or {}
    merged = {**schema, **user_schema}
    if markdown_example is not None:
        return (
            "\n\nRespond with Markdown separated by an hr and then this JSON "
            "in this exact format.\n\n"
            + markdown_example.rstrip()
            + "\n\n---\n\n## JSON\n\n"
            + json.dumps(merged, indent=2)
        )
    return (
        "\n\nRespond with ONLY a JSON object (no markdown fences, no preamble) "
        "using this exact structure:\n\n" + json.dumps(merged, indent=2)
    )
