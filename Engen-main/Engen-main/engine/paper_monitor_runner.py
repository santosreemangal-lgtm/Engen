"""Paper-only monitoring for a public Solana wallet.

Reads public blockchain data and simulates trades.
Never signs or submits real Solana transactions.
"""

import argparse
import json
import logging
import math
import time
from contextlib import nullcontext
from urllib.parse import urlsplit
from decimal import Decimal, InvalidOperation

from engine.config import ConfigError, load_config
from engine.latency import LatencyRecorder
from engine.paper_trader import PaperTrader
from engine.signature_store import SignatureStore
from engine.solana_public import DEFAULT_RPC_URL
from engine.transaction_analysis import QUOTE_TOKEN_MINTS
from engine.transaction_monitor import monitor_wallet


logger = logging.getLogger(__name__)


def positive_decimal(value):
    """Validate a positive virtual starting balance."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError(
            "Paper balance must be a valid number."
        ) from exc

    if not amount.is_finite() or amount <= 0:
        raise argparse.ArgumentTypeError(
            "Paper balance must be greater than zero."
        )

    return str(amount)


def positive_timeout(value):
    """Validate a finite, positive RPC timeout in seconds."""
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "Timeout must be a valid number of seconds."
        ) from exc

    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError(
            "Timeout must be a finite number greater than zero."
        )

    return timeout


def build_parser():
    """Create the command-line argument parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Monitor a public Solana wallet using paper trading only."
        )
    )

    parser.add_argument(
        "--wallet",
        required=True,
        help="Public wallet address to monitor.",
    )

    parser.add_argument(
        "--quote-mint",
        required=True,
        choices=sorted(QUOTE_TOKEN_MINTS),
        help="Quote-token mint for the virtual starting balance.",
    )

    parser.add_argument(
        "--paper-balance",
        type=positive_decimal,
        default="100",
        help="Initial virtual balance for a new database; default: 100.",
    )

    parser.add_argument(
        "--paper-db",
        default="data/paper_trading.db",
        help="SQLite database for virtual balances and trade history.",
    )

    parser.add_argument(
        "--signature-db",
        default="data/signatures.db",
        help="SQLite database for wallet signature history.",
    )

    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Seconds between scans; minimum: 5.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum signatures per scan; valid range: 1-1000.",
    )

    parser.add_argument(
        "--rpc-url",
        default=DEFAULT_RPC_URL,
        help="Public Solana RPC URL.",
    )

    parser.add_argument(
        "--timeout",
        type=positive_timeout,
        default=10.0,
        help="Timeout for each RPC request in seconds; default: 10.",
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help="Perform one scan and exit.",
    )

    return parser


def validate_args(args, parser):
    """Validate command-line settings before initialization."""
    try:
        load_config()
    except ConfigError:
        # ConfigError contains only setting names and safe guidance, not
        # environment dumps or credential values.
        parser.error(
            "Unsafe engine configuration. Keep ENGINE_MODE=read_only and "
            "LIVE_TRADING_ENABLED=false."
        )

    if not args.wallet.strip():
        parser.error("--wallet cannot be empty.")

    if args.interval < 5:
        parser.error("--interval must be at least 5 seconds.")

    if not 1 <= args.limit <= 1000:
        parser.error("--limit must be between 1 and 1000.")

    if not args.paper_db.strip():
        parser.error("--paper-db cannot be empty.")

    if not args.signature_db.strip():
        parser.error("--signature-db cannot be empty.")

    try:
        parsed_rpc_url = urlsplit(args.rpc_url)
        # Accessing .port also validates malformed port values.
        port = parsed_rpc_url.port
    except ValueError:
        parser.error("--rpc-url is malformed.")

    if (
        parsed_rpc_url.scheme not in ("https", "http")
        or not parsed_rpc_url.hostname
        or any(character.isspace() for character in args.rpc_url)
        or parsed_rpc_url.username is not None
        or parsed_rpc_url.password is not None
        or parsed_rpc_url.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        parser.error(
            "--rpc-url must be a valid HTTP(S) URL without user credentials "
            "or a fragment."
        )


def run_once(args, trader, store, latency_recorder=None):
    """Scan the wallet and process available transactions in paper mode."""
    monitor_options = {
        "wallet_address": args.wallet,
        "store": store,
        "limit": args.limit,
        "rpc_url": args.rpc_url,
        "timeout": args.timeout,
        "defer_processed": True,
    }
    if latency_recorder is not None:
        monitor_options["latency_recorder"] = latency_recorder
    scan_timer = (
        latency_recorder.measure("paper.monitor_scan")
        if latency_recorder is not None
        else nullcontext()
    )
    with scan_timer:
        results = monitor_wallet(**monitor_options)

    if not isinstance(results, list):
        raise RuntimeError(
            "Wallet monitor returned an unexpected result."
        )

    logger.info(
        "Scan completed. Transactions returned: %d",
        len(results),
    )

    for result in results:
        if not isinstance(result, dict):
            logger.warning("Skipped an invalid transaction result.")
            continue

        signature = result.get("signature")

        # Keep unavailable transactions pending for retry.
        if result.get("available") is not True:
            logger.warning(
                "Transaction unavailable; leaving pending: %s",
                signature or "unknown",
            )
            continue

        # Process the transaction in paper mode first.
        # If processing raises an exception, it remains pending.
        decision_timer = (
            latency_recorder.measure("paper.local_decision")
            if latency_recorder is not None
            else nullcontext()
        )
        with decision_timer:
            paper_result = trader.process(result)

        # Mark processed only after paper processing returns.
        if isinstance(signature, str) and signature.strip():
            store.mark_processed(args.wallet, signature)

        logger.info(
            "Paper result: signature=%s status=%s",
            signature or "unknown",
            (
                paper_result.get("status", "UNKNOWN")
                if isinstance(paper_result, dict)
                else "UNKNOWN"
            ),
        )

        print(
            json.dumps(
                {
                    "observed_transaction": result,
                    "paper_result": paper_result,
                    "virtual_balances": trader.get_balances(),
                    "real_trading": False,
                },
                indent=2,
                default=str,
            )
        )

    logger.info("Virtual balances: %s", trader.get_balances())


def main(argv=None):
    """Run the paper-only monitoring loop."""
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(args, parser)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # Initialize persistence before starting any scan. If initialization
    # is interrupted or fails, exit without starting a background worker.
    try:
        store = SignatureStore(db_path=args.signature_db)
        trader = PaperTrader(
            {args.quote_mint: args.paper_balance},
            db_path=args.paper_db,
        )
    except KeyboardInterrupt:
        logger.info("Startup interrupted; monitoring was not started.")
        return 130
    except Exception:
        logger.error(
            "Paper monitor initialization failed; monitoring was not started."
        )
        return 1

    latency_recorder = LatencyRecorder()

    logger.info("Paper-only monitoring started.")
    logger.info("Quote mint: %s", args.quote_mint)
    logger.info("RPC timeout: %s seconds", args.timeout)
    logger.info(
        "Initial virtual balance for a new database: %s",
        args.paper_balance,
    )
    logger.info("Paper database: %s", args.paper_db)
    logger.info("No real trades will be submitted.")

    # Report the startup status expected by the existing tests.
    print(
        json.dumps(
            {
                "mode": "paper_trading_only",
                "wallet": args.wallet,
                "paper_database": args.paper_db,
                "real_trading": False,
                "wallet_signing": False,
                "virtual_balances": trader.get_balances(),
            },
            indent=2,
            default=str,
        )
    )

    exit_code = 0

    try:
        while True:
            try:
                run_once(args, trader, store, latency_recorder=latency_recorder)

            except KeyboardInterrupt:
                raise

            except Exception:
                logger.exception(
                    "Scan failed. No live trade was submitted."
                )
                exit_code = 1

            if args.once:
                break

            time.sleep(args.interval)

    except KeyboardInterrupt:
        logger.info("Paper monitoring stopped by user.")

    print(
        json.dumps(
            {
                "final_virtual_balances": trader.get_balances(),
                "paper_trades": trader.trades,
                "latency_summary": latency_recorder.summary(),
                "real_trading": False,
            },
            indent=2,
            default=str,
        )
    )

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())