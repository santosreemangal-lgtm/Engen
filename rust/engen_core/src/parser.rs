//! Conservative token-delta classifier.
//!
//! Quote set matches the Python analyzer: USDC, USDT, wrapped SOL.
//! Native SOL (`source=native_sol`, mint=wSOL, decimals=9) counts as the quote
//! side and must not be mixed with an SPL quote movement.
//! One asset mint and one quote side only. Ambiguous input is UNKNOWN.

use crate::classifier::{classify_swap, BalanceDelta, RawSwapHint};
use crate::paper_simulator::OrderSide;
use serde_json::{json, Value};

pub const WRAPPED_SOL: &str = "So11111111111111111111111111111111111111112";
pub const USDC: &str = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";
pub const USDT: &str = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB";

fn is_spl_quote(mint: &str) -> bool {
    mint == WRAPPED_SOL || mint == USDC || mint == USDT
}

/// Classify owned token deltas. Items use `owner`, `mint`, `delta_amount`.
/// Optional `source=native_sol` with `decimals=9` is native SOL, not an SPL quote.
pub fn classify_balance_changes(wallet: &str, changes: &[Value]) -> Value {
    if wallet.trim().is_empty() {
        return unknown("A monitored wallet address is required.");
    }
    let mut asset: Option<(String, i128)> = None;
    let mut quote: Option<(String, i128)> = None;
    let mut native_sol: Option<i128> = None;

    for change in changes {
        if change.get("owner").and_then(|v| v.as_str()) != Some(wallet) {
            continue;
        }
        let Some(mint) = change.get("mint").and_then(|v| v.as_str()) else {
            continue;
        };
        let Some(raw) = change.get("delta_amount").and_then(|v| v.as_str()) else {
            continue;
        };
        let Ok(delta) = raw.parse::<i128>() else {
            continue;
        };
        if delta == 0 {
            continue;
        }
        let native = change.get("source").and_then(|v| v.as_str()) == Some("native_sol");
        if native {
            if mint != WRAPPED_SOL || change.get("decimals").and_then(|v| v.as_i64()) != Some(9) {
                return unknown("Native SOL balance change is invalid.");
            }
            native_sol = Some(native_sol.unwrap_or(0) + delta);
            continue;
        }
        if is_spl_quote(mint) {
            if quote.is_some() {
                return unknown("More than one quote mint moved.");
            }
            quote = Some((mint.to_string(), delta));
        } else if asset.is_some() {
            return unknown("More than one asset mint moved.");
        } else {
            asset = Some((mint.to_string(), delta));
        }
    }

    if native_sol.is_some() && quote.is_some() {
        return unknown("Both native SOL and SPL quote-token movements are present.");
    }
    if quote.is_none() {
        if let Some(delta) = native_sol {
            quote = Some((WRAPPED_SOL.to_string(), delta));
        }
    }

    let (Some((asset_mint, asset_delta)), Some((quote_mint, quote_delta))) = (asset, quote)
    else {
        return unknown("Need exactly one asset mint and one quote mint.");
    };

    let hint = RawSwapHint {
        signature: String::new(),
        wallet: wallet.to_string(),
        block_time: None,
        confirmation_status: "finalized".into(),
        metadata_complete: true,
        asset_mint: asset_mint.clone(),
        quote_mint: quote_mint.clone(),
        deltas: vec![
            BalanceDelta {
                owner: wallet.to_string(),
                mint: asset_mint.clone(),
                delta: asset_delta.to_string(),
            },
            BalanceDelta {
                owner: wallet.to_string(),
                mint: quote_mint.clone(),
                delta: quote_delta.to_string(),
            },
        ],
    };
    match classify_swap(&hint) {
        Some(candidate) => {
            let classification = match candidate.side {
                OrderSide::Buy => "BUY_CANDIDATE",
                OrderSide::Sell => "SELL_CANDIDATE",
            };
            json!({
                "classification": classification,
                "asset_mints": [asset_mint],
                "quote_mints": [quote_mint],
                "real_trading": false,
                "transaction_broadcast": false
            })
        }
        None => unknown("Deltas are not an unambiguous buy or sell."),
    }
}

fn unknown(reason: &str) -> Value {
    json!({
        "classification": "UNKNOWN",
        "reason": reason,
        "asset_mints": [],
        "quote_mints": [],
        "real_trading": false,
        "transaction_broadcast": false
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn usdc_buy() {
        let wallet = "Wallet11111111111111111111111111111111";
        let changes = vec![
            json!({"owner": wallet, "mint": "TokenMint111111111111111111111111111111", "delta_amount": "1000"}),
            json!({"owner": wallet, "mint": USDC, "delta_amount": "-500"}),
        ];
        let signal = classify_balance_changes(wallet, &changes);
        assert_eq!(signal["classification"], "BUY_CANDIDATE");
    }

    #[test]
    fn same_direction_is_unknown() {
        let wallet = "Wallet11111111111111111111111111111111";
        let changes = vec![
            json!({"owner": wallet, "mint": "TokenMint111111111111111111111111111111", "delta_amount": "1000"}),
            json!({"owner": wallet, "mint": USDC, "delta_amount": "500"}),
        ];
        assert_eq!(
            classify_balance_changes(wallet, &changes)["classification"],
            "UNKNOWN"
        );
    }

    #[test]
    fn native_sol_buy() {
        let wallet = "Wallet11111111111111111111111111111111";
        let changes = vec![
            json!({"owner": wallet, "mint": "TokenMint111111111111111111111111111111", "delta_amount": "1000"}),
            json!({"owner": wallet, "mint": WRAPPED_SOL, "delta_amount": "-900000000", "decimals": 9, "source": "native_sol"}),
        ];
        assert_eq!(
            classify_balance_changes(wallet, &changes)["classification"],
            "BUY_CANDIDATE"
        );
    }

    #[test]
    fn native_sol_mixed_with_usdc_is_unknown() {
        let wallet = "Wallet11111111111111111111111111111111";
        let changes = vec![
            json!({"owner": wallet, "mint": "TokenMint111111111111111111111111111111", "delta_amount": "1000"}),
            json!({"owner": wallet, "mint": USDC, "delta_amount": "-5"}),
            json!({"owner": wallet, "mint": WRAPPED_SOL, "delta_amount": "-1", "decimals": 9, "source": "native_sol"}),
        ];
        assert_eq!(
            classify_balance_changes(wallet, &changes)["classification"],
            "UNKNOWN"
        );
    }
}
