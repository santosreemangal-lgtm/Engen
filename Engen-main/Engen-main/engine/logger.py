"""Console logging that hides secret-looking values.

If a message contains something like  api_key=abc123  or  token: xyz,
the value is replaced with [REDACTED] before it is printed.
"""
import logging
import re
import sys

_SECRET_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|private[_-]?key|seed(?:[_ -]?phrase)?|password)"
    r"(\s*[=:]\s*)(\S+)"
)


def redact(text):
    """Returns text with secret-looking values replaced."""
    return _SECRET_PATTERN.sub(r"\1\2[REDACTED]", text)


class RedactingFilter(logging.Filter):
    def filter(self, record):
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
        except Exception:
            record.msg = "[log message could not be formatted]"
            record.args = ()
        return True


def setup_logging(level="INFO"):
    """Sets up the 'engine' logger. Safe to call more than once."""
    logger = logging.getLogger("engine")
    logger.setLevel(str(level).upper())
    if not any(getattr(h, "_engine_handler", False) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler._engine_handler = True
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
            )
        )
        handler.addFilter(RedactingFilter())
        logger.addHandler(handler)
    logger.propagate = False
    return logger
