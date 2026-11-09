//! SQLite persistence for public wallet signatures and transaction observations.
//! Callers must provide an explicit database path; tests use temporary databases.
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::Value;
use std::{fs, path::{Path, PathBuf}};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum SignatureStoreError {
    #[error("Database operation failed")] Database(#[from] rusqlite::Error),
    #[error("Invalid wallet address or signature")] InvalidInput,
    #[error("Invalid processing stage or status")] InvalidState,
    #[error("Could not create database directory")] Directory(#[from] std::io::Error),
    #[error("Stored transaction JSON is invalid")] InvalidJson(#[from] serde_json::Error),
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FailureState { pub status:String, pub attempts:u32 }
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Position { pub slot:Option<i64>, pub signature:Option<String>, pub updated_at:String }
#[derive(Debug, Clone, PartialEq)]
pub struct TransactionRecord { pub raw_transaction:Option<Value>, pub status:String, pub updated_at:String }
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ErrorRecord {
 pub status:String, pub attempts:u32, pub last_checked_at:Option<String>,
 pub error_stage:Option<String>, pub error_code:Option<String>, pub error_message:Option<String>
}
pub struct SignatureStore { path:PathBuf }
impl SignatureStore {
 pub fn new(path:impl AsRef<Path>)->Result<Self,SignatureStoreError>{
   let path=path.as_ref().to_path_buf();
   if let Some(parent)=path.parent(){ if !parent.as_os_str().is_empty(){fs::create_dir_all(parent)?;} }
   let store=Self{path}; store.initialize()?; Ok(store)
 }
 fn connect(&self)->Result<Connection,SignatureStoreError>{Ok(Connection::open(&self.path)?)}
 fn initialize(&self)->Result<(),SignatureStoreError>{
  let db=self.connect()?;
  db.execute_batch("PRAGMA foreign_keys=ON;
   CREATE TABLE IF NOT EXISTS seen_signatures(
    wallet_address TEXT NOT NULL, signature TEXT NOT NULL,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'processed', attempts INTEGER NOT NULL DEFAULT 0,
    slot INTEGER, last_checked_at TEXT, error_stage TEXT, error_code TEXT, error_message TEXT,
    PRIMARY KEY(wallet_address,signature));
   CREATE TABLE IF NOT EXISTS wallet_baselines(
    wallet_address TEXT PRIMARY KEY, initialized_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
   CREATE TABLE IF NOT EXISTS wallet_positions(
    wallet_address TEXT PRIMARY KEY, slot INTEGER, signature TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
   CREATE TABLE IF NOT EXISTS transaction_records(
    wallet_address TEXT NOT NULL, signature TEXT NOT NULL, raw_transaction_json TEXT,
    processing_status TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(wallet_address,signature));")?;
  // Upgrade older databases without deleting or rewriting existing rows.
  let mut stmt=db.prepare("PRAGMA table_info(seen_signatures)")?;
  let cols=stmt.query_map([],|r|r.get::<_,String>(1))?.collect::<Result<std::collections::HashSet<_>,_>>()?;
  drop(stmt);
  for (name,ddl) in [("status","TEXT NOT NULL DEFAULT 'processed'"),("attempts","INTEGER NOT NULL DEFAULT 0"),("slot","INTEGER"),("last_checked_at","TEXT"),("error_stage","TEXT"),("error_code","TEXT"),("error_message","TEXT")] {
    if !cols.contains(name){db.execute_batch(&format!("ALTER TABLE seen_signatures ADD COLUMN {name} {ddl}"))?;}
  }
  Ok(())
 }
 fn validate(wallet:&str,signature:&str)->Result<(),SignatureStoreError>{
  if wallet.trim().is_empty()||signature.trim().is_empty(){Err(SignatureStoreError::InvalidInput)}else{Ok(())}
 }
 pub fn has_baseline(&self,wallet:&str)->Result<bool,SignatureStoreError>{
  if wallet.trim().is_empty(){return Err(SignatureStoreError::InvalidInput)}
  Ok(self.connect()?.query_row("SELECT 1 FROM wallet_baselines WHERE wallet_address=?1",[wallet],|r|r.get::<_,i64>(0)).optional()?.is_some())
 }
 pub fn mark_baseline(&self,wallet:&str)->Result<bool,SignatureStoreError>{
  if wallet.trim().is_empty(){return Err(SignatureStoreError::InvalidInput)}
  Ok(self.connect()?.execute("INSERT OR IGNORE INTO wallet_baselines(wallet_address) VALUES(?1)",[wallet])?==1)
 }
 pub fn has_seen(&self,wallet:&str,sig:&str)->Result<bool,SignatureStoreError>{
  Self::validate(wallet,sig)?;
  Ok(self.connect()?.query_row("SELECT 1 FROM seen_signatures WHERE wallet_address=?1 AND signature=?2",params![wallet,sig],|r|r.get::<_,i64>(0)).optional()?.is_some())
 }
 pub fn get_status(&self,wallet:&str,sig:&str)->Result<Option<String>,SignatureStoreError>{
  Self::validate(wallet,sig)?;
  Ok(self.connect()?.query_row("SELECT status FROM seen_signatures WHERE wallet_address=?1 AND signature=?2",params![wallet,sig],|r|r.get(0)).optional()?)
 }
 pub fn save(&self,wallet:&str,sig:&str,slot:Option<i64>)->Result<bool,SignatureStoreError>{
  Self::validate(wallet,sig)?; if slot.is_some_and(|v|v<0){return Err(SignatureStoreError::InvalidInput)}
  Ok(self.connect()?.execute("INSERT OR IGNORE INTO seen_signatures(wallet_address,signature,status,slot) VALUES(?1,?2,'pending',?3)",params![wallet,sig,slot])?==1)
 }
 pub fn save_many(&self,wallet:&str,sigs:&[String])->Result<usize,SignatureStoreError>{
  let mut db=self.connect()?; let tx=db.transaction()?; let mut count=0;
  for sig in sigs {Self::validate(wallet,sig)?; count+=tx.execute("INSERT OR IGNORE INTO seen_signatures(wallet_address,signature,status) VALUES(?1,?2,'pending')",params![wallet,sig])?;}
  tx.commit()?; Ok(count)
 }
 pub fn mark_processed(&self,wallet:&str,sig:&str)->Result<bool,SignatureStoreError>{
  Self::validate(wallet,sig)?; let mut db=self.connect()?; let tx=db.transaction()?;
  let changed=tx.execute("UPDATE seen_signatures SET status='processed',last_checked_at=CURRENT_TIMESTAMP,error_stage=NULL,error_code=NULL,error_message=NULL WHERE wallet_address=?1 AND signature=?2",params![wallet,sig])?;
  tx.execute("UPDATE transaction_records SET processing_status='processed',updated_at=CURRENT_TIMESTAMP WHERE wallet_address=?1 AND signature=?2",params![wallet,sig])?;
  if let Some(slot)=tx.query_row("SELECT slot FROM seen_signatures WHERE wallet_address=?1 AND signature=?2",params![wallet,sig],|r|r.get::<_,Option<i64>>(0)).optional()?.flatten(){
   tx.execute("INSERT INTO wallet_positions(wallet_address,slot,signature) VALUES(?1,?2,?3)
    ON CONFLICT(wallet_address) DO UPDATE SET slot=CASE WHEN wallet_positions.slot IS NULL OR excluded.slot>=wallet_positions.slot THEN excluded.slot ELSE wallet_positions.slot END,
    signature=CASE WHEN wallet_positions.slot IS NULL OR excluded.slot>=wallet_positions.slot THEN excluded.signature ELSE wallet_positions.signature END,
    updated_at=CURRENT_TIMESTAMP",params![wallet,slot,sig])?;
  }
  tx.commit()?; Ok(changed==1)
 }
 pub fn record_failure(&self,wallet:&str,sig:&str,stage:&str,code:Option<&str>,message:Option<&str>,max_attempts:u32)->Result<FailureState,SignatureStoreError>{
  Self::validate(wallet,sig)?;
  if max_attempts==0||!["discovery","rpc_fetch","interpretation","paper_simulation","persistence"].contains(&stage){return Err(SignatureStoreError::InvalidState)}
  let safe_code=code.filter(|v|!v.is_empty()).unwrap_or("processing_error");
  let safe_message=message.unwrap_or("Processing failed."); let safe_message=&safe_message[..safe_message.char_indices().map(|(i,_)|i).filter(|i|*i<=300).last().unwrap_or(0)];
  let mut db=self.connect()?; let tx=db.transaction()?;
  let old=tx.query_row("SELECT attempts FROM seen_signatures WHERE wallet_address=?1 AND signature=?2",params![wallet,sig],|r|r.get::<_,u32>(0)).optional()?;
  let attempts=old.unwrap_or(0).saturating_add(1); let status=if attempts>=max_attempts{"needs_review"}else{"pending"};
  tx.execute("INSERT INTO seen_signatures(wallet_address,signature,status,attempts,error_stage,error_code,error_message,last_checked_at)
   VALUES(?1,?2,?3,?4,?5,?6,?7,CURRENT_TIMESTAMP)
   ON CONFLICT(wallet_address,signature) DO UPDATE SET status=excluded.status,attempts=excluded.attempts,error_stage=excluded.error_stage,error_code=excluded.error_code,error_message=excluded.error_message,last_checked_at=CURRENT_TIMESTAMP",
   params![wallet,sig,status,attempts,stage,safe_code,safe_message])?;
  tx.commit()?; Ok(FailureState{status:status.into(),attempts})
 }
 pub fn save_transaction_record(&self,wallet:&str,sig:&str,raw:Option<&Value>,status:&str)->Result<(),SignatureStoreError>{
  Self::validate(wallet,sig)?;
  if !["observed","incomplete","failed","processed","needs_review"].contains(&status){return Err(SignatureStoreError::InvalidState)}
  let raw=raw.map(|v|serde_json::to_string(v)).transpose()?;
  self.connect()?.execute("INSERT INTO transaction_records(wallet_address,signature,raw_transaction_json,processing_status)
   VALUES(?1,?2,?3,?4) ON CONFLICT(wallet_address,signature) DO UPDATE SET
   raw_transaction_json=COALESCE(excluded.raw_transaction_json,transaction_records.raw_transaction_json),
   processing_status=excluded.processing_status,updated_at=CURRENT_TIMESTAMP",params![wallet,sig,raw,status])?;
  Ok(())
 }
 pub fn get_transaction_record(&self,wallet:&str,sig:&str)->Result<Option<TransactionRecord>,SignatureStoreError>{
  Self::validate(wallet,sig)?;
  let row=self.connect()?.query_row("SELECT raw_transaction_json,processing_status,updated_at FROM transaction_records WHERE wallet_address=?1 AND signature=?2",params![wallet,sig],|r|Ok((r.get::<_,Option<String>>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?))).optional()?;
  row.map(|(raw,status,updated_at)|Ok(TransactionRecord{raw_transaction:raw.map(|v|serde_json::from_str(&v)).transpose()?,status,updated_at})).transpose()
 }
 pub fn get_pending_signatures(&self,wallet:&str,limit:u32)->Result<Vec<String>,SignatureStoreError>{
  if wallet.trim().is_empty()||limit==0{return Err(SignatureStoreError::InvalidInput)}
  let db=self.connect()?;let mut q=db.prepare("SELECT signature FROM seen_signatures WHERE wallet_address=?1 AND status='pending' ORDER BY first_seen_at ASC LIMIT ?2")?;
  Ok(q.query_map(params![wallet,limit],|r|r.get(0))?.collect::<Result<Vec<_>,_>>()?)
 }
 pub fn get_seen_signatures(&self,wallet:&str,limit:u32)->Result<Vec<String>,SignatureStoreError>{
  if wallet.trim().is_empty()||limit==0{return Err(SignatureStoreError::InvalidInput)}
  let db=self.connect()?;let mut q=db.prepare("SELECT signature FROM seen_signatures WHERE wallet_address=?1 ORDER BY first_seen_at DESC LIMIT ?2")?;
  Ok(q.query_map(params![wallet,limit],|r|r.get(0))?.collect::<Result<Vec<_>,_>>()?)
 }
 pub fn get_position(&self,wallet:&str)->Result<Option<Position>,SignatureStoreError>{
  if wallet.trim().is_empty(){return Err(SignatureStoreError::InvalidInput)}
  Ok(self.connect()?.query_row("SELECT slot,signature,updated_at FROM wallet_positions WHERE wallet_address=?1",[wallet],|r|Ok(Position{slot:r.get(0)?,signature:r.get(1)?,updated_at:r.get(2)?})).optional()?)
 }
}
#[cfg(test)]
mod tests {
 use super::*;
 #[test] fn idempotent_pending_and_processed_state(){
  let dir=tempfile::tempdir().unwrap();let store=SignatureStore::new(dir.path().join("signatures.db")).unwrap();
  assert!(store.save("wallet","sig",Some(9)).unwrap());assert!(!store.save("wallet","sig",Some(9)).unwrap());
  assert_eq!(store.get_pending_signatures("wallet",10).unwrap(),vec!["sig"]);
  assert!(store.mark_processed("wallet","sig").unwrap());
  assert_eq!(store.get_status("wallet","sig").unwrap().as_deref(),Some("processed"));
  assert_eq!(store.get_position("wallet").unwrap().unwrap().slot,Some(9));
 }
 #[test] fn failures_are_bounded_and_preserve_review_state(){
  let dir=tempfile::tempdir().unwrap();let store=SignatureStore::new(dir.path().join("s.db")).unwrap();
  for _ in 0..5 {let state=store.record_failure("w","s","rpc_fetch",None,None,5).unwrap(); if state.attempts==5{assert_eq!(state.status,"needs_review");}}
 }
}
