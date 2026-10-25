"""Changing text: a list whose next item (a word, sentence, number or range) replaces {word} at each send.

The list lives on the job (``Job.word_list`` holds a JSON list of strings) with a pointer to the item the
next send uses (``Job.word_list_next``). When the list runs out it either starts again from the top
(``word_list_end == "repeat"``) or the message stops (``"stop"``). Skipped sends don't use up an item, and a
retry resends the item its failed send used.
"""
import json

TOKEN = "{word}"
MAX_ITEMS = 500
MAX_ITEM_LENGTH = 1000
END_MODES = {"repeat", "stop"}


def parse_items(raw: str) -> list[str]:
    """One item per line; blank lines are ignored."""
    return [line.strip() for line in (raw or "").splitlines() if line.strip()]


def validate(message: str, raw: str, end: str, next_index: int = 0) -> tuple[list[str], str, int]:
    """Check a submitted list against its message; returns (items, end, next index)."""
    items = parse_items(raw)
    if not items:
        if TOKEN in (message or ""):
            raise ValueError("Add at least one item to the changing text list, or remove {word} from your message")
        return [], "repeat", 0
    if TOKEN not in (message or ""):
        raise ValueError("Put {word} in your message where the changing text should go")
    if len(items) > MAX_ITEMS:
        raise ValueError(f"The changing text list can have up to {MAX_ITEMS} items")
    if any(len(item) > MAX_ITEM_LENGTH for item in items):
        raise ValueError(f"Each item in the changing text list can be up to {MAX_ITEM_LENGTH} characters")
    if end not in END_MODES:
        raise ValueError("Choose what happens when the list runs out")
    return items, end, min(max(next_index, 0), len(items) - 1)


def longest_message(message: str, items: list[str]) -> str:
    """The longest text this message can become, for length checks before saving."""
    return message.replace(TOKEN, max(items, key=lambda item: len(item.encode("utf-8")))) if items else message


def items(job) -> list[str]:
    try:
        value = json.loads(getattr(job, "word_list", None) or "[]")
    except (TypeError, ValueError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def finished(job) -> bool:
    """A list set to stop has used every item."""
    words = items(job)
    return bool(words) and job.word_list_end == "stop" and (job.word_list_next or 0) >= len(words)


def next_item(job) -> str | None:
    words = items(job)
    if not words or finished(job):
        return None
    return words[(job.word_list_next or 0) % len(words)]


def fill(job, text: str, retry: bool = False) -> str:
    """Put the item for this send into the text. A first send moves the pointer on; a retry reuses the last item."""
    words = items(job)
    if not words:
        return text
    position = job.word_list_next or 0
    if retry:
        position = (position - 1) % len(words)
    else:
        job.word_list_next = position + 1 if job.word_list_end == "stop" else (position + 1) % len(words)
    return text.replace(TOKEN, words[min(position, len(words) - 1)])
