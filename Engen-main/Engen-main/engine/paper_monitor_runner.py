"""Paper-only monitoring for a public Solana wallet.

Reads public blockchain data and simulates trades.
Never signs or submits real Solana transactions.
"""

import argparse
import json
import logging
import math
import os
import time
import uuid
from pathlib import Path
from contextlib import nullcontext
from urllib.parse import urlsplit
from decimal import Decimal, InvalidOperation

from engine.config import ConfigError, load_config
from engine.profile_store import ProfileStore
from engine.latency import LatencyRecorder
from engine.paper_trader import PaperTrader
from engine.signature_store import SignatureStore
from engine.solana_public import DEFAULT_RPC_URL
from engine.solana_validation import is_valid_solana_address
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
        "--profile-id",
        help=(
            "Run against an active saved profile. The monitored wallet must be "
            "associated with that profile; profile-specific databases and paper risk settings are used."
        ),
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help="Perform one scan and exit.",
    )

    return parser


def configure_profile_args(args, parser):
    """Resolve an existing active profile into isolated paper-only storage and risk limits."""
    args.profile_risk_limits = {}
    if not args.profile_id:
        return

    try:
        profile_id = str(uuid.UUID(args.profile_id.strip()))
    except (AttributeError, ValueError, TypeError):
        parser.error("--profile-id must be a valid profile UUID.")
    args.profile_id = profile_id

    if args.paper_db != "data/paper_trading.db" or args.signature_db != "data/signatures.db":
        parser.error("Do not combine --profile-id with custom --paper-db or --signature-db paths.")

    profile_store_path = Path(os.environ.get("PROFILE_STORE_DB", "data/profiles.db"))
    if not profile_store_path.is_file():
        parser.error("The profile database does not exist; create a profile in the dashboard first.")

    try:
        profile = ProfileStore(profile_store_path).get_profile(profile_id)
    except (OSError, RuntimeError, ValueError):
        parser.error("The requested profile could not be loaded safely.")
    if profile is None:
        parser.error("The requested profile does not exist.")
    if profile.get("status") != "active":
        parser.error("Activate the profile in the dashboard before starting its monitor.")
    if args.wallet not in profile.get("source_wallets", []):
        parser.error("The monitored public wallet must be associated with this profile first.")

    settings = profile.get("settings", {})
    paper_settings = settings.get("paper_trading", {}) if isinstance(settings, dict) else {}
    if not isinstance(paper_settings, dict):
        parser.error("Profile paper_trading settings must be a JSON object.")
    allowed = {"initial_quote_balance", "max_position_pct", "max_open_positions"}
    unknown = set(paper_settings) - allowed
    if unknown:
        parser.error("Unsupported paper_trading setting(s): " + ", ".join(sorted(unknown)))

    if "initial_quote_balance" in paper_settings:
        try:
            args.paper_balance = positive_decimal(str(paper_settings["initial_quote_balance"]))
        except argparse.ArgumentTypeError as exc:
            parser.error("Invalid profile initial_quote_balance: " + str(exc))

    if "max_position_pct" in paper_settings:
        try:
            percentage = Decimal(str(paper_settings["max_position_pct"]))
        except Exception:
            parser.error("Profile max_position_pct must be a number greater than 0 and at most 100.")
        if not percentage.is_finite() or percentage <= 0 or percentage > 100:
            parser.error("Profile max_position_pct must be a number greater than 0 and at most 100.")
        args.profile_risk_limits["max_position_pct"] = str(percentage)

    if "max_open_positions" in paper_settings:
        count = paper_settings["max_open_positions"]
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 100:
            parser.error("Profile max_open_positions must be an integer from 1 to 100.")
        args.profile_risk_limits["max_open_positions"] = count

    configured_root = os.environ.get("PROFILE_DATA_DIR")
    default_root = profile_store_path.parent / "profiles"
    profile_root = Path(configured_root) if configured_root else default_root
    profile_dir = profile_root / profile_id
    args.paper_db = str(profile_dir / "paper_trading.db")
    args.signature_db = str(profile_dir / "signatures.db")


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

    if not is_valid_solana_address(args.wallet):
        parser.error("--wallet must be a valid Solana public address.")

    if args.interval < 5:
        parser.error("--interval must be at least 5 seconds.")

    if not 1 <= args.limit <= 1000:
        parser.error("--limit must be between 1 and 1000.")

    configure_profile_args(args, parser)

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
        try:
            with decision_timer:
                paper_result = trader.process(result)
        except Exception as exc:
            if (
                isinstance(signature, str)
                and signature.strip()
                and store.get_status(args.wallet, signature) == "pending"
            ):
                store.record_failure(
                    args.wallet,
                    signature,
                    "paper_simulation",
                    type(exc).__name__,
                    "Paper simulation failed; the signature remains recoverable.",
                )
            raise

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
            **args.profile_risk_limits,
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