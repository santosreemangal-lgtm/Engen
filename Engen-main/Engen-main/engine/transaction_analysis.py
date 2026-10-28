"""Read-only transaction analysis helpers for public Solana data."""

from collections.abc import Mapping
import re


WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"
PUMP_FUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
NATIVE_SOL_SOURCE = "native_sol"

# Recognized SPL quote-token mints.
QUOTE_TOKEN_MINTS = frozenset({
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    WRAPPED_SOL_MINT,
})


def analyze_transaction(transaction):
    """Return a normalized transaction summary, including signer keys."""

    if not isinstance(transaction, Mapping):
        raise ValueError("Transaction data must be a mapping.")

    meta = transaction.get("meta")

    if not isinstance(meta, Mapping):
        return {
            "available": False,
            "error": "Transaction metadata is unavailable.",
        }

    error = meta.get("err")
    status = "failed" if error is not None else "success"

    transaction_data = transaction.get("transaction", {})

    if not isinstance(transaction_data, Mapping):
        transaction_data = {}

    message_data = transaction_data.get("message", {})

    if not isinstance(message_data, Mapping):
        message_data = {}

    account_keys = message_data.get("accountKeys", [])

    if not isinstance(account_keys, list):
        account_keys = []

    def normalize_pubkey(item):
        if isinstance(item, str) and item:
            return item

        if isinstance(item, Mapping):
            pubkey = item.get("pubkey")
            if isinstance(pubkey, str) and pubkey:
                return pubkey

        return None

    signers = []
    header = message_data.get("header")

    if isinstance(header, Mapping):
        required = header.get("numRequiredSignatures")

        if (
            isinstance(required, int)
            and not isinstance(required, bool)
            and 0 <= required <= len(account_keys)
        ):
            signers = [
                normalize_pubkey(item)
                for item in account_keys[:required]
            ]
            signers = [pubkey for pubkey in signers if pubkey]

    # Support parsed account keys that explicitly include signer flags.
    # If the header did not produce signer keys, use those flags.
    if not signers:
        signers = [
            pubkey
            for item in account_keys
            if isinstance(item, Mapping)
            and item.get("signer") is True
            for pubkey in [normalize_pubkey(item)]
            if pubkey
        ]

    # Preserve instruction-level context without assuming an instruction is
    # a swap. Both parsed and compiled instructions are represented when possible.
    raw_instructions = message_data.get("instructions", [])
    if not isinstance(raw_instructions, list):
        raw_instructions = []
    normalized_instructions = []
    program_ids = []
    for index, instruction in enumerate(raw_instructions):
        if not isinstance(instruction, Mapping):
            continue
        program_id = instruction.get("programId")
        program_id_index = instruction.get("programIdIndex")
        if (
            not isinstance(program_id, str)
            and isinstance(program_id_index, int)
            and not isinstance(program_id_index, bool)
            and 0 <= program_id_index < len(account_keys)
        ):
            program_id = normalize_pubkey(account_keys[program_id_index])
        if isinstance(program_id, str) and program_id:
            program_ids.append(program_id)
        parsed = instruction.get("parsed")
        parsed_type = (
            parsed.get("type") if isinstance(parsed, Mapping) else None
        )
        normalized_instructions.append({
            "index": index,
            "program_id": program_id if isinstance(program_id, str) else None,
            "program": instruction.get("program"),
            "parsed_type": parsed_type if isinstance(parsed_type, str) else None,
            "stack_height": instruction.get("stackHeight"),
            "source": "message",
        })

    # Inner instructions live in transaction metadata and are grouped by the
    # outer instruction index. Keep that context instead of flattening it away.
    inner_groups = meta.get("innerInstructions", [])
    if isinstance(inner_groups, list):
        for group in inner_groups:
            if not isinstance(group, Mapping):
                continue
            outer_index = group.get("index")
            inner_items = group.get("instructions", [])
            if not isinstance(inner_items, list):
                continue
            for inner_index, instruction in enumerate(inner_items):
                if not isinstance(instruction, Mapping):
                    continue
                program_id = instruction.get("programId")
                program_id_index = instruction.get("programIdIndex")
                if (
                    not isinstance(program_id, str)
                    and isinstance(program_id_index, int)
                    and not isinstance(program_id_index, bool)
                    and 0 <= program_id_index < len(account_keys)
                ):
                    program_id = normalize_pubkey(account_keys[program_id_index])
                if isinstance(program_id, str) and program_id:
                    program_ids.append(program_id)
                parsed = instruction.get("parsed")
                parsed_type = (
                    parsed.get("type") if isinstance(parsed, Mapping) else None
                )
                normalized_instructions.append({
                    "index": outer_index,
                    "inner_index": inner_index,
                    "program_id": program_id if isinstance(program_id, str) else None,
                    "program": instruction.get("program"),
                    "parsed_type": parsed_type if isinstance(parsed_type, str) else None,
                    "stack_height": instruction.get("stackHeight"),
                    "source": "inner",
                })

    return {
        "available": True,
        "status": status,
        "slot": transaction.get("slot"),
        "fee": meta.get("fee"),
        "account_count": len(account_keys),
        "signers": signers,
        "program_ids": sorted(set(program_ids)),
        "instructions": normalized_instructions,
        "error": error,
    }


def analyze_token_balance_changes(meta):
    """Extract exact SPL-token balance changes from transaction metadata."""

    if not isinstance(meta, Mapping):
        return []

    pre_balances = meta.get("preTokenBalances", [])
    post_balances = meta.get("postTokenBalances", [])

    if not isinstance(pre_balances, list):
        pre_balances = []

    if not isinstance(post_balances, list):
        post_balances = []

    def normalize_balances(balances):
        normalized = {}

        for item in balances:
            if not isinstance(item, Mapping):
                continue

            mint = item.get("mint")
            account_index = item.get("accountIndex")
            amount_data = item.get("uiTokenAmount")

            if (
                not isinstance(mint, str)
                or not mint
                or not isinstance(account_index, int)
                or isinstance(account_index, bool)
                or not isinstance(amount_data, Mapping)
            ):
                continue

            raw_amount = amount_data.get("amount")
            decimals = amount_data.get("decimals")

            if (
                not isinstance(raw_amount, str)
                or not raw_amount.isdigit()
                or not isinstance(decimals, int)
                or isinstance(decimals, bool)
                or decimals < 0
            ):
                continue

            owner = item.get("owner")
            key = (account_index, mint, owner)

            normalized[key] = {
                "account_index": account_index,
                "mint": mint,
                "owner": owner,
                "amount": int(raw_amount),
                "decimals": decimals,
            }

        return normalized

    before = normalize_balances(pre_balances)
    after = normalize_balances(post_balances)

    changes = []

    for key in before.keys() | after.keys():
        old = before.get(key)
        new = after.get(key)

        old_amount = old["amount"] if old else 0
        new_amount = new["amount"] if new else 0

        decimals = new["decimals"] if new else old["decimals"]

        if old and new and old["decimals"] != new["decimals"]:
            continue

        delta = new_amount - old_amount

        if delta == 0:
            continue

        changes.append({
            "account_index": key[0],
            "mint": key[1],
            "owner": key[2],
            "decimals": decimals,
            "pre_amount": str(old_amount),
            "post_amount": str(new_amount),
            "delta_amount": str(delta),
        })

    return sorted(
        changes,
        key=lambda item: (item["account_index"], item["mint"]),
    )


def analyze_native_sol_balance_changes(transaction):
    """Extract native SOL balance changes, expressed in lamports.

    For the fee payer (account index zero), the network transaction fee is
    added back to the raw balance delta. This estimates the movement excluding
    the network fee, but does not remove rent, transfers, or other SOL uses.

    This is a heuristic input for classification, not proof of a swap.
    """

    if not isinstance(transaction, Mapping):
        return []

    meta = transaction.get("meta")

    if not isinstance(meta, Mapping):
        return []

    pre_balances = meta.get("preBalances")
    post_balances = meta.get("postBalances")

    if (
        not isinstance(pre_balances, list)
        or not isinstance(post_balances, list)
        or len(pre_balances) != len(post_balances)
    ):
        return []

    transaction_data = transaction.get("transaction")

    if not isinstance(transaction_data, Mapping):
        return []

    message = transaction_data.get("message")

    if not isinstance(message, Mapping):
        return []

    raw_keys = message.get("accountKeys")

    if not isinstance(raw_keys, list):
        return []

    def normalize_key(item):
        if isinstance(item, str) and item:
            return item

        if isinstance(item, Mapping):
            pubkey = item.get("pubkey")
            if isinstance(pubkey, str) and pubkey:
                return pubkey

        return None

    # Preserve positions: account indexes must align with pre/post balances.
    account_keys = [normalize_key(item) for item in raw_keys]

    # For versioned transactions, loaded addresses follow static keys:
    # writable addresses first, then read-only addresses.
    loaded_addresses = meta.get("loadedAddresses")

    if isinstance(loaded_addresses, Mapping):
        for group in ("writable", "readonly"):
            values = loaded_addresses.get(group, [])

            if isinstance(values, list):
                account_keys.extend(normalize_key(item) for item in values)

    fee = meta.get("fee")
    valid_fee = (
        isinstance(fee, int)
        and not isinstance(fee, bool)
        and fee >= 0
    )

    changes = []
    count = min(len(account_keys), len(pre_balances))

    for index in range(count):
        owner = account_keys[index]
        before = pre_balances[index]
        after = post_balances[index]

        if (
            not isinstance(owner, str)
            or not isinstance(before, int)
            or isinstance(before, bool)
            or before < 0
            or not isinstance(after, int)
            or isinstance(after, bool)
            or after < 0
        ):
            continue

        raw_delta = after - before
        adjusted_delta = raw_delta
        fee_adjusted = False

        # The first account is the transaction fee payer. Without a valid fee
        # value, skip this account rather than treating the fee as swap spend.
        if index == 0:
            if not valid_fee:
                continue

            adjusted_delta += fee
            fee_adjusted = True

        if adjusted_delta == 0:
            continue

        changes.append({
            "account_index": index,
            "mint": WRAPPED_SOL_MINT,
            "owner": owner,
            "decimals": 9,
            "pre_amount": str(before),
            "post_amount": str(after),
            "delta_amount": str(adjusted_delta),
            "raw_delta_amount": str(raw_delta),
            "fee_adjusted": fee_adjusted,
            "source": NATIVE_SOL_SOURCE,
        })

    return changes


def classify_token_balance_changes(token_balance_changes, wallet_address):
    """Conservatively classify a wallet's balance deltas.

    This is a heuristic, not proof of a swap. Native SOL movements can include
    rent, transfers, and other uses. Native SOL is not combined with wrapped
    SOL or other SPL quote-token movements.
    """

    def unknown(reason):
        return {
            "classification": "UNKNOWN",
            "reason": reason,
            "asset_mints": [],
            "quote_mints": [],
        }

    if not isinstance(wallet_address, str) or not wallet_address.strip():
        return unknown("A monitored wallet address is required.")

    if not isinstance(token_balance_changes, list):
        return unknown("Token balance changes are unavailable.")

    token_deltas = {}
    native_sol_delta = 0
    native_sol_seen = False

    for change in token_balance_changes:
        if not isinstance(change, Mapping):
            continue

        if change.get("owner") != wallet_address:
            continue

        mint = change.get("mint")
        raw_delta = change.get("delta_amount")

        if not isinstance(mint, str) or not mint:
            continue

        if (
            not isinstance(raw_delta, str)
            or re.fullmatch(r"-?\d+", raw_delta) is None
        ):
            continue

        delta = int(raw_delta)

        if delta == 0:
            continue

        if change.get("source") == NATIVE_SOL_SOURCE:
            if (
                mint != WRAPPED_SOL_MINT
                or change.get("decimals") != 9
            ):
                return unknown("Native SOL balance change is invalid.")

            native_sol_seen = True
            native_sol_delta += delta
            continue

        token_deltas[mint] = token_deltas.get(mint, 0) + delta

    token_deltas = {
        mint: delta
        for mint, delta in token_deltas.items()
        if delta != 0
    }

    token_quote_deltas = {
        mint: delta
        for mint, delta in token_deltas.items()
        if mint in QUOTE_TOKEN_MINTS
    }

    asset_deltas = {
        mint: delta
        for mint, delta in token_deltas.items()
        if mint not in QUOTE_TOKEN_MINTS
    }

    # Do not mix native SOL with wrapped SOL, USDC, or USDT movements.
    if native_sol_seen:
        if token_quote_deltas:
            return unknown(
                "Both native SOL and SPL quote-token movements are present."
            )

        token_quote_deltas = {
            WRAPPED_SOL_MINT: native_sol_delta,
        }

    if not token_deltas and not native_sol_seen:
        return unknown(
            "No usable balance changes owned by the monitored wallet."
        )

    if not token_quote_deltas:
        return unknown(
            "No recognized quote-token movement or native SOL movement."
        )

    if not asset_deltas:
        return unknown("No non-quote token movement was found.")

    if (
        all(delta > 0 for delta in asset_deltas.values())
        and all(delta < 0 for delta in token_quote_deltas.values())
    ):
        return {
            "classification": "BUY_CANDIDATE",
            "reason": (
                "A non-quote token increased while a recognized quote "
                "asset decreased."
            ),
            "asset_mints": sorted(asset_deltas),
            "quote_mints": sorted(token_quote_deltas),
        }

    if (
        all(delta < 0 for delta in asset_deltas.values())
        and all(delta > 0 for delta in token_quote_deltas.values())
    ):
        return {
            "classification": "SELL_CANDIDATE",
            "reason": (
                "A non-quote token decreased while a recognized quote "
                "asset increased."
            ),
            "asset_mints": sorted(asset_deltas),
            "quote_mints": sorted(token_quote_deltas),
        }

    return unknown(
        "Balance changes do not show one clear quote-token swap direction."
    )



def _pumpfun_instruction_action(transaction):
    """Return BUY, SELL, or AMBIGUOUS only for Pump.fun instruction logs.

    The action log must belong to a transaction instruction invoking the
    known Pump.fun program. Balance deltas are checked separately before a
    trade candidate is emitted.
    """
    if not isinstance(transaction, Mapping):
        return None

    transaction_data = transaction.get("transaction")
    message = (
        transaction_data.get("message")
        if isinstance(transaction_data, Mapping) else None
    )
    meta = transaction.get("meta")
    if not isinstance(message, Mapping) or not isinstance(meta, Mapping):
        return None

    logs = meta.get("logMessages")
    if not isinstance(logs, list):
        return None

    account_keys = message.get("accountKeys", [])
    if not isinstance(account_keys, list):
        account_keys = []

    def normalize_pubkey(item):
        if isinstance(item, str) and item:
            return item
        if isinstance(item, Mapping):
            pubkey = item.get("pubkey")
            if isinstance(pubkey, str) and pubkey:
                return pubkey
        return None

    program_ids = set()

    def include_program_id(instruction):
        if not isinstance(instruction, Mapping):
            return
        program_id = instruction.get("programId")
        program_id_index = instruction.get("programIdIndex")
        if (
            not isinstance(program_id, str)
            and isinstance(program_id_index, int)
            and not isinstance(program_id_index, bool)
            and 0 <= program_id_index < len(account_keys)
        ):
            program_id = normalize_pubkey(account_keys[program_id_index])
        if isinstance(program_id, str):
            program_ids.add(program_id)

    outer_instructions = message.get("instructions", [])
    if isinstance(outer_instructions, list):
        for instruction in outer_instructions:
            include_program_id(instruction)

    inner_groups = meta.get("innerInstructions", [])
    if isinstance(inner_groups, list):
        for group in inner_groups:
            if not isinstance(group, Mapping):
                continue
            inner_instructions = group.get("instructions", [])
            if isinstance(inner_instructions, list):
                for instruction in inner_instructions:
                    include_program_id(instruction)

    if PUMP_FUN_PROGRAM_ID not in program_ids:
        return None

    invocation_stack = []
    actions = set()
    for line in logs:
        if not isinstance(line, str):
            continue

        invocation = re.fullmatch(r"Program (\\S+) invoke \\[(\\d+)\\]", line)
        if invocation:
            depth = int(invocation.group(2))
            if depth < 1:
                continue
            invocation_stack = invocation_stack[:depth - 1]
            invocation_stack.append(invocation.group(1))
            continue

        completed = re.fullmatch(
            r"Program (\\S+) (?:success|failed(?::.*)?)",
            line,
        )
        if completed:
            completed_program = completed.group(1)
            for index in range(len(invocation_stack) - 1, -1, -1):
                if invocation_stack[index] == completed_program:
                    invocation_stack = invocation_stack[:index]
                    break
            continue

        prefix = "Program log: Instruction: "
        if not invocation_stack or invocation_stack[-1] != PUMP_FUN_PROGRAM_ID:
            continue
        if not line.startswith(prefix):
            continue

        instruction_name = line[len(prefix):].strip()
        if instruction_name in ("Buy", "BuyV2"):
            actions.add("BUY")
        elif instruction_name in ("Sell", "SellV2"):
            actions.add("SELL")

    if len(actions) == 1:
        return next(iter(actions))
    if len(actions) > 1:
        return "AMBIGUOUS"
    return None


def classify_transaction(transaction, wallet_address, balance_changes=None):
    """Classify conservatively and return auditable evidence and confidence.

    BUY_CANDIDATE and SELL_CANDIDATE are analytical hints, not execution
    instructions. Unknown or failed transactions must never be promoted to a
    trade candidate solely from a token receipt or SOL balance delta.
    """
    meta = transaction.get("meta") if isinstance(transaction, Mapping) else None
    if isinstance(meta, Mapping) and meta.get("err") is not None:
        return {
            "classification": "FAILED_TRANSACTION",
            "confidence": "high",
            "reason": "The chain metadata reports a failed transaction; no swap candidate is emitted.",
            "asset_mints": [],
            "quote_mints": [],
            "evidence": {
                "transaction_error": meta.get("err"),
                "fee_lamports": meta.get("fee"),
                "balance_changes_used": False,
            },
        }

    base_changes = balance_changes
    if base_changes is None and isinstance(meta, Mapping):
        base_changes = analyze_token_balance_changes(meta)
        if isinstance(transaction, Mapping):
            base_changes.extend(analyze_native_sol_balance_changes(transaction))

    signal = classify_token_balance_changes(base_changes, wallet_address)
    instructions = []
    fee = None
    if isinstance(meta, Mapping):
        fee = meta.get("fee")
    if isinstance(transaction, Mapping):
        tx_data = transaction.get("transaction")
        message = tx_data.get("message") if isinstance(tx_data, Mapping) else None
        if isinstance(message, Mapping):
            outer = message.get("instructions", [])
            if isinstance(outer, list):
                for index, instruction in enumerate(outer):
                    if not isinstance(instruction, Mapping):
                        continue
                    parsed = instruction.get("parsed")
                    instructions.append({
                        "index": index,
                        "program": instruction.get("program"),
                        "program_id": instruction.get("programId"),
                        "parsed_type": parsed.get("type") if isinstance(parsed, Mapping) else None,
                        "source": "message",
                    })
        if isinstance(meta, Mapping):
            inner_groups = meta.get("innerInstructions", [])
            if isinstance(inner_groups, list):
                for group in inner_groups:
                    if not isinstance(group, Mapping):
                        continue
                    inner = group.get("instructions", [])
                    if not isinstance(inner, list):
                        continue
                    for inner_index, instruction in enumerate(inner):
                        if not isinstance(instruction, Mapping):
                            continue
                        parsed = instruction.get("parsed")
                        instructions.append({
                            "outer_index": group.get("index"),
                            "inner_index": inner_index,
                            "program": instruction.get("program"),
                            "program_id": instruction.get("programId"),
                            "parsed_type": parsed.get("type") if isinstance(parsed, Mapping) else None,
                            "source": "inner",
                        })

    owned_changes = [
        {
            "owner": item.get("owner"),
            "mint": item.get("mint"),
            "raw_delta_amount": item.get("delta_amount"),
            "decimals": item.get("decimals"),
            "source": item.get("source", "spl_token"),
            "raw_pre_amount": item.get("pre_amount"),
            "raw_post_amount": item.get("post_amount"),
            "fee_adjusted": item.get("fee_adjusted", False),
        }
        for item in (base_changes if isinstance(base_changes, list) else [])
        if isinstance(item, Mapping) and item.get("owner") == wallet_address
    ]
    pumpfun_action = _pumpfun_instruction_action(transaction)
    candidate = signal.get("classification") in ("BUY_CANDIDATE", "SELL_CANDIDATE")

    # Pump.fun can use a quote mint outside the static USDC/USDT/WSOL list.
    # Its successful instruction log supplies direction, but wallet-owned
    # balance deltas must independently agree before emitting a candidate.
    if pumpfun_action in ("BUY", "SELL"):
        pumpfun_token_deltas = {}
        pumpfun_native_sol_delta = 0
        for item in (base_changes if isinstance(base_changes, list) else []):
            if not isinstance(item, Mapping) or item.get("owner") != wallet_address:
                continue
            mint = item.get("mint")
            raw_delta = item.get("delta_amount")
            if (
                not isinstance(mint, str)
                or not isinstance(raw_delta, str)
                or re.fullmatch(r"-?\\d+", raw_delta) is None
            ):
                continue
            delta = int(raw_delta)
            if delta == 0:
                continue
            if item.get("source") == NATIVE_SOL_SOURCE:
                if mint == WRAPPED_SOL_MINT and item.get("decimals") == 9:
                    pumpfun_native_sol_delta += delta
                continue
            pumpfun_token_deltas[mint] = pumpfun_token_deltas.get(mint, 0) + delta

        pumpfun_token_deltas = {
            mint: delta
            for mint, delta in pumpfun_token_deltas.items()
            if delta != 0
        }
        received_mints = sorted(
            mint for mint, delta in pumpfun_token_deltas.items() if delta > 0
        )
        spent_mints = sorted(
            mint for mint, delta in pumpfun_token_deltas.items() if delta < 0
        )

        if pumpfun_action == "BUY":
            asset_mints = [
                mint for mint in received_mints
                if mint not in QUOTE_TOKEN_MINTS
            ]
            payment_present = bool(spent_mints) or pumpfun_native_sol_delta < 0
            if (
                len(received_mints) == 1
                and len(asset_mints) == 1
                and payment_present
            ):
                quote_mints = spent_mints[:]
                if not quote_mints and pumpfun_native_sol_delta < 0:
                    quote_mints = [WRAPPED_SOL_MINT]
                signal = {
                    "classification": "BUY_CANDIDATE",
                    "reason": (
                        "A successful Pump.fun buy instruction was observed "
                        "and wallet-owned token balance changes agree with a buy."
                    ),
                    "asset_mints": asset_mints,
                    "quote_mints": quote_mints,
                }
                candidate = True
            else:
                signal = {
                    "classification": "UNKNOWN",
                    "reason": (
                        "Pump.fun buy was observed, but wallet-owned balance "
                        "changes do not unambiguously confirm the buy direction."
                    ),
                    "asset_mints": [],
                    "quote_mints": [],
                }
                candidate = False
        else:
            asset_mints = [
                mint for mint in spent_mints
                if mint not in QUOTE_TOKEN_MINTS
            ]
            payment_present = bool(received_mints) or pumpfun_native_sol_delta > 0
            if (
                len(spent_mints) == 1
                and len(asset_mints) == 1
                and payment_present
            ):
                quote_mints = received_mints[:]
                if not quote_mints and pumpfun_native_sol_delta > 0:
                    quote_mints = [WRAPPED_SOL_MINT]
                signal = {
                    "classification": "SELL_CANDIDATE",
                    "reason": (
                        "A successful Pump.fun sell instruction was observed "
                        "and wallet-owned token balance changes agree with a sell."
                    ),
                    "asset_mints": asset_mints,
                    "quote_mints": quote_mints,
                }
                candidate = True
            else:
                signal = {
                    "classification": "UNKNOWN",
                    "reason": (
                        "Pump.fun sell was observed, but wallet-owned balance "
                        "changes do not unambiguously confirm the sell direction."
                    ),
                    "asset_mints": [],
                    "quote_mints": [],
                }
                candidate = False
    elif pumpfun_action == "AMBIGUOUS":
        signal = {
            "classification": "UNKNOWN",
            "reason": "Conflicting Pump.fun buy/sell instructions were observed.",
            "asset_mints": [],
            "quote_mints": [],
        }
        candidate = False
    # If raw transaction context is available, a transfer/account-lifecycle
    # pattern plus balance deltas is not enough to prove a swap. Prefer UNKNOWN
    # unless a recognizable swap instruction/program is also present.
    if candidate and isinstance(transaction, Mapping):
        instruction_text = " ".join(
            str(value).lower()
            for instruction in instructions
            for value in (
                instruction.get("parsed_type"),
                instruction.get("program"),
                instruction.get("program_id"),
            )
            if value is not None
        )
        explicit_swap_context = pumpfun_action in ("BUY", "SELL") or any(
            marker in instruction_text
            for marker in (
                "swap", "jupiter", "raydium", "orca", "lifinity",
                "meteora", "phoenix", "openbook",
            )
        )
        lifecycle_context = any(
            marker in instruction_text
            for marker in (
                "transfer", "createaccount", "initializeaccount",
                "closeaccount", "syncnative",
            )
        )
        has_native_sol_delta = any(
            isinstance(change, Mapping)
            and change.get("owner") == wallet_address
            and change.get("source") == NATIVE_SOL_SOURCE
            and change.get("delta_amount") not in (None, "0", 0)
            for change in (base_changes if isinstance(base_changes, list) else [])
        )
        if not explicit_swap_context and (lifecycle_context or has_native_sol_delta):
            signal = {
                "classification": "UNKNOWN",
                "reason": (
                    "Balance changes overlap with transfer, account-lifecycle, "
                    "or native SOL activity without recognizable swap context."
                ),
                "asset_mints": [],
                "quote_mints": [],
            }
            candidate = False

    signal.update({
        "confidence": "medium" if candidate else "low",
        "evidence": {
            "wallet_address": wallet_address,
            "wallet_balance_changes": owned_changes,
            "instruction_context": instructions,
            "fee_lamports": fee,
            "classification_basis": "wallet-owned raw balance deltas plus available instruction context",
            "pumpfun_instruction_action": pumpfun_action,
            "limitations": [
                "Balance deltas do not prove an economic swap.",
                "Native SOL deltas can include transfers, rent, account creation, and other SOL uses.",
                "Unknown token owners are not attributed to the monitored wallet.",
                "Raw amounts are retained; decimals are metadata and no market-value estimate is made.",
            ],
        },
    })
    if candidate and not instructions:
        signal["reason"] += " No instruction context was available, so confidence remains medium."
    elif not candidate:
        signal["reason"] += " Evidence is insufficient or ambiguous; preserve UNKNOWN."
    return signal


if __name__ == "__main__":
    print("Transaction analysis module loaded successfully.")