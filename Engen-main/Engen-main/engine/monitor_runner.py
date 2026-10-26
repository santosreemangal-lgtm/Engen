
"""Read-only runner for monitoring a public Solana wallet."""

import argparse
import json
import logging
import time

from engine.solana_public import DEFAULT_RPC_URL
from engine.signature_store import SignatureStore
from engine.transaction_monitor import monitor_wallet


def main():
    parser = argparse.ArgumentParser(
        description="Monitor a public Solana wallet without trading."
    )
    parser.add_argument(
        "--wallet",
        required=True,
        help="Public Solana wallet address to monitor.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Seconds between scans (minimum 5).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum signatures fetched per scan (1-1000).",
    )

    args = parser.parse_args()

    if args.interval < 5:
        parser.error("--interval must be at least 5 seconds.")

    if not 1 <= args.limit <= 1000:
        parser.error("--limit must be between 1 and 1000.")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("engine.monitor_runner")

    store = SignatureStore()

    log.info("Starting read-only wallet monitoring.")
    log.info("No trades or wallet signing will be performed.")

    try:
        while True:
            try:
                results = monitor_wallet(
                    wallet_address=args.wallet,
                    store=store,
                    limit=args.limit,
                    rpc_url=DEFAULT_RPC_URL,
                )

                for result in results:
                    print(json.dumps(result, indent=2, default=str))

                log.info(
                    "Scan complete. New transactions analyzed: %s",
                    len(results),
                )

            except Exception:
                log.exception("Wallet scan failed; will retry.")

            time.sleep(args.interval)

    except KeyboardInterrupt:
        log.info("Monitoring stopped by user.")


if __name__ == "__main__":
    main()
