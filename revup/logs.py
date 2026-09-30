from __future__ import annotations

import logging
import os
import re
import sys
import time
from typing import TYPE_CHECKING, Dict, List, Optional, Type, TypeVar

from rich.logging import RichHandler
from rich.text import Text

if TYPE_CHECKING:
    # Importing rich.traceback costs 100ms, and rich only needs it for tracebacks, which
    # revup doesn't enable.
    from rich.console import ConsoleRenderable
    from rich.traceback import Traceback

from revup.version import REVUP_VERSION

HandlerType = TypeVar("HandlerType", bound=logging.Handler)


class RedactingFilter(logging.Filter):
    redactions: Dict[str, str]

    def __init__(self) -> None:
        super().__init__()
        self.redactions = {}

    # Remove sensitive information from URLs
    def _filter(self, s: str) -> str:
        s = re.sub(r":\/\/(.*?)\@", r"://<USERNAME>:<PASSWORD>@", s)
        for needle, replace in self.redactions.items():
            s = s.replace(needle, replace)
        return s

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._filter(record.msg)
        return True

    # Redact specific strings; e.g., authorization tokens.  This won't
    # retroactively redact stuff you've already leaked, so make sure
    # you redact things as soon as possible
    def redact(self, needle: str, replace: str = "<REDACTED>") -> None:
        # Don't redact empty strings; this will lead to something
        # that looks like s<REDACTED>t<REDACTED>r<REDACTED>...
        if needle == "":
            return
        self.redactions[needle] = replace


LEVEL_PREFIXES = {
    "WARNING": Text.styled("W: ", style="bold yellow"),
    "ERROR": Text.styled("E: ", style="bold red"),
}


class RevupRichHandler(RichHandler):
    def render(
        self,
        *,
        record: logging.LogRecord,
        traceback: Optional[Traceback],
        message_renderable: ConsoleRenderable,
    ) -> ConsoleRenderable:
        """
        Render the message on its own instead of in a column layout, so that every line of a
        multiline message starts at the left margin.
        """
        prefix = LEVEL_PREFIXES.get(record.levelname)
        if prefix is None:
            return message_renderable
        assert isinstance(message_renderable, Text)
        return prefix + message_renderable


def find_handler(handler_type: Type[HandlerType]) -> Optional[HandlerType]:
    """
    Find an already installed handler of the given type.
    """
    for h in logging.getLogger().handlers:
        if isinstance(h, handler_type):
            return h
    return None


def get_log_filter() -> RedactingFilter:
    """
    The filter shared by all handlers, so redactions added at any point apply to all
    backends. Must be called after configure_logger().
    """
    for h in logging.getLogger().handlers:
        for f in h.filters:
            if isinstance(f, RedactingFilter):
                return f
    raise AssertionError("Logger must be configured before redacting")


def prune_old_logs(log_dir: str, keep: int) -> None:
    """
    Delete all but the newest `keep` log files. Names sort chronologically so we can
    prune without stat-ing every file.
    """
    try:
        names: List[str] = sorted(n for n in os.listdir(log_dir) if n.endswith(".log"))
    except OSError:
        return

    for name in names[: max(len(names) - keep, 0)]:
        try:
            os.remove(os.path.join(log_dir, name))
        except OSError:
            # Another revup may have pruned it already, or we may not own it.
            pass


def make_console_handler(log_filter: logging.Filter) -> RichHandler:
    handler = RevupRichHandler(keywords=[])
    handler.addFilter(log_filter)
    handler.highlighter = None  # type: ignore
    return handler


def make_file_handler(log_filter: logging.Filter) -> Optional[logging.FileHandler]:
    """
    Create a handler that writes debug logs for this invocation to a new file. Returns
    None if the file couldn't be created, since logging is never worth failing over.
    """
    # Follow the XDG base directory spec for state that should persist between restarts
    # but isn't precious.
    state_home = os.environ.get("XDG_STATE_HOME")
    if not state_home:
        state_home = os.path.join(os.path.expanduser("~"), ".local", "state")
    log_dir = os.path.join(state_home, "revup", "logs")

    try:
        os.makedirs(log_dir, exist_ok=True)
        # Keep 19 old logs plus the one we're about to add, for 20 runs of history.
        prune_old_logs(log_dir, 19)
        path = os.path.join(
            log_dir, "{}-{}.log".format(time.strftime("%Y%m%dT%H%M%S"), os.getpid())
        )
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError as e:
        logging.warning(f"Couldn't write verbose logs to {log_dir}: {e}")
        return None

    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    handler.addFilter(log_filter)
    return handler


def configure_logger(debug: bool, redactions: Dict[str, str], log_to_file: bool = False) -> None:
    """
    Set up logging backends. Can be called more than once to update options, which is
    needed since some logging happens before config and args are fully parsed.

    Args:
        debug: Show debug logs on the console.
        redactions: Strings that must never appear in any log, and their replacements.
        log_to_file: Additionally write debug logs to a file in the state directory,
            regardless of what the console shows.
    """
    root_logger = logging.getLogger()
    # The root level must allow debug logs through if any handler wants them; each
    # handler then decides for itself what to show.
    root_logger.setLevel(logging.DEBUG if debug or log_to_file else logging.INFO)

    console_handler = find_handler(RichHandler)
    if console_handler is None:
        console_handler = make_console_handler(RedactingFilter())
        root_logger.addHandler(console_handler)
    console_handler.setLevel(logging.DEBUG if debug else logging.INFO)

    log_filter = get_log_filter()
    for k, v in redactions.items():
        log_filter.redact(k, v)

    if log_to_file and find_handler(logging.FileHandler) is None:
        file_handler = make_file_handler(log_filter)
        if file_handler is not None:
            root_logger.addHandler(file_handler)
            # Record what was run, since the file outlives the terminal it came from.
            logging.debug("revup {} : {}".format(REVUP_VERSION, " ".join(sys.argv[1:])))


def redact(redactions: Dict[str, str]) -> None:
    log_filter = get_log_filter()
    for k, v in redactions.items():
        log_filter.redact(k, v)
