//! Read-only Solana transaction interpretation and conservative swap-candidate classification.
//!
//! This module is an auditable heuristic layer. Balance deltas are not proof of a swap;
//! incomplete, ambiguous, transfer-only, and failed activity must never become candidates.

use std::collections::{BTreeMap, BTreeSet};
use std::str::FromStr;

use bigdecimal::BigDecimal;
use serde_json::{json, Map, Value};

pub const WRAPPED_SOL_MINT: &str = "So11111111111111111111111111111111111111112";
pub const PUMP_FUN_PROGRAM_ID: &str = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P";
pub const NATIVE_SOL_SOURCE: &str = "native_sol";
pub const USDC_MINT: &str = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";
pub const USDT_MINT: &str = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB";
pub const SYSTEM_PROGRAM_ID: &str = "11111111111111111111111111111111";
pub const SPL_TOKEN_PROGRAM_ID: &str = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA";
pub const TOKEN_2022_PROGRAM_ID: &str = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb";

#[derive(Debug, Clone)]
struct TokenBalance {
    owner: Option<String>,
    amount: BigDecimal,
    decimals: u64,
}

fn is_mapping(value: &Value) -> bool {
    value.is_object()
}

fn normalize_pubkey(item: &Value) -> Option<String> {
    if let Some(value) = item.as_str() {
        return (!value.is_empty()).then(|| value.to_string());
    }
    item.get("pubkey")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
}

fn signed_integer(value: &str) -> bool {
    let digits = value.strip_prefix('-').unwrap_or(value);
    !digits.is_empty() && digits.bytes().all(|byte| byte.is_ascii_digit())
}

fn decimal_integer(value: &str) -> Option<BigDecimal> {
    if !signed_integer(value) {
        return None;
    }
    BigDecimal::from_str(value).ok()
}

fn decimal_string(value: &BigDecimal) -> String {
    if value == &BigDecimal::from(0) {
        "0".to_string()
    } else {
        value.normalized().to_string()
    }
}

fn as_non_negative_usize(value: &Value) -> Option<usize> {
    value
        .as_u64()
        .and_then(|number| usize::try_from(number).ok())
}

fn resolve_program_id(instruction: &Value, account_keys: &[Value]) -> Option<String> {
    if let Some(program_id) = instruction
        .get("programId")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
    {
        return Some(program_id.to_string());
    }
    let index = instruction
        .get("programIdIndex")
        .and_then(as_non_negative_usize)?;
    account_keys.get(index).and_then(normalize_pubkey)
}

/// Resolve lookup-table program-index keys without duplicating keys already
/// expanded into parsed RPC accountKeys.
fn instruction_account_keys(transaction: &Value) -> Vec<Value> {
    let Some(message) = transaction
        .get("transaction")
        .and_then(|value| value.get("message"))
        .filter(|value| value.is_object())
    else {
        return Vec::new();
    };
    let mut keys = message
        .get("accountKeys")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let Some(meta) = transaction.get("meta").filter(|value| value.is_object()) else {
        return keys;
    };
    let Some(loaded) = meta
        .get("loadedAddresses")
        .filter(|value| value.is_object())
    else {
        return keys;
    };
    let mut lookup_keys = Vec::new();
    for group in ["writable", "readonly"] {
        if let Some(rows) = loaded.get(group).and_then(Value::as_array) {
            lookup_keys.extend(rows.iter().cloned());
        }
    }
    if lookup_keys.is_empty() {
        return keys;
    }
    let balances_already_expanded = meta
        .get("preBalances")
        .and_then(Value::as_array)
        .zip(meta.get("postBalances").and_then(Value::as_array))
        .is_some_and(|(pre, post)| pre.len() == post.len() && pre.len() == keys.len());
    let parsed_keys_already_expanded = keys
        .iter()
        .any(|item| item.get("source").and_then(Value::as_str) == Some("lookupTable"));
    if !balances_already_expanded && !parsed_keys_already_expanded {
        keys.extend(lookup_keys);
    }
    keys
}

fn is_supported_transfer_program(instruction: &Value, account_keys: &[Value]) -> bool {
    let program_id = resolve_program_id(instruction, account_keys).unwrap_or_default();
    matches!(
        program_id.as_str(),
        SYSTEM_PROGRAM_ID | SPL_TOKEN_PROGRAM_ID | TOKEN_2022_PROGRAM_ID
    )
}

/// Return a normalized summary while preserving instruction-level evidence.
pub fn analyze_transaction(transaction: &Value) -> Result<Value, &'static str> {
    if !is_mapping(transaction) {
        return Err("Transaction data must be a mapping.");
    }
    let Some(meta) = transaction.get("meta").filter(|value| value.is_object()) else {
        return Ok(json!({
            "available": false,
            "error": "Transaction metadata is unavailable."
        }));
    };
    let error = meta.get("err").cloned().unwrap_or(Value::Null);
    let status = if error.is_null() { "success" } else { "failed" };

    let message = transaction
        .get("transaction")
        .and_then(|value| value.get("message"))
        .filter(|value| value.is_object());
    let account_keys = instruction_account_keys(transaction);

    let mut signers = Vec::new();
    if let Some(required) = message
        .and_then(|value| value.get("header"))
        .and_then(|value| value.get("numRequiredSignatures"))
        .and_then(as_non_negative_usize)
    {
        if required <= account_keys.len() {
            signers.extend(account_keys[..required].iter().filter_map(normalize_pubkey));
        }
    }
    if signers.is_empty() {
        signers.extend(account_keys.iter().filter_map(|item| {
            (item.get("signer").and_then(Value::as_bool) == Some(true))
                .then(|| normalize_pubkey(item))
                .flatten()
        }));
    }

    let mut normalized_instructions = Vec::new();
    let mut program_ids = BTreeSet::new();
    if let Some(instructions) = message
        .and_then(|value| value.get("instructions"))
        .and_then(Value::as_array)
    {
        for (index, instruction) in instructions.iter().enumerate() {
            if !instruction.is_object() {
                continue;
            }
            let program_id = resolve_program_id(instruction, &account_keys);
            if let Some(program) = &program_id {
                program_ids.insert(program.clone());
            }
            let parsed_type = instruction
                .get("parsed")
                .and_then(|value| value.get("type"))
                .filter(|value| value.is_string())
                .cloned()
                .unwrap_or(Value::Null);
            normalized_instructions.push(json!({
                "index": index,
                "program_id": program_id,
                "program": instruction.get("program").cloned().unwrap_or(Value::Null),
                "parsed_type": parsed_type,
                "stack_height": instruction.get("stackHeight").cloned().unwrap_or(Value::Null),
                "source": "message"
            }));
        }
    }

    if let Some(groups) = meta.get("innerInstructions").and_then(Value::as_array) {
        for group in groups {
            if !group.is_object() {
                continue;
            }
            let outer_index = group.get("index").cloned().unwrap_or(Value::Null);
            if let Some(instructions) = group.get("instructions").and_then(Value::as_array) {
                for (inner_index, instruction) in instructions.iter().enumerate() {
                    if !instruction.is_object() {
                        continue;
                    }
                    let program_id = resolve_program_id(instruction, &account_keys);
                    if let Some(program) = &program_id {
                        program_ids.insert(program.clone());
                    }
                    let parsed_type = instruction
                        .get("parsed")
                        .and_then(|value| value.get("type"))
                        .filter(|value| value.is_string())
                        .cloned()
                        .unwrap_or(Value::Null);
                    normalized_instructions.push(json!({
                        "index": outer_index,
                        "inner_index": inner_index,
                        "program_id": program_id,
                        "program": instruction.get("program").cloned().unwrap_or(Value::Null),
                        "parsed_type": parsed_type,
                        "stack_height": instruction.get("stackHeight").cloned().unwrap_or(Value::Null),
                        "source": "inner"
                    }));
                }
            }
        }
    }

    Ok(json!({
        "available": true,
        "status": status,
        "slot": transaction.get("slot").cloned().unwrap_or(Value::Null),
        "fee": meta.get("fee").cloned().unwrap_or(Value::Null),
        "account_count": account_keys.len(),
        "signers": signers,
        "program_ids": program_ids.into_iter().collect::<Vec<_>>(),
        "instructions": normalized_instructions,
        "error": error
    }))
}

/// Extract SPL-token balance changes keyed by (account index, mint).
pub fn analyze_token_balance_changes(meta: &Value) -> Vec<Value> {
    if !meta.is_object() {
        return Vec::new();
    }

    fn normalize_balances(rows: Option<&Vec<Value>>) -> BTreeMap<(usize, String), TokenBalance> {
        let mut normalized = BTreeMap::new();
        let mut owner_conflicts = BTreeSet::new();
        for item in rows.into_iter().flatten() {
            if !item.is_object() {
                continue;
            }
            let Some(mint) = item
                .get("mint")
                .and_then(Value::as_str)
                .filter(|v| !v.is_empty())
            else {
                continue;
            };
            let Some(account_index) = item.get("accountIndex").and_then(as_non_negative_usize)
            else {
                continue;
            };
            let Some(amount_data) = item.get("uiTokenAmount").filter(|v| v.is_object()) else {
                continue;
            };
            let Some(raw_amount) = amount_data.get("amount").and_then(Value::as_str) else {
                continue;
            };
            if raw_amount.is_empty() || !raw_amount.bytes().all(|b| b.is_ascii_digit()) {
                continue;
            }
            let Some(amount) = BigDecimal::from_str(raw_amount).ok() else {
                continue;
            };
            let Some(decimals) = amount_data.get("decimals").and_then(Value::as_u64) else {
                continue;
            };
            let owner = item
                .get("owner")
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .map(str::to_string);
            let key = (account_index, mint.to_string());
            if let Some(existing) = normalized.get(&key) {
                let existing: &TokenBalance = existing;
                if existing.owner.is_some() && owner.is_some() && existing.owner != owner {
                    owner_conflicts.insert(key);
                }
                continue;
            }
            normalized.insert(
                key,
                TokenBalance {
                    owner,
                    amount,
                    decimals,
                },
            );
        }
        for key in owner_conflicts {
            normalized.remove(&key);
        }
        normalized
    }

    let before = normalize_balances(meta.get("preTokenBalances").and_then(Value::as_array));
    let after = normalize_balances(meta.get("postTokenBalances").and_then(Value::as_array));
    let keys: BTreeSet<_> = before.keys().chain(after.keys()).cloned().collect();
    let mut changes = Vec::new();

    for (account_index, mint) in keys {
        let old = before.get(&(account_index, mint.clone()));
        let new = after.get(&(account_index, mint.clone()));
        if old.is_some() && new.is_some() && old.unwrap().decimals != new.unwrap().decimals {
            continue;
        }
        let old_owner = old.and_then(|value| value.owner.as_ref());
        let new_owner = new.and_then(|value| value.owner.as_ref());
        if old_owner.is_some() && new_owner.is_some() && old_owner != new_owner {
            continue;
        }
        let owner = new_owner.or(old_owner).cloned();
        let decimals = new.or(old).map(|value| value.decimals).unwrap_or(0);
        let old_amount = old
            .map(|value| value.amount.clone())
            .unwrap_or_else(|| BigDecimal::from(0));
        let new_amount = new
            .map(|value| value.amount.clone())
            .unwrap_or_else(|| BigDecimal::from(0));
        let delta = &new_amount - &old_amount;
        if delta == 0 {
            continue;
        }
        changes.push(json!({
            "account_index": account_index,
            "mint": mint,
            "owner": owner,
            "decimals": decimals,
            "pre_amount": decimal_string(&old_amount),
            "post_amount": decimal_string(&new_amount),
            "delta_amount": decimal_string(&delta)
        }));
    }
    changes
}

/// Extract native SOL movements in lamports, fee-adjusting the fee payer only.
pub fn analyze_native_sol_balance_changes(transaction: &Value) -> Vec<Value> {
    let Some(meta) = transaction.get("meta").filter(|value| value.is_object()) else {
        return Vec::new();
    };
    let Some(pre_balances) = meta.get("preBalances").and_then(Value::as_array) else {
        return Vec::new();
    };
    let Some(post_balances) = meta.get("postBalances").and_then(Value::as_array) else {
        return Vec::new();
    };
    if pre_balances.len() != post_balances.len() {
        return Vec::new();
    }
    let Some(message) = transaction
        .get("transaction")
        .and_then(|value| value.get("message"))
        .filter(|value| value.is_object())
    else {
        return Vec::new();
    };
    let Some(raw_keys) = message.get("accountKeys").and_then(Value::as_array) else {
        return Vec::new();
    };
    let mut account_keys: Vec<Option<String>> = raw_keys.iter().map(normalize_pubkey).collect();
    if let Some(loaded) = meta
        .get("loadedAddresses")
        .filter(|value| value.is_object())
    {
        for group in ["writable", "readonly"] {
            if let Some(rows) = loaded.get(group).and_then(Value::as_array) {
                account_keys.extend(rows.iter().map(normalize_pubkey));
            }
        }
    }
    // Solana pre/post balance arrays are indexed against the complete account-key
    // list (static keys plus loaded addresses). Never silently truncate mismatched
    // arrays: doing so can attribute a partial transaction as a complete movement.
    if account_keys.len() != pre_balances.len()
        || account_keys.len() != post_balances.len()
        || account_keys.iter().any(Option::is_none)
        || pre_balances.iter().any(|value| value.as_u64().is_none())
        || post_balances.iter().any(|value| value.as_u64().is_none())
    {
        return Vec::new();
    }
    let fee = meta.get("fee").and_then(Value::as_u64);
    let count = account_keys.len();
    let mut changes = Vec::new();

    for index in 0..count {
        let Some(owner) = account_keys[index].as_ref() else {
            continue;
        };
        let Some(before) = pre_balances[index].as_u64() else {
            continue;
        };
        let Some(after) = post_balances[index].as_u64() else {
            continue;
        };
        let raw_delta = i128::from(after) - i128::from(before);
        let mut adjusted_delta = raw_delta;
        let mut fee_adjusted = false;
        if index == 0 {
            let Some(fee_value) = fee else {
                continue;
            };
            adjusted_delta += i128::from(fee_value);
            fee_adjusted = true;
        }
        if adjusted_delta == 0 {
            continue;
        }
        changes.push(json!({
            "account_index": index,
            "mint": WRAPPED_SOL_MINT,
            "owner": owner,
            "decimals": 9,
            "pre_amount": before.to_string(),
            "post_amount": after.to_string(),
            "delta_amount": adjusted_delta.to_string(),
            "raw_delta_amount": raw_delta.to_string(),
            "fee_adjusted": fee_adjusted,
            "source": NATIVE_SOL_SOURCE
        }));
    }
    changes
}

fn unknown(reason: &str) -> Value {
    json!({
        "classification": "UNKNOWN",
        "reason": reason,
        "asset_mints": [],
        "quote_mints": []
    })
}

/// Conservatively classify a wallet's raw balance deltas without mixing native SOL and SPL quotes.
pub fn classify_token_balance_changes(changes: &Value, wallet_address: &str) -> Value {
    if wallet_address.trim().is_empty() {
        return unknown("A monitored wallet address is required.");
    }
    let Some(changes) = changes.as_array() else {
        return unknown("Token balance changes are unavailable.");
    };

    let mut token_deltas: BTreeMap<String, BigDecimal> = BTreeMap::new();
    let mut native_sol_delta = BigDecimal::from(0);
    let mut native_sol_seen = false;
    for change in changes {
        if !change.is_object()
            || change.get("owner").and_then(Value::as_str) != Some(wallet_address)
        {
            continue;
        }
        let Some(mint) = change
            .get("mint")
            .and_then(Value::as_str)
            .filter(|v| !v.is_empty())
        else {
            continue;
        };
        let Some(raw_delta) = change.get("delta_amount").and_then(Value::as_str) else {
            continue;
        };
        let Some(delta) = decimal_integer(raw_delta) else {
            continue;
        };
        if delta == 0 {
            continue;
        }
        if change.get("source").and_then(Value::as_str) == Some(NATIVE_SOL_SOURCE) {
            if mint != WRAPPED_SOL_MINT || change.get("decimals").and_then(Value::as_u64) != Some(9)
            {
                return unknown("Native SOL balance change is invalid.");
            }
            native_sol_seen = true;
            native_sol_delta += delta;
            continue;
        }
        token_deltas
            .entry(mint.to_string())
            .and_modify(|total| *total += &delta)
            .or_insert(delta);
    }
    token_deltas.retain(|_, delta| *delta != 0);

    let quote_token_mints = [USDC_MINT, USDT_MINT, WRAPPED_SOL_MINT];
    let token_quote_deltas: BTreeMap<_, _> = token_deltas
        .iter()
        .filter(|(mint, _)| quote_token_mints.contains(&mint.as_str()))
        .map(|(mint, delta)| (mint.clone(), delta.clone()))
        .collect();
    let asset_deltas: BTreeMap<_, _> = token_deltas
        .iter()
        .filter(|(mint, _)| !quote_token_mints.contains(&mint.as_str()))
        .map(|(mint, delta)| (mint.clone(), delta.clone()))
        .collect();

    let mut quote_deltas = token_quote_deltas;
    if native_sol_seen {
        if !quote_deltas.is_empty() {
            return unknown("Both native SOL and SPL quote-token movements are present.");
        }
        quote_deltas.insert(WRAPPED_SOL_MINT.to_string(), native_sol_delta);
    }
    if token_deltas.is_empty() && !native_sol_seen {
        return unknown("No usable balance changes owned by the monitored wallet.");
    }
    if quote_deltas.is_empty() {
        return unknown("No recognized quote-token movement or native SOL movement.");
    }
    if asset_deltas.is_empty() {
        return unknown("No non-quote token movement was found.");
    }

    let asset_mints: Vec<_> = asset_deltas.keys().cloned().collect();
    let quote_mints: Vec<_> = quote_deltas.keys().cloned().collect();
    if asset_deltas.values().all(|delta| delta > 0) && quote_deltas.values().all(|delta| delta < 0)
    {
        return json!({
            "classification": "BUY_CANDIDATE",
            "reason": "A non-quote token increased while a recognized quote asset decreased.",
            "asset_mints": asset_mints,
            "quote_mints": quote_mints
        });
    }
    if asset_deltas.values().all(|delta| delta < 0) && quote_deltas.values().all(|delta| delta > 0)
    {
        return json!({
            "classification": "SELL_CANDIDATE",
            "reason": "A non-quote token decreased while a recognized quote asset increased.",
            "asset_mints": asset_mints,
            "quote_mints": quote_mints
        });
    }
    unknown("Balance changes do not show one clear quote-token swap direction.")
}

fn pumpfun_instruction_context(transaction: &Value) -> Option<Value> {
    let message = transaction.get("transaction")?.get("message")?;
    let meta = transaction.get("meta")?;
    let logs = meta.get("logMessages")?.as_array()?;
    let account_keys = message
        .get("accountKeys")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();

    let mut pumpfun_instructions = Vec::new();
    if let Some(outer) = message.get("instructions").and_then(Value::as_array) {
        for instruction in outer {
            if resolve_program_id(instruction, &account_keys).as_deref()
                != Some(PUMP_FUN_PROGRAM_ID)
            {
                continue;
            }
            if let Some(accounts) = instruction.get("accounts").and_then(Value::as_array) {
                if accounts.len() >= 3 {
                    if let (Some(base), Some(quote)) = (accounts[1].as_str(), accounts[2].as_str())
                    {
                        if !base.is_empty() && !quote.is_empty() {
                            pumpfun_instructions.push((base.to_string(), quote.to_string()));
                        }
                    }
                }
            }
        }
    }
    if let Some(groups) = meta.get("innerInstructions").and_then(Value::as_array) {
        for group in groups {
            if let Some(inner) = group.get("instructions").and_then(Value::as_array) {
                for instruction in inner {
                    if resolve_program_id(instruction, &account_keys).as_deref()
                        != Some(PUMP_FUN_PROGRAM_ID)
                    {
                        continue;
                    }
                    if let Some(accounts) = instruction.get("accounts").and_then(Value::as_array) {
                        if accounts.len() >= 3 {
                            if let (Some(base), Some(quote)) =
                                (accounts[1].as_str(), accounts[2].as_str())
                            {
                                if !base.is_empty() && !quote.is_empty() {
                                    pumpfun_instructions
                                        .push((base.to_string(), quote.to_string()));
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    let mut invocation_stack: Vec<String> = Vec::new();
    let mut action_events = Vec::new();
    for log in logs.iter().filter_map(Value::as_str) {
        if let Some(rest) = log.strip_prefix("Program ") {
            if let Some((program, depth)) = rest.split_once(" invoke [") {
                if let Some(depth) = depth
                    .strip_suffix(']')
                    .and_then(|v| v.parse::<usize>().ok())
                {
                    if !program.is_empty() && depth >= 1 {
                        invocation_stack.truncate(depth - 1);
                        invocation_stack.push(program.to_string());
                        continue;
                    }
                }
            }
            let mut pieces = rest.splitn(2, ' ');
            let program = pieces.next().unwrap_or_default();
            let suffix = pieces.next().unwrap_or_default();
            if !program.is_empty() && (suffix == "success" || suffix.starts_with("failed")) {
                if let Some(index) = invocation_stack.iter().rposition(|value| value == program) {
                    invocation_stack.truncate(index);
                }
                continue;
            }
        }
        let prefix = "Program log: Instruction: ";
        if invocation_stack.last().map(String::as_str) != Some(PUMP_FUN_PROGRAM_ID)
            || !log.starts_with(prefix)
        {
            continue;
        }
        match log[prefix.len()..].trim() {
            "BuyV2" => action_events.push("BUY"),
            "SellV2" => action_events.push("SELL"),
            _ => {}
        }
    }

    if action_events.is_empty() {
        return None;
    }
    if action_events.len() != 1 || pumpfun_instructions.len() != 1 {
        return Some(json!({"action":"AMBIGUOUS"}));
    }
    Some(json!({
        "action": action_events[0],
        "base_mint": pumpfun_instructions[0].0,
        "quote_mint": pumpfun_instructions[0].1
    }))
}

/// Classify a transaction with auditable evidence; unknown or failed data cannot become a candidate.
pub fn classify_transaction(
    transaction: &Value,
    wallet_address: &str,
    supplied_balance_changes: Option<&Value>,
) -> Value {
    let meta = transaction.get("meta").filter(|value| value.is_object());
    if let Some(meta) = meta {
        let error = meta.get("err").cloned().unwrap_or(Value::Null);
        if !error.is_null() {
            return json!({
                "classification": "FAILED_TRANSACTION",
                "transaction_type": "FAILED",
                "trade_signal": "NOT_COPYABLE",
                "confidence": "high",
                "reason": "The chain metadata reports a failed transaction; no swap candidate is emitted.",
                "asset_mints": [],
                "quote_mints": [],
                "evidence": {
                    "transaction_error": error,
                    "fee_lamports": meta.get("fee").cloned().unwrap_or(Value::Null),
                    "balance_changes_used": false
                }
            });
        }
    }

    let mut derived_changes = Vec::new();
    if supplied_balance_changes.is_none() {
        if let Some(meta) = meta {
            derived_changes.extend(analyze_token_balance_changes(meta));
            derived_changes.extend(analyze_native_sol_balance_changes(transaction));
        }
    }
    let derived_changes_value = Value::Array(derived_changes);
    let base_changes: &Value = supplied_balance_changes.unwrap_or(&derived_changes_value);
    let mut signal = classify_token_balance_changes(base_changes, wallet_address);

    let mut instructions = Vec::new();
    let mut direct_transfer_instruction = false;
    let mut recognized_non_transfer_instruction = false;
    let mut recognized_other_instruction = false;
    let fee = meta
        .and_then(|value| value.get("fee"))
        .cloned()
        .unwrap_or(Value::Null);
    if let Some(message) = transaction
        .get("transaction")
        .and_then(|value| value.get("message"))
    {
        let account_keys = instruction_account_keys(transaction);
        if let Some(outer) = message.get("instructions").and_then(Value::as_array) {
            for (index, instruction) in outer.iter().enumerate() {
                if !instruction.is_object() {
                    continue;
                }
                let parsed_type = instruction
                    .get("parsed")
                    .and_then(|value| value.get("type"))
                    .cloned()
                    .unwrap_or(Value::Null);
                let parsed_name = parsed_type
                    .as_str()
                    .unwrap_or_default()
                    .to_ascii_lowercase();
                if matches!(
                    parsed_name.as_str(),
                    "transfer" | "transferchecked" | "transfer_checked"
                ) && is_supported_transfer_program(instruction, &account_keys)
                {
                    direct_transfer_instruction = true;
                } else if !parsed_name.is_empty()
                    && !matches!(
                        parsed_name.as_str(),
                        "transfer" | "transferchecked" | "transfer_checked"
                    )
                {
                    recognized_non_transfer_instruction = true;
                    if matches!(
                        parsed_name.as_str(),
                        "createaccount"
                            | "create_account"
                            | "initializeaccount"
                            | "initialize_account"
                            | "closeaccount"
                            | "close_account"
                            | "syncnative"
                            | "sync_native"
                            | "approve"
                            | "revoke"
                            | "setauthority"
                            | "set_authority"
                            | "mintto"
                            | "mint_to"
                            | "burn"
                            | "burnchecked"
                            | "burn_checked"
                    ) {
                        recognized_other_instruction = true;
                    }
                }
                instructions.push(json!({
                    "index": index,
                    "program": instruction.get("program").cloned().unwrap_or(Value::Null),
                    "program_id": instruction.get("programId").cloned().unwrap_or(Value::Null),
                    "parsed_type": parsed_type,
                    "source": "message"
                }));
            }
        }
    }
    if let Some(meta) = meta {
        if let Some(groups) = meta.get("innerInstructions").and_then(Value::as_array) {
            for group in groups {
                if let Some(inner) = group.get("instructions").and_then(Value::as_array) {
                    for (inner_index, instruction) in inner.iter().enumerate() {
                        if !instruction.is_object() {
                            continue;
                        }
                        let parsed_type = instruction
                            .get("parsed")
                            .and_then(|value| value.get("type"))
                            .cloned()
                            .unwrap_or(Value::Null);
                        instructions.push(json!({
                            "outer_index": group.get("index").cloned().unwrap_or(Value::Null),
                            "inner_index": inner_index,
                            "program": instruction.get("program").cloned().unwrap_or(Value::Null),
                            "program_id": instruction.get("programId").cloned().unwrap_or(Value::Null),
                            "parsed_type": parsed_type,
                            "source": "inner"
                        }));
                    }
                }
            }
        }
    }

    let owned_changes: Vec<Value> = base_changes
        .as_array()
        .into_iter()
        .flatten()
        .filter(|item| {
            item.is_object() && item.get("owner").and_then(Value::as_str) == Some(wallet_address)
        })
        .map(|item| {
            json!({
                "owner": item.get("owner").cloned().unwrap_or(Value::Null),
                "mint": item.get("mint").cloned().unwrap_or(Value::Null),
                "raw_delta_amount": item.get("delta_amount").cloned().unwrap_or(Value::Null),
                "decimals": item.get("decimals").cloned().unwrap_or(Value::Null),
                "source": item.get("source").cloned().unwrap_or(json!("spl_token")),
                "raw_pre_amount": item.get("pre_amount").cloned().unwrap_or(Value::Null),
                "raw_post_amount": item.get("post_amount").cloned().unwrap_or(Value::Null),
                "fee_adjusted": item.get("fee_adjusted").cloned().unwrap_or(Value::Bool(false))
            })
        })
        .collect();

    let pumpfun_context = pumpfun_instruction_context(transaction);
    let pumpfun_action = pumpfun_context
        .as_ref()
        .and_then(|value| value.get("action"))
        .and_then(Value::as_str);
    let mut candidate = matches!(
        signal.get("classification").and_then(Value::as_str),
        Some("BUY_CANDIDATE" | "SELL_CANDIDATE")
    );

    if matches!(pumpfun_action, Some("BUY" | "SELL")) {
        let base_mint = pumpfun_context
            .as_ref()
            .and_then(|value| value.get("base_mint"))
            .and_then(Value::as_str);
        let quote_mint = pumpfun_context
            .as_ref()
            .and_then(|value| value.get("quote_mint"))
            .and_then(Value::as_str);
        let mut pumpfun_token_deltas: BTreeMap<String, BigDecimal> = BTreeMap::new();
        let mut pumpfun_native_sol_delta = BigDecimal::from(0);
        for item in base_changes.as_array().into_iter().flatten() {
            if !item.is_object()
                || item.get("owner").and_then(Value::as_str) != Some(wallet_address)
            {
                continue;
            }
            let Some(mint) = item.get("mint").and_then(Value::as_str) else {
                continue;
            };
            let Some(raw_delta) = item.get("delta_amount").and_then(Value::as_str) else {
                continue;
            };
            let Some(delta) = decimal_integer(raw_delta) else {
                continue;
            };
            if delta == 0 {
                continue;
            }
            if item.get("source").and_then(Value::as_str) == Some(NATIVE_SOL_SOURCE) {
                if mint == WRAPPED_SOL_MINT
                    && item.get("decimals").and_then(Value::as_u64) == Some(9)
                {
                    pumpfun_native_sol_delta += delta;
                }
                continue;
            }
            pumpfun_token_deltas
                .entry(mint.to_string())
                .and_modify(|total| *total += &delta)
                .or_insert(delta);
        }

        if let (Some(base_mint), Some(quote_mint)) = (base_mint, quote_mint) {
            if base_mint != quote_mint {
                let base_delta = pumpfun_token_deltas
                    .get(base_mint)
                    .cloned()
                    .unwrap_or_else(|| BigDecimal::from(0));
                let quote_delta = pumpfun_token_deltas
                    .get(quote_mint)
                    .cloned()
                    .unwrap_or_else(|| BigDecimal::from(0));
                let (base_matches, quote_matches, classification) = if pumpfun_action == Some("BUY")
                {
                    (
                        base_delta > 0,
                        if quote_mint != WRAPPED_SOL_MINT {
                            quote_delta < 0
                        } else {
                            quote_delta < 0 || pumpfun_native_sol_delta < 0
                        },
                        "BUY_CANDIDATE",
                    )
                } else {
                    (
                        base_delta < 0,
                        if quote_mint != WRAPPED_SOL_MINT {
                            quote_delta > 0
                        } else {
                            quote_delta > 0 || pumpfun_native_sol_delta > 0
                        },
                        "SELL_CANDIDATE",
                    )
                };
                if base_matches && quote_matches {
                    signal = json!({
                        "classification": classification,
                        "reason": "A successful Pump.fun V2 instruction was observed and the specified base/quote mint balance changes agree with its direction.",
                        "asset_mints": [base_mint],
                        "quote_mints": [quote_mint]
                    });
                    candidate = true;
                } else {
                    signal = json!({
                        "classification": "UNKNOWN",
                        "reason": "Pump.fun V2 was observed, but the specified base/quote mint balance changes do not confirm its direction.",
                        "asset_mints": [],
                        "quote_mints": []
                    });
                    candidate = false;
                }
            } else {
                signal = json!({
                    "classification": "UNKNOWN",
                    "reason": "Pump.fun V2 was observed, but its base/quote mint context was unavailable or ambiguous.",
                    "asset_mints": [],
                    "quote_mints": []
                });
                candidate = false;
            }
        } else {
            signal = json!({
                "classification": "UNKNOWN",
                "reason": "Pump.fun V2 was observed, but its base/quote mint context was unavailable or ambiguous.",
                "asset_mints": [],
                "quote_mints": []
            });
            candidate = false;
        }
    } else if pumpfun_action == Some("AMBIGUOUS") {
        signal = json!({
            "classification": "UNKNOWN",
            "reason": "Pump.fun V2 instruction context is ambiguous.",
            "asset_mints": [],
            "quote_mints": []
        });
        candidate = false;
    }

    if candidate {
        let mut instruction_text = String::new();
        for instruction in &instructions {
            for key in ["parsed_type", "program", "program_id"] {
                if let Some(value) = instruction.get(key).and_then(Value::as_str) {
                    instruction_text.push(' ');
                    instruction_text.push_str(&value.to_ascii_lowercase());
                }
            }
        }
        let explicit_swap_context = matches!(pumpfun_action, Some("BUY" | "SELL"))
            || [
                "swap", "jupiter", "raydium", "orca", "lifinity", "meteora", "phoenix", "openbook",
            ]
            .iter()
            .any(|marker| instruction_text.contains(marker));
        let lifecycle_context = [
            "transfer",
            "createaccount",
            "initializeaccount",
            "closeaccount",
            "syncnative",
        ]
        .iter()
        .any(|marker| instruction_text.contains(marker));
        let has_native_sol_delta = base_changes.as_array().into_iter().flatten().any(|change| {
            if !change.is_object()
                || change.get("owner").and_then(Value::as_str) != Some(wallet_address)
                || change.get("source").and_then(Value::as_str) != Some(NATIVE_SOL_SOURCE)
            {
                return false;
            }
            match change.get("delta_amount") {
                None | Some(Value::Null) => false,
                Some(Value::String(value)) => value != "0",
                Some(Value::Number(value)) => value.to_string() != "0",
                Some(_) => true,
            }
        });
        if !explicit_swap_context && (lifecycle_context || has_native_sol_delta) {
            signal = json!({
                "classification": "UNKNOWN",
                "reason": "Balance changes overlap with transfer, account-lifecycle, or native SOL activity without recognizable swap context.",
                "asset_mints": [],
                "quote_mints": []
            });
            candidate = false;
        }
    }

    let signal_object = signal.as_object().cloned().unwrap_or_default();
    let classification = signal_object
        .get("classification")
        .and_then(Value::as_str)
        .unwrap_or("UNKNOWN")
        .to_string();
    let reason = signal_object
        .get("reason")
        .and_then(Value::as_str)
        .unwrap_or("Evidence is insufficient or ambiguous.")
        .to_string();
    let mut result: Map<String, Value> = signal_object;
    let transaction_type = if classification == "FAILED_TRANSACTION" {
        "FAILED"
    } else if candidate && classification == "BUY_CANDIDATE" {
        "BUY"
    } else if candidate && classification == "SELL_CANDIDATE" {
        "SELL"
    } else if direct_transfer_instruction && !recognized_non_transfer_instruction {
        "TRANSFER"
    } else if recognized_other_instruction {
        "OTHER"
    } else {
        "UNKNOWN"
    };
    result.insert("transaction_type".to_string(), json!(transaction_type));
    result.insert(
        "trade_signal".to_string(),
        json!(if candidate {
            classification.as_str()
        } else {
            "NOT_COPYABLE"
        }),
    );
    result.insert(
        "confidence".to_string(),
        json!(if candidate { "medium" } else { "low" }),
    );
    result.insert("evidence".to_string(), json!({
        "wallet_address": wallet_address,
        "wallet_balance_changes": owned_changes,
        "instruction_context": instructions,
        "fee_lamports": fee,
        "classification_basis": "wallet-owned raw balance deltas plus available instruction context",
        "pumpfun_instruction_action": pumpfun_action,
        "pumpfun_base_mint": pumpfun_context.as_ref().and_then(|value| value.get("base_mint")).cloned().unwrap_or(Value::Null),
        "pumpfun_quote_mint": pumpfun_context.as_ref().and_then(|value| value.get("quote_mint")).cloned().unwrap_or(Value::Null),
        "limitations": [
            "Balance deltas do not prove an economic swap.",
            "Native SOL deltas can include transfers, rent, account creation, and other SOL uses.",
            "Unknown token owners are not attributed to the monitored wallet.",
            "Raw amounts are retained; decimals are metadata and no market-value estimate is made."
        ]
    }));
    let suffix = if candidate {
        if instructions.is_empty() {
            " No instruction context was available, so confidence remains medium."
        } else {
            ""
        }
    } else {
        " Evidence is insufficient or ambiguous; preserve UNKNOWN."
    };
    let final_reason = format!("{reason}{suffix}");
    result.insert("reason".to_string(), Value::String(final_reason));
    result.insert("classification".to_string(), Value::String(classification));
    Value::Object(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    const WALLET: &str = "Wallet_A";
    const ASSET: &str = "Token_A";

    #[test]
    fn transaction_summary_preserves_signers_programs_and_inner_instruction_context() {
        let transaction = json!({
            "slot": 99,
            "transaction": {"message": {
                "accountKeys": [
                    {"pubkey":WALLET,"signer":true},
                    "TOKEN_PROGRAM"
                ],
                "header":{"numRequiredSignatures":1},
                "instructions":[{
                    "programIdIndex":1,
                    "parsed":{"type":"transfer"},
                    "stackHeight":1
                }]
            }},
            "meta":{"err":null,"fee":5000,"innerInstructions":[{
                "index":0,
                "instructions":[{
                    "programIdIndex":1,
                    "parsed":{"type":"checked_transfer"},
                    "stackHeight":2
                }]
            }]}
        });
        let result = analyze_transaction(&transaction).unwrap();
        assert_eq!(result["status"], "success");
        assert_eq!(result["signers"], json!([WALLET]));
        assert_eq!(result["program_ids"], json!(["TOKEN_PROGRAM"]));
        assert_eq!(result["instructions"][1]["source"], "inner");
        assert_eq!(result["instructions"][1]["parsed_type"], "checked_transfer");
    }

    #[test]
    fn versioned_lookup_table_program_indices_resolve_for_outer_and_inner_instructions() {
        let transaction = json!({
            "slot": 101,
            "transaction": {"message": {
                "header": {"numRequiredSignatures": 1},
                "accountKeys": [
                    {"pubkey": WALLET, "signer": true},
                    {"pubkey": "Other"}
                ],
                "instructions": [{
                    "programIdIndex": 2,
                    "parsed": {"type": "transfer"}
                }]
            }},
            "meta": {
                "err": null,
                "fee": 5000,
                "preBalances": [10000, 20000, 30000],
                "postBalances": [9000, 21000, 30000],
                "loadedAddresses": {
                    "writable": [SYSTEM_PROGRAM_ID],
                    "readonly": []
                },
                "innerInstructions": [{
                    "index": 0,
                    "instructions": [{
                        "programIdIndex": 2,
                        "parsed": {"type": "transferChecked"}
                    }]
                }]
            }
        });
        let summary = analyze_transaction(&transaction).unwrap();
        assert_eq!(summary["instructions"][0]["program_id"], SYSTEM_PROGRAM_ID);
        assert_eq!(summary["instructions"][1]["program_id"], SYSTEM_PROGRAM_ID);
        assert_eq!(summary["account_count"], 3);
        let classification = classify_transaction(&transaction, WALLET, None);
        assert_eq!(classification["transaction_type"], "TRANSFER");
        assert_eq!(classification["trade_signal"], "NOT_COPYABLE");
    }

    #[test]
    fn parsed_lookup_keys_are_not_duplicated_when_metadata_is_expanded() {
        let transaction = json!({
            "transaction": {"message": {
                "accountKeys": [
                    {"pubkey": WALLET, "source": "transaction"},
                    {"pubkey": SYSTEM_PROGRAM_ID, "source": "lookupTable"}
                ],
                "instructions": [{"programIdIndex": 1, "parsed":{"type":"transfer"}}]
            }},
            "meta": {
                "err": null,
                "fee": 5000,
                "preBalances": [10000, 20000],
                "postBalances": [9000, 21000],
                "loadedAddresses": {"writable":[SYSTEM_PROGRAM_ID],"readonly":[]}
            }
        });
        let summary = analyze_transaction(&transaction).unwrap();
        assert_eq!(summary["account_count"], 2);
        assert_eq!(summary["instructions"][0]["program_id"], SYSTEM_PROGRAM_ID);
    }

    #[test]
    fn native_sol_balance_analysis_rejects_account_key_and_balance_length_mismatch() {
        let transaction = json!({
            "transaction":{"message":{"accountKeys":[WALLET, "Other"]}},
            "meta":{"fee":5000,"preBalances":[10000],"postBalances":[9000]}
        });
        assert!(analyze_native_sol_balance_changes(&transaction).is_empty());
    }

    #[test]
    fn direct_outer_transfer_is_explicitly_classified_and_never_copyable() {
        let transaction = json!({
            "transaction":{"message":{"accountKeys":[WALLET],"instructions":[{
                "program":"system",
                "programId":"11111111111111111111111111111111",
                "parsed":{"type":"transfer"}
            }]}},
            "meta":{"err":null,"fee":5000,"preBalances":[100000],"postBalances":[90000]}
        });
        let result = classify_transaction(&transaction, WALLET, None);
        assert_eq!(result["transaction_type"], "TRANSFER");
        assert_eq!(result["trade_signal"], "NOT_COPYABLE");
        assert_eq!(result["classification"], "UNKNOWN");
    }

    #[test]
    fn spl_token_2022_direct_transfer_is_classified_as_transfer_only() {
        let transaction = json!({
            "transaction":{"message":{"accountKeys":[WALLET, TOKEN_2022_PROGRAM_ID],"instructions":[{
                "program":"spl-token-2022",
                "programId":TOKEN_2022_PROGRAM_ID,
                "parsed":{"type":"transferChecked"}
            }]}},
            "meta":{"err":null,"fee":5000}
        });
        let result = classify_transaction(&transaction, WALLET, None);
        assert_eq!(result["transaction_type"], "TRANSFER");
        assert_eq!(result["trade_signal"], "NOT_COPYABLE");
    }

    #[test]
    fn unknown_program_cannot_claim_a_verified_transfer_type() {
        let transaction = json!({
            "transaction":{"message":{"accountKeys":[WALLET, "UnknownProgram"],"instructions":[{
                "program":"unknown",
                "programId":"UnknownProgram",
                "parsed":{"type":"transfer"}
            }]}},
            "meta":{"err":null,"fee":5000}
        });
        let result = classify_transaction(&transaction, WALLET, None);
        assert_eq!(result["transaction_type"], "UNKNOWN");
        assert_eq!(result["trade_signal"], "NOT_COPYABLE");
    }

    #[test]
    fn recognized_account_lifecycle_instruction_is_other_and_not_copyable() {
        let transaction = json!({
            "transaction":{"message":{"accountKeys":[WALLET],"instructions":[{
                "program":"system",
                "parsed":{"type":"createAccount"}
            }]}},
            "meta":{"err":null,"fee":5000}
        });
        let result = classify_transaction(&transaction, WALLET, None);
        assert_eq!(result["transaction_type"], "OTHER");
        assert_eq!(result["trade_signal"], "NOT_COPYABLE");
    }

    #[test]
    fn token_balance_changes_preserve_exact_integer_amounts() {
        let meta = json!({
            "preTokenBalances":[{
                "accountIndex":1,"mint":ASSET,"owner":WALLET,
                "uiTokenAmount":{"amount":"900719925474099300000","decimals":6}
            }],
            "postTokenBalances":[{
                "accountIndex":1,"mint":ASSET,"owner":WALLET,
                "uiTokenAmount":{"amount":"900719925474099300075","decimals":6}
            }]
        });
        let changes = analyze_token_balance_changes(&meta);
        assert_eq!(changes.len(), 1);
        assert_eq!(changes[0]["delta_amount"], "75");
    }

    #[test]
    fn ambiguous_or_conflicting_token_owners_are_not_attributed() {
        let meta = json!({
            "preTokenBalances":[
                {"accountIndex":1,"mint":ASSET,"owner":"Wallet_A","uiTokenAmount":{"amount":"100","decimals":2}},
                {"accountIndex":1,"mint":ASSET,"owner":"Wallet_B","uiTokenAmount":{"amount":"100","decimals":2}}
            ],
            "postTokenBalances":[]
        });
        assert!(analyze_token_balance_changes(&meta).is_empty());
    }

    #[test]
    fn native_sol_fee_is_added_back_only_for_fee_payer() {
        let transaction = json!({
            "transaction":{"message":{"accountKeys":[WALLET,"Other"]}},
            "meta":{
                "fee":5000,
                "preBalances":[100000,200000,300000],
                "postBalances":[95000,150000,300000],
                "loadedAddresses":{"writable":["Loaded_W"],"readonly":[]}
            }
        });
        let changes = analyze_native_sol_balance_changes(&transaction);
        assert_eq!(changes.len(), 1);
        assert_eq!(changes[0]["account_index"], 1);
        assert_eq!(changes[0]["delta_amount"], "-50000");
    }

    #[test]
    fn recognizes_stablecoin_buy_and_sell_candidates() {
        let buy = json!([
            {"owner":WALLET,"mint":ASSET,"delta_amount":"100","decimals":2},
            {"owner":WALLET,"mint":USDC_MINT,"delta_amount":"-2500000","decimals":6}
        ]);
        assert_eq!(
            classify_token_balance_changes(&buy, WALLET)["classification"],
            "BUY_CANDIDATE"
        );
        let sell = json!([
            {"owner":WALLET,"mint":ASSET,"delta_amount":"-100","decimals":2},
            {"owner":WALLET,"mint":USDC_MINT,"delta_amount":"2500000","decimals":6}
        ]);
        assert_eq!(
            classify_token_balance_changes(&sell, WALLET)["classification"],
            "SELL_CANDIDATE"
        );
    }

    #[test]
    fn rejects_native_sol_and_spl_quote_mixing_and_transfer_only_activity() {
        let mixed = json!([
            {"owner":WALLET,"mint":ASSET,"delta_amount":"100","decimals":2},
            {"owner":WALLET,"mint":USDC_MINT,"delta_amount":"-50","decimals":6},
            {"owner":WALLET,"mint":WRAPPED_SOL_MINT,"delta_amount":"-10","decimals":9,"source":"native_sol"}
        ]);
        assert_eq!(
            classify_token_balance_changes(&mixed, WALLET)["classification"],
            "UNKNOWN"
        );

        let transfer_tx = json!({
            "transaction":{"message":{"accountKeys":[WALLET],"instructions":[{
                "program":"system","programId":"11111111111111111111111111111111",
                "parsed":{"type":"transfer"}
            }]}},
            "meta":{"err":null,"fee":5000,"innerInstructions":[]}
        });
        let native_only = json!([
            {"owner":WALLET,"mint":ASSET,"delta_amount":"100","decimals":2},
            {"owner":WALLET,"mint":WRAPPED_SOL_MINT,"delta_amount":"-200000000","decimals":9,
             "source":"native_sol","fee_adjusted":true}
        ]);
        assert_eq!(
            classify_transaction(&transfer_tx, WALLET, Some(&native_only))["classification"],
            "UNKNOWN"
        );
    }

    #[test]
    fn failed_transaction_never_becomes_a_candidate() {
        let transaction = json!({"meta":{"err":{"InstructionError":[0,"Custom"]},"fee":5000}});
        let changes = json!([
            {"owner":WALLET,"mint":ASSET,"delta_amount":"100","decimals":2},
            {"owner":WALLET,"mint":USDC_MINT,"delta_amount":"-2500000","decimals":6}
        ]);
        let signal = classify_transaction(&transaction, WALLET, Some(&changes));
        assert_eq!(signal["classification"], "FAILED_TRANSACTION");
        assert_eq!(signal["confidence"], "high");
    }

    #[test]
    fn pumpfun_v2_candidate_requires_action_context_and_matching_mints() {
        let transaction = json!({
            "transaction":{"message":{
                "accountKeys":[WALLET,PUMP_FUN_PROGRAM_ID],
                "instructions":[{
                    "programIdIndex":1,
                    "accounts":["global","BASE_MINT","QUOTE_MINT"]
                }]
            }},
            "meta":{
                "err":null,"fee":5000,"innerInstructions":[],
                "logMessages":[
                    format!("Program {} invoke [1]",PUMP_FUN_PROGRAM_ID),
                    "Program log: Instruction: BuyV2",
                    format!("Program {} success",PUMP_FUN_PROGRAM_ID)
                ]
            }
        });
        let changes = json!([
            {"owner":WALLET,"mint":"BASE_MINT","delta_amount":"100","decimals":6},
            {"owner":WALLET,"mint":"QUOTE_MINT","delta_amount":"-200","decimals":9}
        ]);
        let result = classify_transaction(&transaction, WALLET, Some(&changes));
        assert_eq!(result["classification"], "BUY_CANDIDATE");
        assert_eq!(result["evidence"]["pumpfun_base_mint"], "BASE_MINT");
    }

    #[test]
    fn missing_transaction_metadata_is_not_fabricated() {
        let result = analyze_transaction(&json!({"slot":1})).unwrap();
        assert_eq!(result["available"], false);
        assert!(result.get("status").is_none());
    }
}
