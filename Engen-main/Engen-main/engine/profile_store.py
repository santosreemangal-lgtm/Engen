"""SQLite persistence for read-only Solana profile and portfolio data."""
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path("data/profiles.db")
LABELS = {"BUY_CANDIDATE", "SELL_CANDIDATE", "TRANSFER", "UNKNOWN",
          "FAILED_TRANSACTION", "RENT", "LIQUIDITY_OPERATION"}
STATUSES = {"observed", "confirmed", "failed", "pending", "unknown"}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def exact_amount(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError("Amounts must be exact integer or decimal strings.")
    value = str(value).strip()
    if not value or any(c not in "0123456789.-" for c in value):
        raise ValueError("Invalid exact amount.")
    if value.count("-") > 1 or ("-" in value and not value.startswith("-")) or value.count(".") > 1:
        raise ValueError("Invalid exact amount.")
    return value


class ProfileStore:
    """SQLite store. Profiles are disabled by default and deletions are soft."""

    def __init__(self, db_path=DEFAULT_DB_PATH):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def initialize(self):
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise RuntimeError("Database schema is newer than this application.")
            if version == 0:
                db.executescript("""
                CREATE TABLE profiles(
                  profile_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                  destination_public_address TEXT,
                  status TEXT NOT NULL DEFAULT 'disabled'
                    CHECK(status IN ('disabled','active','archived')),
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT);
                CREATE TABLE profile_settings(
                  profile_id TEXT PRIMARY KEY REFERENCES profiles(profile_id),
                  settings_json TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE source_wallets(
                  wallet_address TEXT PRIMARY KEY, created_at TEXT NOT NULL);
                CREATE TABLE profile_source_wallets(
                  profile_id TEXT REFERENCES profiles(profile_id),
                  wallet_address TEXT REFERENCES source_wallets(wallet_address),
                  associated_at TEXT NOT NULL, PRIMARY KEY(profile_id,wallet_address));
                CREATE TABLE transactions(
                  signature TEXT PRIMARY KEY, source_wallet_address TEXT NOT NULL,
                  slot INTEGER, block_time INTEGER, observed_at TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN
                    ('observed','confirmed','failed','pending','unknown')),
                  classification TEXT NOT NULL CHECK(classification IN
                    ('BUY_CANDIDATE','SELL_CANDIDATE','TRANSFER','UNKNOWN',
                     'FAILED_TRANSACTION','RENT','LIQUIDITY_OPERATION')),
                  evidence_json TEXT NOT NULL, raw_amount TEXT, token_mint TEXT,
                  token_decimals INTEGER,
                  CHECK(slot IS NULL OR slot>=0),
                  CHECK(token_decimals IS NULL OR token_decimals BETWEEN 0 AND 30));
                CREATE TABLE profile_transactions(
                  profile_id TEXT REFERENCES profiles(profile_id),
                  signature TEXT REFERENCES transactions(signature),
                  linked_at TEXT NOT NULL, PRIMARY KEY(profile_id,signature));
                CREATE TABLE balance_snapshots(
                  snapshot_id TEXT PRIMARY KEY, profile_id TEXT REFERENCES profiles(profile_id),
                  wallet_address TEXT NOT NULL, token_mint TEXT NOT NULL,
                  raw_amount TEXT NOT NULL, token_decimals INTEGER NOT NULL,
                  observed_at TEXT NOT NULL, source TEXT NOT NULL,
                  freshness_seconds INTEGER, is_current INTEGER NOT NULL DEFAULT 0
                    CHECK(is_current IN (0,1)));
                CREATE INDEX idx_balance_history ON balance_snapshots(profile_id,token_mint,observed_at);
                CREATE INDEX idx_transactions_observed ON transactions(observed_at);
                """)
                db.execute("PRAGMA user_version=1")

    def create_profile(self, name, destination_public_address=None, settings=None):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError("Profile name must contain 1–80 characters.")
        if destination_public_address is not None and (
            not isinstance(destination_public_address, str) or not destination_public_address.strip()
        ):
            raise ValueError("Destination must be a public address string.")
        settings = {} if settings is None else settings
        if not isinstance(settings, dict):
            raise ValueError("Settings must be an object.")
        forbidden = {"private_key","secret_key","seed_phrase","recovery_phrase","mnemonic"}
        if forbidden.intersection(str(k).lower() for k in settings):
            raise ValueError("Secret authorization fields cannot be stored in profile settings.")
        settings_json = json.dumps(settings, sort_keys=True, allow_nan=False)
        pid, now = str(uuid.uuid4()), utc_now()
        with self.connect() as db:
            db.execute("INSERT INTO profiles VALUES(?,?,?,'disabled',?,?,NULL)",
                       (pid,name.strip(),destination_public_address,now,now))
            db.execute("INSERT INTO profile_settings VALUES(?,?,?)",(pid,settings_json,now))
        return pid

    def get_profile(self, profile_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM profiles WHERE profile_id=? AND deleted_at IS NULL",
                             (profile_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            s = db.execute("SELECT settings_json FROM profile_settings WHERE profile_id=?",
                           (profile_id,)).fetchone()
            result["settings"] = json.loads(s["settings_json"]) if s else {}
            result["source_wallets"] = [r[0] for r in db.execute(
                "SELECT wallet_address FROM profile_source_wallets WHERE profile_id=? ORDER BY wallet_address",
                (profile_id,))]
            return result

    def set_settings(self, profile_id, settings):
        if not isinstance(settings, dict):
            raise ValueError("Settings must be an object.")
        forbidden = {"private_key","secret_key","seed_phrase","recovery_phrase","mnemonic"}
        if forbidden.intersection(str(k).lower() for k in settings):
            raise ValueError("Secret authorization fields cannot be stored in profile settings.")
        payload, now = json.dumps(settings,sort_keys=True,allow_nan=False),utc_now()
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM profiles WHERE profile_id=? AND deleted_at IS NULL",
                              (profile_id,)).fetchone():
                raise ValueError("Profile not found.")
            db.execute("UPDATE profile_settings SET settings_json=?,updated_at=? WHERE profile_id=?",
                       (payload,now,profile_id))
            db.execute("UPDATE profiles SET updated_at=? WHERE profile_id=?",(now,profile_id))

    def associate_source_wallet(self, profile_id, wallet_address):
        if not isinstance(wallet_address,str) or not wallet_address.strip():
            raise ValueError("Source wallet address is required.")
        now = utc_now()
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM profiles WHERE profile_id=? AND deleted_at IS NULL",
                              (profile_id,)).fetchone():
                raise ValueError("Profile not found.")
            db.execute("INSERT OR IGNORE INTO source_wallets VALUES(?,?)",(wallet_address,now))
            db.execute("INSERT OR IGNORE INTO profile_source_wallets VALUES(?,?,?)",
                       (profile_id,wallet_address,now))

    def save_transaction(self, signature, source_wallet_address, *, classification="UNKNOWN",
                         status="unknown", evidence=None, slot=None, block_time=None,
                         raw_amount=None, token_mint=None, token_decimals=None,
                         profile_ids=(), observed_at=None):
        if not isinstance(signature,str) or not signature.strip():
            raise ValueError("Transaction signature is required.")
        if classification not in LABELS or status not in STATUSES:
            raise ValueError("Invalid transaction label or status.")
        if slot is not None and (not isinstance(slot,int) or isinstance(slot,bool) or slot<0):
            raise ValueError("Invalid slot.")
        if token_decimals is not None and (not isinstance(token_decimals,int) or isinstance(token_decimals,bool) or not 0<=token_decimals<=30):
            raise ValueError("Invalid token decimals.")
        amount = None if raw_amount is None else exact_amount(raw_amount)
        evidence_json = json.dumps(evidence or {},sort_keys=True,allow_nan=False)
        now = observed_at or utc_now()
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO source_wallets VALUES(?,?)",(source_wallet_address,utc_now()))
            db.execute("""INSERT OR IGNORE INTO transactions
              VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
              (signature,source_wallet_address,slot,block_time,now,status,classification,
               evidence_json,amount,token_mint,token_decimals))
            stored=db.execute("SELECT source_wallet_address FROM transactions WHERE signature=?",
                              (signature,)).fetchone()
            if stored["source_wallet_address"] != source_wallet_address:
                raise ValueError("Signature already stored for a different source wallet.")
            for pid in dict.fromkeys(profile_ids):
                if not db.execute("SELECT 1 FROM profiles WHERE profile_id=? AND deleted_at IS NULL",
                                  (pid,)).fetchone():
                    raise ValueError("Profile not found.")
                if not db.execute("SELECT 1 FROM profile_source_wallets WHERE profile_id=? AND wallet_address=?",
                                  (pid,source_wallet_address)).fetchone():
                    raise ValueError("Associate source wallet with profile before linking transaction.")
                db.execute("INSERT OR IGNORE INTO profile_transactions VALUES(?,?,?)",
                           (pid,signature,utc_now()))
        return signature

    def save_balance_snapshot(self, profile_id, wallet_address, token_mint, raw_amount,
                               token_decimals, *, source, observed_at=None,
                               freshness_seconds=None, is_current=True):
        amount = exact_amount(raw_amount)
        if not isinstance(token_decimals,int) or isinstance(token_decimals,bool) or not 0<=token_decimals<=30:
            raise ValueError("Invalid token decimals.")
        if not isinstance(source,str) or not source.strip():
            raise ValueError("Balance source/provenance is required.")
        if freshness_seconds is not None and (not isinstance(freshness_seconds,int) or freshness_seconds<0):
            raise ValueError("Invalid freshness.")
        sid, now = str(uuid.uuid4()), observed_at or utc_now()
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM profiles WHERE profile_id=? AND deleted_at IS NULL",(profile_id,)).fetchone():
                raise ValueError("Profile not found.")
            if not db.execute("SELECT 1 FROM profile_source_wallets WHERE profile_id=? AND wallet_address=?",
                              (profile_id,wallet_address)).fetchone():
                raise ValueError("Wallet is not associated with profile.")
            if is_current:
                db.execute("UPDATE balance_snapshots SET is_current=0 WHERE profile_id=? AND wallet_address=? AND token_mint=?",
                           (profile_id,wallet_address,token_mint))
            db.execute("INSERT INTO balance_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (sid,profile_id,wallet_address,token_mint,amount,token_decimals,now,
                        source.strip(),freshness_seconds,int(bool(is_current))))
        return sid

    def current_balances(self, profile_id):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM balance_snapshots WHERE profile_id=? AND is_current=1 ORDER BY token_mint",
                (profile_id,))]

    def delete_profile(self, profile_id):
        now=utc_now()
        with self.connect() as db:
            cur=db.execute("UPDATE profiles SET deleted_at=?,updated_at=?,status='archived' WHERE profile_id=? AND deleted_at IS NULL",
                           (now,now,profile_id))
            if cur.rowcount != 1:
                raise ValueError("Profile not found or already deleted.")
        return True
