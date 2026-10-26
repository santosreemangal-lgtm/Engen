"""SQLite persistence for read-only Solana profile and portfolio data."""
import json
import re
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

DEFAULT_DB_PATH = Path("data/profiles.db")
LABELS = {"BUY_CANDIDATE", "SELL_CANDIDATE", "TRANSFER", "UNKNOWN",
          "FAILED_TRANSACTION", "RENT", "LIQUIDITY_OPERATION"}
STATUSES = {"observed", "confirmed", "failed", "pending", "unknown"}
ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def validate_public_address(value, field="wallet address"):
    if not isinstance(value, str) or not ADDRESS_RE.fullmatch(value):
        raise ValueError(f"Invalid Solana {field}.")
    number = 0
    for char in value:
        number = number * 58 + BASE58.index(char)
    decoded = bytearray()
    while number:
        number, remainder = divmod(number, 256)
        decoded.append(remainder)
    decoded.reverse()
    decoded = bytearray(len(value) - len(value.lstrip("1"))) + decoded
    if len(decoded) != 32:
        raise ValueError(f"Invalid Solana {field}.")
    return value


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def exact_amount(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError("Amounts must be exact integer or decimal strings.")
    text = str(value).strip()
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("Invalid exact amount.") from exc
    if not parsed.is_finite():
        raise ValueError("Amount must be finite.")
    if parsed != parsed.to_integral_value():
        raise ValueError("Raw on-chain amounts must be integer strings.")
    return text


def validate_timestamp(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Timestamp must be a non-empty ISO-8601 string.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Timestamp must be ISO-8601.") from exc
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a timezone.")
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

    @contextmanager
    def session(self):
        with closing(self.connect()) as db:
            with db:
                yield db

    def initialize(self):
        with self.session() as db:
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
                  profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
                  wallet_address TEXT NOT NULL REFERENCES source_wallets(wallet_address),
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
                  profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
                  signature TEXT NOT NULL REFERENCES transactions(signature),
                  linked_at TEXT NOT NULL, PRIMARY KEY(profile_id,signature));
                CREATE TABLE balance_snapshots(
                  snapshot_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
                  wallet_address TEXT NOT NULL REFERENCES source_wallets(wallet_address), token_mint TEXT NOT NULL,
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
        if destination_public_address is not None:
            validate_public_address(destination_public_address, "destination public address")
        settings = {} if settings is None else settings
        if not isinstance(settings, dict):
            raise ValueError("Settings must be an object.")
        forbidden = {"private_key","secret_key","seed_phrase","recovery_phrase","mnemonic"}
        if forbidden.intersection(str(k).lower() for k in settings):
            raise ValueError("Secret authorization fields cannot be stored in profile settings.")
        settings_json = json.dumps(settings, sort_keys=True, allow_nan=False)
        pid, now = str(uuid.uuid4()), utc_now()
        with self.session() as db:
            db.execute("INSERT INTO profiles VALUES(?,?,?,'disabled',?,?,NULL)",
                       (pid,name.strip(),destination_public_address,now,now))
            db.execute("INSERT INTO profile_settings VALUES(?,?,?)",(pid,settings_json,now))
        return pid

    def get_profile(self, profile_id):
        with self.session() as db:
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

    def list_profiles(self, include_archived=False):
        with self.session() as db:
            query = "SELECT profile_id FROM profiles WHERE deleted_at IS NULL"
            if not include_archived:
                query += " AND status != 'archived'"
            query += " ORDER BY created_at, profile_id"
            ids = [row["profile_id"] for row in db.execute(query)]
        return [self.get_profile(pid) for pid in ids]

    def update_profile(self, profile_id, *, name=None,
                       destination_public_address=None, status=None):
        updates, values = [], []
        if name is not None:
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
                raise ValueError("Profile name must contain 1–80 characters.")
            updates.append("name=?")
            values.append(name.strip())
        if destination_public_address is not None:
            validate_public_address(destination_public_address, "destination public address")
            updates.append("destination_public_address=?")
            values.append(destination_public_address)
        if status is not None:
            if status not in {"disabled", "active", "archived"}:
                raise ValueError("Invalid profile status.")
            updates.append("status=?")
            values.append(status)
        if not updates:
            return self.get_profile(profile_id)
        updates.append("updated_at=?")
        values.extend([utc_now(), profile_id])
        with self.session() as db:
            cur = db.execute(
                "UPDATE profiles SET " + ", ".join(updates) +
                " WHERE profile_id=? AND deleted_at IS NULL", values
            )
            if cur.rowcount != 1:
                raise ValueError("Profile not found.")
        return self.get_profile(profile_id)

    def get_transaction(self, signature, profile_id=None):
        with self.session() as db:
            if profile_id is None:
                row = db.execute("SELECT * FROM transactions WHERE signature=?",
                                 (signature,)).fetchone()
            else:
                row = db.execute(
                    """SELECT t.* FROM transactions t JOIN profile_transactions p
                       ON p.signature=t.signature
                       WHERE t.signature=? AND p.profile_id=?""",
                    (signature, profile_id),
                ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["evidence"] = json.loads(result.pop("evidence_json"))
        return result

    def set_settings(self, profile_id, settings):
        if not isinstance(settings, dict):
            raise ValueError("Settings must be an object.")
        forbidden = {"private_key","secret_key","seed_phrase","recovery_phrase","mnemonic"}
        if forbidden.intersection(str(k).lower() for k in settings):
            raise ValueError("Secret authorization fields cannot be stored in profile settings.")
        payload, now = json.dumps(settings,sort_keys=True,allow_nan=False),utc_now()
        with self.session() as db:
            if not db.execute("SELECT 1 FROM profiles WHERE profile_id=? AND deleted_at IS NULL",
                              (profile_id,)).fetchone():
                raise ValueError("Profile not found.")
            db.execute("UPDATE profile_settings SET settings_json=?,updated_at=? WHERE profile_id=?",
                       (payload,now,profile_id))
            db.execute("UPDATE profiles SET updated_at=? WHERE profile_id=?",(now,profile_id))

    def associate_source_wallet(self, profile_id, wallet_address):
        validate_public_address(wallet_address)
        now = utc_now()
        with self.session() as db:
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
        validate_public_address(source_wallet_address)
        if classification not in LABELS or status not in STATUSES:
            raise ValueError("Invalid transaction label or status.")
        if slot is not None and (not isinstance(slot,int) or isinstance(slot,bool) or slot<0):
            raise ValueError("Invalid slot.")
        if token_decimals is not None and (not isinstance(token_decimals,int) or isinstance(token_decimals,bool) or not 0<=token_decimals<=30):
            raise ValueError("Invalid token decimals.")
        amount = None if raw_amount is None else exact_amount(raw_amount)
        if token_mint is not None:
            validate_public_address(token_mint, "token mint")
        evidence_json = json.dumps(evidence or {},sort_keys=True,allow_nan=False)
        now = validate_timestamp(observed_at or utc_now())
        if block_time is not None and (not isinstance(block_time, int) or isinstance(block_time, bool) or block_time < 0):
            raise ValueError("Invalid block time.")
        with self.session() as db:
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
        validate_public_address(wallet_address)
        validate_public_address(token_mint, "token mint")
        if not isinstance(token_decimals,int) or isinstance(token_decimals,bool) or not 0<=token_decimals<=30:
            raise ValueError("Invalid token decimals.")
        if not isinstance(source,str) or not source.strip():
            raise ValueError("Balance source/provenance is required.")
        if freshness_seconds is not None and (not isinstance(freshness_seconds,int) or freshness_seconds<0):
            raise ValueError("Invalid freshness.")
        sid, now = str(uuid.uuid4()), validate_timestamp(observed_at or utc_now())
        with self.session() as db:
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
        with self.session() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM balance_snapshots WHERE profile_id=? AND is_current=1 ORDER BY token_mint",
                (profile_id,))]

    def list_transactions(self, profile_id):
        """Return transaction records linked to one profile only."""
        with self.session() as db:
            rows = db.execute(
                """SELECT t.* FROM transactions t
                   JOIN profile_transactions p ON p.signature=t.signature
                   WHERE p.profile_id=? ORDER BY t.observed_at DESC, t.signature""",
                (profile_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json"))
            result.append(item)
        return result

    def portfolio_holdings(self, profile_id, *, max_age_seconds=None):
        """Aggregate current snapshots without float rounding; retain zero balances."""
        if max_age_seconds is not None and (
            not isinstance(max_age_seconds, int) or isinstance(max_age_seconds, bool)
            or max_age_seconds < 0
        ):
            raise ValueError("max_age_seconds must be a non-negative integer.")
        rows = self.current_balances(profile_id)
        now = datetime.now(timezone.utc)
        totals = {}
        for row in rows:
            try:
                observed = datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            if observed.tzinfo is None:
                continue
            age = max(0, int((now - observed).total_seconds()))
            if max_age_seconds is not None and age > max_age_seconds:
                continue
            mint = row["token_mint"]
            decimals = row["token_decimals"]
            entry = totals.setdefault(mint, {
                "token_mint": mint, "token_decimals": decimals,
                "raw_amount": 0, "amount": Decimal("0"),
                "wallet_count": 0, "oldest_observed_at": row["observed_at"],
                "newest_observed_at": row["observed_at"], "metadata": None,
                "provenance": [],
            })
            if entry["token_decimals"] != decimals:
                raise ValueError("Conflicting decimals for the same token mint.")
            entry["raw_amount"] += int(Decimal(row["raw_amount"]))
            entry["amount"] += Decimal(row["raw_amount"]) / (Decimal(10) ** decimals)
            entry["wallet_count"] += 1
            entry["oldest_observed_at"] = min(entry["oldest_observed_at"], row["observed_at"])
            entry["newest_observed_at"] = max(entry["newest_observed_at"], row["observed_at"])
            entry["provenance"].append(row["source"])
        return [
            {**item, "raw_amount": str(item["raw_amount"]),
             "amount": format(item["amount"], "f"),
             "provenance": sorted(set(item["provenance"]))}
            for item in sorted(totals.values(), key=lambda item: item["token_mint"])
        ]

    def backup_to(self, destination_path):
        """Create a consistent SQLite backup, including committed WAL content."""
        destination = Path(destination_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_db = self.connect()
        target_db = sqlite3.connect(str(destination))
        try:
            source_db.backup(target_db)
        finally:
            target_db.close()
            source_db.close()
        return str(destination)

    def delete_profile(self, profile_id):
        now=utc_now()
        with self.session() as db:
            cur=db.execute("UPDATE profiles SET deleted_at=?,updated_at=?,status='archived' WHERE profile_id=? AND deleted_at IS NULL",
                           (now,now,profile_id))
            if cur.rowcount != 1:
                raise ValueError("Profile not found or already deleted.")
        return True
