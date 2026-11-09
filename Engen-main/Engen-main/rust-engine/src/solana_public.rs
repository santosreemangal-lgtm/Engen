//! Read-only Solana JSON-RPC helpers. No signing or transaction submission APIs.
use serde_json::{json, Value};
use thiserror::Error;
use crate::rpc_policy::{RpcClient, RpcError, RpcPolicy};

pub const DEFAULT_RPC_URL: &str = "https://api.mainnet-beta.solana.com";
const B58: &str = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";

#[derive(Debug, Error, PartialEq, Eq)]
pub enum SolanaError {
    #[error("Invalid public Solana address or transaction signature")]
    InvalidBase58,
    #[error("Invalid RPC response")]
    InvalidResponse,
    #[error("Public RPC request failed")]
    Rpc,
}
impl From<RpcError> for SolanaError { fn from(_: RpcError) -> Self { Self::Rpc } }

fn decode_base58(input: &str) -> Result<Vec<u8>, SolanaError> {
    if input.is_empty() || input.chars().any(|c| !B58.contains(c)) { return Err(SolanaError::InvalidBase58); }
    let mut bytes = vec![0u8];
    for ch in input.chars() {
        let mut carry = B58.find(ch).ok_or(SolanaError::InvalidBase58)? as u32;
        for byte in bytes.iter_mut().rev() {
            carry += (*byte as u32) * 58;
            *byte = (carry & 0xff) as u8;
            carry >>= 8;
        }
        while carry > 0 { bytes.insert(0, (carry & 0xff) as u8); carry >>= 8; }
    }
    let leading = input.chars().take_while(|c| *c == '1').count();
    let first_nonzero = bytes.iter().position(|b| *b != 0).unwrap_or(bytes.len());
    let mut decoded = vec![0u8; leading];
    decoded.extend_from_slice(&bytes[first_nonzero..]);
    Ok(decoded)
}
pub fn is_valid_public_key(value: &str) -> bool { decode_base58(value).map(|v| v.len() == 32).unwrap_or(false) }
pub fn is_valid_transaction_signature(value: &str) -> bool { decode_base58(value).map(|v| v.len() == 64).unwrap_or(false) }

#[derive(Clone)]
pub struct SolanaPublicClient { rpc: RpcClient }
impl SolanaPublicClient {
    pub fn new(url: &str, policy: RpcPolicy) -> Result<Self, SolanaError> { Ok(Self { rpc: RpcClient::new(url, policy)? }) }
    pub fn from_env(url: &str) -> Result<Self, SolanaError> { Ok(Self { rpc: RpcClient::from_env(url)? }) }
    async fn call(&self, method: &str, params: Value) -> Result<Value, SolanaError> { self.rpc.call(method, params).await.map_err(Into::into) }
    pub async fn health(&self) -> Result<String, SolanaError> {
        self.call("getHealth", json!([])).await?.as_str().map(str::to_owned).ok_or(SolanaError::InvalidResponse)
    }
    pub async fn slot(&self) -> Result<u64, SolanaError> {
        self.call("getSlot", json!([])).await?.as_u64().ok_or(SolanaError::InvalidResponse)
    }
    pub async fn latest_blockhash(&self) -> Result<String, SolanaError> {
        self.call("getLatestBlockhash", json!([{"commitment":"confirmed"}])).await?
            .get("value").and_then(|v|v.get("blockhash")).and_then(Value::as_str)
            .map(str::to_owned).ok_or(SolanaError::InvalidResponse)
    }
    pub async fn signature_status(&self, signature: &str) -> Result<Option<Value>, SolanaError> {
        if !is_valid_transaction_signature(signature) { return Err(SolanaError::InvalidBase58); }
        let result=self.call("getSignatureStatuses",json!([[signature],{"searchTransactionHistory":true}])).await?;
        let values=result.get("value").and_then(Value::as_array).ok_or(SolanaError::InvalidResponse)?;
        if values.len()!=1 { return Err(SolanaError::InvalidResponse); }
        Ok(values[0].clone())
    }
    pub async fn transaction(&self, signature: &str) -> Result<Option<Value>, SolanaError> {
        if !is_valid_transaction_signature(signature) { return Err(SolanaError::InvalidBase58); }
        let result=self.call("getTransaction",json!([signature,{"encoding":"jsonParsed","commitment":"confirmed","maxSupportedTransactionVersion":0}])).await?;
        if result.is_null() || result.is_object() { Ok(if result.is_null(){None}else{Some(result)}) } else { Err(SolanaError::InvalidResponse) }
    }
}
#[cfg(test)]
mod tests {
 use super::*;
 #[test] fn validates_base58_lengths() {
   assert!(is_valid_public_key("11111111111111111111111111111111"));
   assert!(!is_valid_transaction_signature("not-a-signature"));
   assert!(!is_valid_public_key("111"));
 }
}
