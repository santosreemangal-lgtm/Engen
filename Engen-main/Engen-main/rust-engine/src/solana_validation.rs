//! Base58 length validation for public Solana keys and signatures.

use thiserror::Error;

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum SolanaValueError {
    #[error("Base58 value must be a non-empty string.")]
    Empty,
    #[error("Invalid Base58 value.")]
    InvalidBase58,
    #[error("Solana public address must decode to exactly 32 bytes.")]
    InvalidAddressLength,
    #[error("Solana transaction signature must decode to exactly 64 bytes.")]
    InvalidSignatureLength,
}

pub fn decode_base58(value: &str) -> Result<Vec<u8>, SolanaValueError> {
    if value.is_empty() {
        return Err(SolanaValueError::Empty);
    }
    bs58::decode(value)
        .into_vec()
        .map_err(|_| SolanaValueError::InvalidBase58)
}

pub fn is_valid_solana_address(value: &str) -> bool {
    decode_base58(value).is_ok_and(|decoded| decoded.len() == 32)
}

pub fn is_valid_transaction_signature(value: &str) -> bool {
    decode_base58(value).is_ok_and(|decoded| decoded.len() == 64)
}

pub fn require_solana_address(value: &str) -> Result<&str, SolanaValueError> {
    let decoded = decode_base58(value)?;
    if decoded.len() != 32 {
        return Err(SolanaValueError::InvalidAddressLength);
    }
    Ok(value)
}

pub fn require_transaction_signature(value: &str) -> Result<&str, SolanaValueError> {
    let decoded = decode_base58(value)?;
    if decoded.len() != 64 {
        return Err(SolanaValueError::InvalidSignatureLength);
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validates_public_key_length() {
        assert!(is_valid_solana_address("11111111111111111111111111111111"));
        assert!(!is_valid_solana_address("not-a-solana-address"));
    }

    #[test]
    fn validates_signature_length() {
        let signature = "1".repeat(64);
        assert!(is_valid_transaction_signature(&signature));
        assert!(!is_valid_transaction_signature("11111111111111111111111111111111"));
    }

    #[test]
    fn distinguishes_invalid_base58_from_wrong_length() {
        assert_eq!(require_solana_address("0"), Err(SolanaValueError::InvalidBase58));
        assert_eq!(
            require_solana_address("1"),
            Err(SolanaValueError::InvalidAddressLength)
        );
    }
}
