//! Pump.fun instruction context. Paper classification only.
//!
//! V2 layout used by the Python analyzer: accounts[1] is base mint,
//! accounts[2] is quote mint. Exactly one such instruction is accepted.

use serde_json::Value;

pub const PUMP_FUN_PROGRAM: &str = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PumpContext {
    pub base_mint: String,
    pub quote_mint: String,
}

fn program_id(instruction: &Value) -> Option<&str> {
    instruction.get("programId").and_then(|v| v.as_str())
}

fn mints(instruction: &Value) -> Option<(String, String)> {
    if program_id(instruction) != Some(PUMP_FUN_PROGRAM) {
        return None;
    }
    let accounts = instruction.get("accounts")?.as_array()?;
    if accounts.len() < 3 {
        return None;
    }
    let base = accounts[1].as_str()?.to_string();
    let quote = accounts[2].as_str()?.to_string();
    if base.is_empty() || quote.is_empty() {
        return None;
    }
    Some((base, quote))
}

/// Return base/quote only when there is exactly one Pump.fun instruction.
pub fn pumpfun_context(transaction: &Value) -> Option<PumpContext> {
    let mut found = Vec::new();
    if let Some(outer) = transaction
        .pointer("/transaction/message/instructions")
        .and_then(|v| v.as_array())
    {
        for instruction in outer {
            if let Some((base, quote)) = mints(instruction) {
                found.push(PumpContext {
                    base_mint: base,
                    quote_mint: quote,
                });
            }
        }
    }
    if let Some(groups) = transaction
        .pointer("/meta/innerInstructions")
        .and_then(|v| v.as_array())
    {
        for group in groups {
            let Some(inner) = group.get("instructions").and_then(|v| v.as_array()) else {
                continue;
            };
            for instruction in inner {
                if let Some((base, quote)) = mints(instruction) {
                    found.push(PumpContext {
                        base_mint: base,
                        quote_mint: quote,
                    });
                }
            }
        }
    }
    if found.len() == 1 {
        found.pop()
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn one_instruction_is_context() {
        let tx = json!({
            "transaction": {"message": {"instructions": [{
                "programId": PUMP_FUN_PROGRAM,
                "accounts": ["user", "BaseMint1111111111111111111111111111111", "QuoteMint111111111111111111111111111111"]
            }]}},
            "meta": {"innerInstructions": []}
        });
        let ctx = pumpfun_context(&tx).unwrap();
        assert!(ctx.base_mint.starts_with("BaseMint"));
        assert!(ctx.quote_mint.starts_with("QuoteMint"));
    }

    #[test]
    fn two_instructions_are_ambiguous() {
        let ix = json!({
            "programId": PUMP_FUN_PROGRAM,
            "accounts": ["user", "A11111111111111111111111111111111111111", "B11111111111111111111111111111111111111"]
        });
        let tx = json!({
            "transaction": {"message": {"instructions": [ix.clone(), ix]}},
            "meta": {}
        });
        assert!(pumpfun_context(&tx).is_none());
    }
}
