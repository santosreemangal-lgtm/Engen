use thiserror::Error;

pub type Result<T> = std::result::Result<T, EngenError>;

#[derive(Debug, Error)]
pub enum EngenError {
    #[error("invalid input: {0}")]
    InvalidInput(String),
    #[error("not found: {0}")]
    NotFound(String),
    #[error("database: {0}")]
    Database(String),
    #[error("stale source transaction")]
    StaleSource,
    #[error("paper only: signing and broadcast are disabled")]
    LiveDisabled,
    #[error("{0}")]
    Other(String),
}

impl From<rusqlite::Error> for EngenError {
    fn from(value: rusqlite::Error) -> Self {
        EngenError::Database(value.to_string())
    }
}
