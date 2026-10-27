"""Console logging that hides secret-looking values.

Common credential assignments and HTTP bearer tokens are redacted before
messages are written to the console. This is a defensive filter, not a
substitute for never logging credentials in the first place.
"""
import logging
import re
import sys

_SECRET_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|private[_-]?key|seed(?:[_ -]?phrase)?|password)"
    r"(\s*[=:]\s*)([^\s,;&]+)"
)
_BEARER_PATTERN = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]+")
_URL_CREDENTIAL_PATTERN = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|auth|token|secret|password)=)[^&#\s]+"
)


def redact(text):
    """Returns text with common secret-looking values replaced."""
    text = _SECRET_PATTERN.sub(r"\1\2[REDACTED]", str(text))
    text = _BEARER_PATTERN.sub(r"\1[REDACTED]", text)
    return _URL_CREDENTIAL_PATTERN.sub(r"\1[REDACTED]", text)


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
