"""Stage 1 entry point: starts the app and runs a health check.

It does NOT connect to Solana, use a wallet, or place trades.
Exit code 0 = healthy, 1 = a problem was found.
"""
import json
import logging
import sys
import time

from engine.config import ConfigError, load_config
from engine.health import get_status, run_checks
from engine.logger import setup_logging


def main():
    started = time.perf_counter()

    try:
        config = load_config()
    except ConfigError as exc:
        print("CONFIG ERROR:\n%s" % exc)
        return 1

    setup_logging(config.log_level)
    log = logging.getLogger("engine.main")

    try:
        log.info("Starting solana-copy-engine (Stage 1, read-only)")
        all_ok = True
        for result in run_checks(config):
            if result.passed:
                log.info("PASS | %s (%s)", result.name, result.detail)
            else:
                log.error("FAIL | %s (%s)", result.name, result.detail)
                all_ok = False

        status = get_status(config)
        status["startup_ms"] = round((time.perf_counter() - started) * 1000, 2)
        status["status"] = "ok" if all_ok else "failed"
        print(json.dumps(status, indent=2))
        log.info("Health check %s", "passed" if all_ok else "FAILED")
        return 0 if all_ok else 1
    except Exception:
        log.exception("Unexpected error during startup")
        return 1


if __name__ == "__main__":
    sys.exit(main())
