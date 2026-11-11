//! Conservative swap classifier. Paper candidates only.
//!
//! A classified BUY requires the watched wallet's asset delta > 0 and quote delta < 0.
//! A SELL is the reverse. Anything else is ignored.

use crate::paper_copy::MonitorCandidate;
use crate::paper_simulator::OrderSide;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BalanceDelta {
    pub owner: String,
    pub mint: String,
    pub delta: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RawSwapHint {
    pub signature: String,
    pub wallet: String,
    pub block_time: Option<i64>,
    pub confirmation_status: String,
    pub metadata_complete: bool,
    pub asset_mint: String,
    pub quote_mint: String,
    pub deltas: Vec<BalanceDelta>,
}

fn parse_delta(raw: &str) -> Option<i128> {
    raw.parse().ok()
}

/// Returns a paper candidate only for an unambiguous two-sided swap.
pub fn classify_swap(hint: &RawSwapHint) -> Option<MonitorCandidate> {
    if !hint.metadata_complete {
        return None;
    }
    if hint.confirmation_status != "confirmed" && hint.confirmation_status != "finalized" {
        return None;
    }
    let asset = hint
        .deltas
        .iter()
        .find(|d| d.owner == hint.wallet && d.mint == hint.asset_mint)
        .and_then(|d| parse_delta(&d.delta))?;
    let quote = hint
        .deltas
        .iter()
        .find(|d| d.owner == hint.wallet && d.mint == hint.quote_mint)
        .and_then(|d| parse_delta(&d.delta))?;
    let side = if asset > 0 && quote < 0 {
        OrderSide::Buy
    } else if asset < 0 && quote > 0 {
        OrderSide::Sell
    } else {
        return None;
    };
    Some(MonitorCandidate {
        signature: hint.signature.clone(),
        wallet: hint.wallet.clone(),
        side: side.clone(),
        asset_mint: hint.asset_mint.clone(),
        block_time: hint.block_time,
        confirmation_status: hint.confirmation_status.clone(),
        metadata_complete: true,
        target_notional_usd: None,
        source_sell_fraction: if matches!(side, OrderSide::Sell) {
            Some("1".into())
        } else {
            None
        },
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hint(asset: &str, quote: &str) -> RawSwapHint {
        RawSwapHint {
            signature: "sig".into(),
            wallet: "W".into(),
            block_time: Some(1),
            confirmation_status: "finalized".into(),
            metadata_complete: true,
            asset_mint: "A".into(),
            quote_mint: "Q".into(),
            deltas: vec![
                BalanceDelta {
                    owner: "W".into(),
                    mint: "A".into(),
                    delta: asset.into(),
                },
                BalanceDelta {
                    owner: "W".into(),
                    mint: "Q".into(),
                    delta: quote.into(),
                },
            ],
        }
    }

    #[test]
    fn buy_and_sell() {
        assert!(matches!(
            classify_swap(&hint("10", "-5")).unwrap().side,
            OrderSide::Buy
        ));
        assert!(matches!(
            classify_swap(&hint("-10", "5")).unwrap().side,
            OrderSide::Sell
        ));
        assert!(classify_swap(&hint("10", "5")).is_none());
    }
}
