from __future__ import annotations

import re
from datetime import datetime

from monitor.models import Alert
from monitor.rules import FIELD_GAP
from monitor.session import format_nz_clock

_CHANGE = re.compile(r"[+-]\d+(?:\.\d+)?%")
_PRICE = re.compile(r"\$\d[\d,.]*")

# Visual break between the live alert and older same-symbol history.
# En dashes (not ASCII "-"): same look, avoids Pushover dropping HTML on long "- -" runs.
HISTORY_DIVIDER = "– " * 27 + "–"


def _numbered_block(index: int, message: str, fired_at: datetime | None = None) -> str:
    """Put ``(#N)`` and NZ ``HH:MM`` on the headline like the Pushover title."""
    lines = message.strip().splitlines()
    clock = format_nz_clock(fired_at) if fired_at is not None else ""
    if not lines:
        return f"(#{index}){FIELD_GAP}{clock}".rstrip()
    first = lines[0]
    if re.search(r"\(#\d+\)", first):
        return message.strip()
    marker = _CHANGE.search(first) or _PRICE.search(first)
    stamp = f"{FIELD_GAP}{clock}" if clock else ""
    if marker is not None:
        name = first[: marker.start()].rstrip()
        first = f"{name}(#{index}){stamp}{first[len(name) :]}"
    else:
        first = f"{first.rstrip()}(#{index}){stamp}"
    return "\n".join([first, *lines[1:]])


def with_repeat_history(alert: Alert, prior: list[Alert]) -> Alert:
    """Stamp ``repeat_count`` and, from the 2nd ping, prepend history into a send copy.

    ``prior`` is same-symbol alerts earlier today (oldest first). The returned alert is
    what we deliver; the caller should still persist the original single-shot message.

    Merged body is newest-first. The live top line stays unnumbered (title already has
    ``SYMBOL(#N)``); older headlines get ``(#N)`` and NZ time. A divider separates live
    from history.
    """
    count = len(prior) + 1
    alert.repeat_count = count
    if not prior:
        return alert
    ordered = [alert, *reversed(prior)]
    top = ordered[0].message.strip()
    history = [
        _numbered_block(count - offset, item.message, item.fired_at)
        for offset, item in enumerate(ordered)
        if offset > 0
    ]
    message = top + "\n\n" + HISTORY_DIVIDER + "\n\n" + "\n\n".join(history)
    return alert.model_copy(update={"message": message, "repeat_count": count})
