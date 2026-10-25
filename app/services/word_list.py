"""Changing text: lists whose next items (words, sentences, numbers or ranges) replace {word}, {word2}… at each send.

``Job.word_list`` holds a JSON list of lists, one per changing part: the first list fills {word}, the second
{word2}, and so on. (Messages made before there could be several parts hold one flat list.) All the lists move
together: ``Job.word_list_next`` counts the sends since the start, and each list uses its item at that position,
starting over on its own when it is shorter than the longest list. When the longest list runs out, everything
starts again from the top (``word_list_end == "repeat"``) or the message stops (``"stop"``). Skipped sends don't
use up an item, and a retry resends the items its failed send used.
"""
import json
import re

from app.i18n import _

TOKEN = "{word}"
TOKENS = re.compile(r"\{word(\d*)\}")
MAX_LISTS = 5
MAX_ITEMS = 500
MAX_ITEM_LENGTH = 1000
END_MODES = {"repeat", "stop"}


def token(number: int) -> str:
    """{word} for the first part, then {word2}, {word3}…"""
    return TOKEN if number == 1 else "{word%d}" % number


def parse_items(raw: str) -> list[str]:
    """One item per line; blank lines are ignored."""
    return [line.strip() for line in (raw or "").splitlines() if line.strip()]


def used_parts(message: str) -> set[int]:
    """Which changing parts the message mentions ({word} and {word1} are both part 1)."""
    return {int(number or 1) for number in TOKENS.findall(message or "")} - {0}


def validate(message: str, raws: list[str], end: str, next_index: int = 0, lang: str = "en") -> tuple[list[list[str]], str, int]:
    """Check submitted lists (one raw text per part) against the message; returns (lists, end, next index)."""
    word_lists = [parse_items(raw) for raw in raws]
    while word_lists and not word_lists[-1] and len(word_lists) not in used_parts(message):
        word_lists.pop()
    used = used_parts(message)
    if not word_lists:
        if used:
            raise ValueError(_("Add at least one item to the changing text list, or remove {word} from your message", lang))
        return [], "repeat", 0
    if len(word_lists) > MAX_LISTS:
        raise ValueError(_("A message can have up to 5 changing parts", lang))
    for number, items in enumerate(word_lists, start=1):
        if not items and len(word_lists) == 1:
            raise ValueError(_("Add at least one item to the changing text list, or remove {word} from your message", lang))
        if not items:
            raise ValueError(_("Add at least one item to changing part {number}, or remove {token} from your message", lang)
                             .replace("{number}", str(number)).replace("{token}", token(number)))
        if number not in used:
            raise ValueError(_("Put {token} in your message where changing part {number} should go", lang)
                             .replace("{number}", str(number)).replace("{token}", token(number)))
        if len(items) > MAX_ITEMS:
            raise ValueError(_("The changing text list can have up to 500 items", lang))
        if any(len(item) > MAX_ITEM_LENGTH for item in items):
            raise ValueError(_("Each item in the changing text list can be up to 1000 characters", lang))
    extra = sorted(number for number in used if number > len(word_lists))
    if extra:
        raise ValueError(_("Your message uses {token} but has no list for it. Add another changing part or remove it", lang)
                         .replace("{token}", token(extra[0])))
    if end not in END_MODES:
        raise ValueError(_("Choose what happens when the list runs out", lang))
    return word_lists, end, min(max(next_index, 0), max(len(items) for items in word_lists) - 1)


def substitute(text: str, word_lists: list[list[str]], step: int) -> str:
    def item(match):
        number = int(match[1] or 1)
        if not 1 <= number <= len(word_lists):
            return match[0]
        items = word_lists[number - 1]
        return items[step % len(items)]
    return TOKENS.sub(item, text)


def longest_message(message: str, word_lists: list[list[str]]) -> str:
    """The longest text this message can become, for length checks before saving."""
    longest = [[max(items, key=lambda value: len(value.encode("utf-8")))] for items in word_lists]
    return substitute(message, longest, 0) if word_lists else message


def lists(job) -> list[list[str]]:
    try:
        value = json.loads(getattr(job, "word_list", None) or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list) or not value:
        return []
    if not isinstance(value[0], list):
        value = [value]  # one flat list from before there could be several parts
    return [[str(item) for item in items] for items in value if isinstance(items, list) and items]


def dump(word_lists: list[list[str]]) -> str | None:
    return json.dumps(word_lists, ensure_ascii=False) if word_lists else None


def length(job) -> int:
    """How many sends one pass through the lists takes: the length of the longest list."""
    return max((len(items) for items in lists(job)), default=0)


def finished(job) -> bool:
    """Lists set to stop have sent every item of the longest one."""
    total = length(job)
    return bool(total) and job.word_list_end == "stop" and (job.word_list_next or 0) >= total


def next_items(job) -> list[str]:
    """The items the next send uses, one per changing part."""
    word_lists = lists(job)
    if not word_lists or finished(job):
        return []
    step = job.word_list_next or 0
    return [items[step % len(items)] for items in word_lists]


def fill(job, text: str, retry: bool = False) -> str:
    """Put this send's items into the text. A first send moves on a step; a retry reuses the last step's items."""
    word_lists = lists(job)
    if not word_lists:
        return text
    total = max(len(items) for items in word_lists)
    step = job.word_list_next or 0
    if retry:
        step = max(step - 1, 0) if job.word_list_end == "stop" else (step - 1) % total
    else:
        job.word_list_next = step + 1 if job.word_list_end == "stop" else (step + 1) % total
    return substitute(text, word_lists, step)
