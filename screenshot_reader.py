"""Read missing answers off a screenshot of a practice exam question, using Claude.

A saved results page leaves some answers out (see scrape_results.learndash_answers),
but it does keep every gap's list of choices. Claude is shown the screenshot and
asked to pick, for each gap, one of those choices, so the answer is always one the
question really offered, or "not visible" when the screenshot doesn't show it.

Needs ANTHROPIC_API_KEY in the environment. On Azure App Service that's an app
setting referencing the Key Vault secret.
"""

import base64
import json
import os

import anthropic

MODEL = "claude-opus-5-5"
NOT_VISIBLE = "not visible"
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
MAX_IMAGES = 4

SYSTEM = """\
You read screenshots of practice exam questions that have already been answered and \
graded. For each gap you're asked about, find it in the screenshot and report which of \
its listed choices applies, copying the choice exactly. If the screenshot doesn't show \
it clearly, answer "not visible" rather than guessing.

How the grading screen shows answers:
- Dropdown questions: the choice shown inside the dropdown box is the one the test \
taker picked. The correct answer appears next to it in green, in parentheses. When \
asked for "your pick", report what's in the box, never the green text.
- Drag-to-match questions: each row shows the item the test taker placed there. For \
"correct match", report the item that belongs in that row according to the screenshot \
or the explanation, which often lists the right order."""


class ReaderUnavailable(RuntimeError):
    """Screenshot reading isn't configured or the API couldn't be reached."""


def is_configured():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def read_gaps(images, gap):
    """Pick an answer for each of a question's gaps from its screenshots.

    images is a list of (bytes, media_type); gap is one entry of /api/parse's "gaps".
    Returns {position in gap["slots"]: choice}, leaving out slots the screenshots
    don't show.
    """
    if not is_configured():
        raise ReaderUnavailable("Screenshot reading isn't set up on this server.")
    slots = gap["slots"]
    keys = [f"slot_{i}" for i in range(len(slots))]
    schema = {
        "type": "object",
        "properties": {k: {"type": "string", "enum": [*s["choices"], NOT_VISIBLE]}
                       for k, s in zip(keys, slots)},
        "required": keys,
        "additionalProperties": False,
    }

    lines = [f"Question {gap['number']}:", gap["question"], ""]
    if any(s["field"] == "correct_answer" for s in slots) and gap.get("explanation"):
        lines += ["Explanation from the results page:", gap["explanation"], ""]
    lines.append("Gaps to fill:")
    for k, s in zip(keys, slots):
        who = "your pick" if s["field"] == "your_answer" else "correct match"
        where = f"row {s['prompt']}" if gap.get("type") == "matrix_sort_answer" else s["prompt"]
        lines.append(f"- {k}: {who} for {where!r}. Choices: {json.dumps(s['choices'])}")

    content = [{"type": "image", "source": {"type": "base64", "media_type": media_type,
                                            "data": base64.standard_b64encode(data).decode()}}
               for data, media_type in images]
    content.append({"type": "text", "text": "\n".join(lines)})

    try:
        response = anthropic.Anthropic().beta.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=SYSTEM,
            messages=[{"role": "user", "content": content}],
            # Reading a dropdown is simple; low effort keeps it quick and cheap.
            output_config={"effort": "low",
                           "format": {"type": "json_schema", "schema": schema}},
            # If a safety classifier declines, retry on Anthropic's recommended model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as e:
        raise ReaderUnavailable("The server's Anthropic API key was rejected.") from e
    except anthropic.RateLimitError as e:
        raise ReaderUnavailable("Too many screenshot requests right now. Try again in a minute.") from e
    except anthropic.BadRequestError as e:
        raise ValueError(f"The screenshot couldn't be read: {e.message}") from e
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
        raise ReaderUnavailable("Couldn't reach the screenshot reader. Try again.") from e

    if response.stop_reason == "refusal":
        raise ValueError("The screenshot reader declined this image.")
    if response.stop_reason == "max_tokens":
        raise ReaderUnavailable("The screenshot reader ran out of room. Try again.")
    text = next(b.text for b in response.content if b.type == "text")
    picks = json.loads(text)
    return {i: picks[k] for i, (k, slot) in enumerate(zip(keys, slots))
            if picks.get(k) in slot["choices"]}
