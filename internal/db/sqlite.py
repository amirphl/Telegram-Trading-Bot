import sqlite3
import time
from datetime import datetime, timezone

from internal.services.blocking import BlockingWorkPool

DDL_MESSAGES = """
CREATE TABLE IF NOT EXISTS messages (
  chat_id            INTEGER NOT NULL,
  message_id         INTEGER NOT NULL,
  date_utc           TEXT NOT NULL,
  edit_date_utc      TEXT,
  text               TEXT,
  views              INTEGER,
  forwards           INTEGER,
  replies_count      INTEGER,
  post_author        TEXT,
  grouped_id         INTEGER,
  reply_to_msg_id    INTEGER,
  fwd_from_raw       TEXT,
  via_bot_id         INTEGER,
  entities_raw       TEXT,
  raw_json           TEXT NOT NULL,
  persistence_status TEXT NOT NULL DEFAULT 'complete'
    CHECK(persistence_status IN ('pending_media', 'complete', 'rejected', 'repair_required')),
  persistence_error  TEXT,
  persistence_updated_at_utc TEXT,
  CHECK(views IS NULL OR views >= 0),
  CHECK(forwards IS NULL OR forwards >= 0),
  CHECK(replies_count IS NULL OR replies_count >= 0),
  PRIMARY KEY (chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS media_files (
  chat_id        INTEGER NOT NULL,
  message_id     INTEGER NOT NULL,
  file_name      TEXT NOT NULL,
  mime_type      TEXT,
  file_size      INTEGER CHECK(file_size IS NULL OR file_size >= 0),
  local_path     TEXT NOT NULL,
  PRIMARY KEY (chat_id, message_id, file_name),
  FOREIGN KEY (chat_id, message_id)
    REFERENCES messages(chat_id, message_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS trade_signals (
  chat_id            INTEGER NOT NULL,
  message_id         INTEGER NOT NULL,
  token              TEXT,
  position_type      TEXT,
  entry_price        REAL,
  leverage           REAL,
  stop_losses_json   TEXT,
  take_profits_json  TEXT,
  model_name         TEXT,
  created_at_utc     TEXT NOT NULL,
  CHECK(position_type IS NULL OR position_type IN ('long', 'short')),
  CHECK(entry_price IS NULL OR entry_price > 0),
  CHECK(leverage IS NULL OR leverage > 0),
  PRIMARY KEY (chat_id, message_id),
  FOREIGN KEY (chat_id, message_id)
    REFERENCES messages(chat_id, message_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS positions_submitted (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id            INTEGER NOT NULL,
  message_id         INTEGER NOT NULL,
  symbol             TEXT NOT NULL,
  side               TEXT NOT NULL CHECK(side IN ('buy', 'sell', 'invalid')),
  quantity           REAL NOT NULL CHECK(quantity >= 0),
  price              REAL CHECK(price IS NULL OR price > 0),
  leverage           REAL CHECK(leverage IS NULL OR leverage > 0),
  order_id           TEXT,
  client_order_id    TEXT,
  market_type        TEXT CHECK(market_type IS NULL OR market_type IN ('spot', 'swap')),
  requested_quantity REAL CHECK(requested_quantity IS NULL OR requested_quantity >= 0),
  filled_quantity    REAL CHECK(filled_quantity IS NULL OR filled_quantity >= 0),
  average_price      REAL CHECK(average_price IS NULL OR average_price > 0),
  cost               REAL CHECK(cost IS NULL OR cost >= 0),
  fee_json           TEXT,
  exchange_status    TEXT,
  price_source       TEXT,
  price_timestamp_utc TEXT,
  price_deviation_pct REAL CHECK(price_deviation_pct IS NULL OR
                                  (price_deviation_pct >= 0 AND price_deviation_pct <= 1)),
  entry_order_raw_json TEXT,
  protective_orders_json TEXT,
  submitted_at_utc   TEXT,
  reconciled_at_utc  TEXT,
  status             TEXT NOT NULL CHECK(status IN (
    'claimed','submitting','submitted','open','closed','canceled','cancelled',
    'expired','rejected','execution_not_authorized','validation_rejected',
    'submission_state_conflict','entry_rejected','unknown_requires_reconciliation',
    'entry_submitted_protected','entry_submitted_unprotected',
    'protection_unknown_manual_review','protection_failed_position_closed',
    'unprotected_position','position_closed_by_immediate_protection',
    'abandoned_before_submission','reconciliation_not_found_manual_review',
    'reconciled_entry_found','reconciled_entry_found_protection_review')),
  error              TEXT,
  created_at_utc     TEXT NOT NULL,
  updated_at_utc     TEXT NOT NULL,
  UNIQUE(chat_id, message_id),
  FOREIGN KEY (chat_id, message_id)
    REFERENCES messages(chat_id, message_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS position_events (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id            INTEGER NOT NULL,
  message_id         INTEGER NOT NULL,
  status             TEXT NOT NULL CHECK(status IN (
    'claimed','submitting','submitted','open','closed','canceled','cancelled',
    'expired','rejected','execution_not_authorized','validation_rejected',
    'submission_state_conflict','entry_rejected','unknown_requires_reconciliation',
    'entry_submitted_protected','entry_submitted_unprotected',
    'protection_unknown_manual_review','protection_failed_position_closed',
    'unprotected_position','position_closed_by_immediate_protection',
    'abandoned_before_submission','reconciliation_not_found_manual_review',
    'reconciled_entry_found','reconciled_entry_found_protection_review')),
  detail_json        TEXT,
  created_at_utc     TEXT NOT NULL,
  FOREIGN KEY (chat_id, message_id)
    REFERENCES positions_submitted(chat_id, message_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS protective_orders (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id            INTEGER NOT NULL,
  message_id         INTEGER NOT NULL,
  role               TEXT NOT NULL CHECK(role IN ('stop_loss', 'take_profit')),
  order_index        INTEGER NOT NULL CHECK(order_index >= 0),
  trigger_price      REAL NOT NULL CHECK(trigger_price > 0),
  requested_quantity REAL NOT NULL CHECK(requested_quantity > 0),
  client_order_id    TEXT NOT NULL,
  order_id           TEXT,
  exchange_status    TEXT,
  status             TEXT NOT NULL CHECK(status IN (
    'pending','open','closed','canceled','cancelled','expired','rejected',
    'partially_filled','unknown','cancel_failed_manual_review')),
  error              TEXT,
  raw_json           TEXT,
  created_at_utc     TEXT NOT NULL,
  updated_at_utc     TEXT NOT NULL,
  UNIQUE(chat_id, message_id, role, order_index),
  FOREIGN KEY (chat_id, message_id)
    REFERENCES positions_submitted(chat_id, message_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS message_processing (
  chat_id            INTEGER NOT NULL,
  message_id         INTEGER NOT NULL,
  status             TEXT NOT NULL,
  reason             TEXT,
  updated_at_utc     TEXT NOT NULL,
  PRIMARY KEY (chat_id, message_id),
  FOREIGN KEY (chat_id, message_id)
    REFERENCES messages(chat_id, message_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS message_processing_events (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id            INTEGER NOT NULL,
  message_id         INTEGER NOT NULL,
  status             TEXT NOT NULL,
  reason             TEXT,
  created_at_utc     TEXT NOT NULL,
  FOREIGN KEY (chat_id, message_id)
    REFERENCES messages(chat_id, message_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS message_revisions (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id            INTEGER NOT NULL,
  message_id         INTEGER NOT NULL,
  date_utc           TEXT NOT NULL,
  edit_date_utc      TEXT,
  text               TEXT,
  raw_json           TEXT NOT NULL,
  archived_at_utc    TEXT NOT NULL,
  FOREIGN KEY (chat_id, message_id)
    REFERENCES messages(chat_id, message_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS signal_extraction_jobs (
  chat_id            INTEGER NOT NULL,
  message_id         INTEGER NOT NULL,
  status             TEXT NOT NULL CHECK(status IN (
    'pending','processing','retrying','failed','no_signal','rejected',
    'completed','historical_skipped','disabled')),
  attempts           INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
  max_attempts       INTEGER NOT NULL CHECK(max_attempts > 0),
  next_attempt_at_utc TEXT,
  last_error_code    TEXT,
  last_error         TEXT,
  model_result_json  TEXT,
  validation_json    TEXT,
  input_text         TEXT,
  image_paths_json   TEXT,
  historical         INTEGER NOT NULL DEFAULT 0 CHECK(historical IN (0, 1)),
  allow_execution    INTEGER NOT NULL DEFAULT 0 CHECK(allow_execution IN (0, 1)),
  created_at_utc     TEXT NOT NULL,
  updated_at_utc     TEXT NOT NULL,
  CHECK(attempts <= max_attempts),
  PRIMARY KEY (chat_id, message_id),
  FOREIGN KEY (chat_id, message_id)
    REFERENCES messages(chat_id, message_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS signal_extraction_events (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id            INTEGER NOT NULL,
  message_id         INTEGER NOT NULL,
  status             TEXT NOT NULL,
  error_code         TEXT,
  detail             TEXT,
  created_at_utc     TEXT NOT NULL,
  FOREIGN KEY (chat_id, message_id)
    REFERENCES messages(chat_id, message_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS channel_checkpoints (
  chat_id             INTEGER PRIMARY KEY,
  last_message_id     INTEGER NOT NULL DEFAULT 0 CHECK(last_message_id >= 0),
  status              TEXT NOT NULL CHECK(status IN
    ('ready', 'migrated', 'catching_up', 'retry_exhausted')),
  last_error          TEXT,
  updated_at_utc      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_chat_date ON messages(chat_id, date_utc);
CREATE INDEX IF NOT EXISTS idx_processing_events_message
  ON message_processing_events(chat_id, message_id, created_at_utc);
CREATE INDEX IF NOT EXISTS idx_extraction_jobs_due
  ON signal_extraction_jobs(status, next_attempt_at_utc);
"""


POSITION_COLUMNS = {
    "client_order_id": "TEXT",
    "market_type": "TEXT",
    "requested_quantity": "REAL",
    "filled_quantity": "REAL",
    "average_price": "REAL",
    "cost": "REAL",
    "fee_json": "TEXT",
    "exchange_status": "TEXT",
    "price_source": "TEXT",
    "price_timestamp_utc": "TEXT",
    "price_deviation_pct": "REAL",
    "entry_order_raw_json": "TEXT",
    "protective_orders_json": "TEXT",
    "submitted_at_utc": "TEXT",
    "reconciled_at_utc": "TEXT",
}

EXTRACTION_JOB_COLUMNS = {
    "input_text": "TEXT",
    "image_paths_json": "TEXT",
}

MESSAGE_COLUMNS = {
    "persistence_status": "TEXT NOT NULL DEFAULT 'complete'",
    "persistence_error": "TEXT",
    "persistence_updated_at_utc": "TEXT",
}

POSITION_STATES = (
    "claimed", "submitting", "submitted", "open", "closed", "canceled",
    "cancelled", "expired", "rejected", "execution_not_authorized",
    "validation_rejected", "submission_state_conflict", "entry_rejected",
    "unknown_requires_reconciliation", "entry_submitted_protected",
    "entry_submitted_unprotected", "protection_unknown_manual_review",
    "protection_failed_position_closed", "unprotected_position",
    "position_closed_by_immediate_protection", "abandoned_before_submission",
    "reconciliation_not_found_manual_review", "reconciled_entry_found",
    "reconciled_entry_found_protection_review",
)

EXTRACTION_STATES = (
    "pending", "processing", "retrying", "failed", "no_signal", "rejected",
    "completed", "historical_skipped", "disabled",
)

PROTECTIVE_ORDER_STATES = (
    "pending", "open", "closed", "canceled", "cancelled", "expired",
    "rejected", "partially_filled", "unknown", "cancel_failed_manual_review",
)


class RuntimeConnection(sqlite3.Connection):
    """Connection metadata used to serialize access through the worker pool."""

    worker_pool: BlockingWorkPool | None = None
    async_lock = None


def connect_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path,
        isolation_level=None,
        timeout=0.0,
        check_same_thread=False,
        factory=RuntimeConnection,
    )
    conn.execute("PRAGMA foreign_keys=ON;")
    # Waiting is implemented below with bounded retries in a worker. SQLite must
    # not perform its own opaque wait on an asyncio thread.
    conn.execute("PRAGMA busy_timeout=0;")
    return conn


def attach_runtime(conn: sqlite3.Connection, pool: BlockingWorkPool) -> None:
    if isinstance(conn, RuntimeConnection):
        import asyncio

        conn.worker_pool = pool
        conn.async_lock = asyncio.Lock()


def close_db(conn: sqlite3.Connection) -> None:
    """Checkpoint committed WAL data and close the connection on every shutdown."""
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
    finally:
        conn.close()


def _execute_schema(conn: sqlite3.Connection) -> None:
    for stmt in filter(None, (s.strip() for s in DDL_MESSAGES.split(";"))):
        conn.execute(stmt)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _has_message_foreign_key(conn: sqlite3.Connection, table: str) -> bool:
    rows = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    grouped: dict[int, list[tuple[str, str]]] = {}
    targets: dict[int, str] = {}
    for row in rows:
        targets[int(row[0])] = str(row[2])
        grouped.setdefault(int(row[0]), []).append((str(row[3]), str(row[4])))
    return any(
        targets[key] == "messages"
        and set(columns) == {("chat_id", "chat_id"), ("message_id", "message_id")}
        for key, columns in grouped.items()
    )


def _migration_additive_columns(conn: sqlite3.Connection) -> None:
    existing = {
        row[1] for row in conn.execute("PRAGMA table_info(positions_submitted)").fetchall()
    }
    for column, column_type in POSITION_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE positions_submitted ADD COLUMN {column} {column_type}")
    extraction_existing = {
        row[1] for row in conn.execute("PRAGMA table_info(signal_extraction_jobs)").fetchall()
    }
    for column, column_type in EXTRACTION_JOB_COLUMNS.items():
        if column not in extraction_existing:
            conn.execute(f"ALTER TABLE signal_extraction_jobs ADD COLUMN {column} {column_type}")
    message_existing = _columns(conn, "messages")
    for column, column_type in MESSAGE_COLUMNS.items():
        if column not in message_existing:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {column} {column_type}")


def _rebuild_positions_with_message_fk(conn: sqlite3.Connection) -> None:
    if _has_message_foreign_key(conn, "positions_submitted"):
        return
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT OR IGNORE INTO messages
          (chat_id, message_id, date_utc, text, raw_json,
           persistence_status, persistence_error, persistence_updated_at_utc)
        SELECT DISTINCT chat_id, message_id, ?,
          '[migration placeholder for retained position audit]', '{}',
          'repair_required', 'source message was absent during schema migration', ?
        FROM positions_submitted
        """,
        (now, now),
    )
    existing = _columns(conn, "positions_submitted")
    conn.execute("ALTER TABLE positions_submitted RENAME TO positions_submitted_legacy")
    allowed = ", ".join(repr(value) for value in POSITION_STATES)
    conn.execute(
        f"""
        CREATE TABLE positions_submitted (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          chat_id INTEGER NOT NULL,
          message_id INTEGER NOT NULL,
          symbol TEXT NOT NULL,
          side TEXT NOT NULL CHECK(side IN ('buy', 'sell', 'invalid')),
          quantity REAL NOT NULL CHECK(quantity >= 0),
          price REAL CHECK(price IS NULL OR price > 0),
          leverage REAL CHECK(leverage IS NULL OR leverage > 0),
          order_id TEXT,
          client_order_id TEXT,
          market_type TEXT CHECK(market_type IS NULL OR market_type IN ('spot', 'swap')),
          requested_quantity REAL CHECK(requested_quantity IS NULL OR requested_quantity >= 0),
          filled_quantity REAL CHECK(filled_quantity IS NULL OR filled_quantity >= 0),
          average_price REAL CHECK(average_price IS NULL OR average_price > 0),
          cost REAL CHECK(cost IS NULL OR cost >= 0),
          fee_json TEXT,
          exchange_status TEXT,
          price_source TEXT,
          price_timestamp_utc TEXT,
          price_deviation_pct REAL CHECK(price_deviation_pct IS NULL OR
            (price_deviation_pct >= 0 AND price_deviation_pct <= 1)),
          entry_order_raw_json TEXT,
          protective_orders_json TEXT,
          submitted_at_utc TEXT,
          reconciled_at_utc TEXT,
          status TEXT NOT NULL CHECK(status IN ({allowed})),
          error TEXT,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL,
          UNIQUE(chat_id, message_id),
          FOREIGN KEY(chat_id, message_id)
            REFERENCES messages(chat_id, message_id) ON DELETE RESTRICT
        )
        """
    )
    columns = (
        "id", "chat_id", "message_id", "symbol", "side", "quantity", "price",
        "leverage", "order_id", "client_order_id", "market_type",
        "requested_quantity", "filled_quantity", "average_price", "cost",
        "fee_json", "exchange_status", "price_source", "price_timestamp_utc",
        "price_deviation_pct", "entry_order_raw_json", "protective_orders_json",
        "submitted_at_utc", "reconciled_at_utc", "status", "error",
        "created_at_utc", "updated_at_utc",
    )
    defaults = {
        "client_order_id": "NULL", "market_type": "NULL",
        "requested_quantity": "quantity", "filled_quantity": "NULL",
        "average_price": "NULL", "cost": "NULL", "fee_json": "NULL",
        "exchange_status": "NULL", "price_source": "NULL",
        "price_timestamp_utc": "NULL", "price_deviation_pct": "NULL",
        "entry_order_raw_json": "NULL", "protective_orders_json": "NULL",
        "submitted_at_utc": "NULL", "reconciled_at_utc": "NULL",
    }
    expressions = []
    for column in columns:
        expression = column if column in existing else defaults.get(column, "NULL")
        if column == "side":
            expression = "CASE WHEN side IN ('buy','sell','invalid') THEN side ELSE 'invalid' END"
        elif column == "quantity":
            expression = "CASE WHEN quantity >= 0 THEN quantity ELSE 0 END"
        elif column in {"price", "leverage", "average_price"}:
            expression = f"CASE WHEN {expression} > 0 THEN {expression} ELSE NULL END"
        elif column in {"requested_quantity", "filled_quantity", "cost"}:
            expression = f"CASE WHEN {expression} >= 0 THEN {expression} ELSE NULL END"
        elif column == "market_type":
            expression = "CASE WHEN market_type IN ('spot','swap') THEN market_type ELSE NULL END" if column in existing else "NULL"
        elif column == "price_deviation_pct":
            expression = "CASE WHEN price_deviation_pct BETWEEN 0 AND 1 THEN price_deviation_pct ELSE NULL END" if column in existing else "NULL"
        elif column == "status":
            expression = f"CASE WHEN status IN ({allowed}) THEN status ELSE 'reconciliation_not_found_manual_review' END"
        expressions.append(expression)
    conn.execute(
        f"INSERT INTO positions_submitted ({', '.join(columns)}) "
        f"SELECT {', '.join(expressions)} FROM positions_submitted_legacy"
    )
    conn.execute("DROP TABLE positions_submitted_legacy")


def _guard_triggers(
    conn: sqlite3.Connection,
    table: str,
    invalid_condition: str,
    message: str,
) -> None:
    for operation in ("INSERT", "UPDATE"):
        name = f"validate_{table}_{operation.lower()}"
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.execute(
            f"""
            CREATE TRIGGER {name} BEFORE {operation} ON {table}
            WHEN {invalid_condition}
            BEGIN
              SELECT RAISE(ABORT, '{message}');
            END
            """
        )


def _migration_integrity_constraints(conn: sqlite3.Connection) -> None:
    _rebuild_positions_with_message_fk(conn)
    position_states = ", ".join(repr(value) for value in POSITION_STATES)
    extraction_states = ", ".join(repr(value) for value in EXTRACTION_STATES)
    _guard_triggers(
        conn, "messages",
        "NEW.persistence_status NOT IN ('pending_media','complete','rejected','repair_required') "
        "OR NEW.views < 0 OR NEW.forwards < 0 OR NEW.replies_count < 0",
        "invalid message persistence state or counters",
    )
    _guard_triggers(conn, "media_files", "NEW.file_size < 0", "invalid media size")
    _guard_triggers(
        conn, "trade_signals",
        "NEW.position_type NOT IN ('long','short') OR NEW.entry_price <= 0 OR NEW.leverage <= 0",
        "invalid trade signal values",
    )
    _guard_triggers(
        conn, "positions_submitted",
        f"NEW.status NOT IN ({position_states}) OR NEW.side NOT IN ('buy','sell','invalid') "
        "OR NEW.quantity < 0 OR (NEW.quantity = 0 AND NEW.status NOT IN "
        "('claimed','validation_rejected','execution_not_authorized',"
        "'submission_state_conflict','abandoned_before_submission',"
        "'reconciliation_not_found_manual_review')) "
        "OR NEW.price <= 0 OR NEW.leverage <= 0 "
        "OR NEW.requested_quantity < 0 OR NEW.filled_quantity < 0 OR NEW.average_price <= 0 "
        "OR NEW.cost < 0 OR NEW.price_deviation_pct < 0 OR NEW.price_deviation_pct > 1 "
        "OR NEW.market_type NOT IN ('spot','swap')",
        "invalid position lifecycle or numeric values",
    )
    _guard_triggers(
        conn, "protective_orders",
        "NEW.role NOT IN ('stop_loss','take_profit') OR NEW.order_index < 0 "
        "OR NEW.trigger_price <= 0 OR NEW.requested_quantity <= 0 OR NEW.status NOT IN ("
        + ",".join(repr(value) for value in PROTECTIVE_ORDER_STATES)
        + ") OR NOT EXISTS (SELECT 1 FROM positions_submitted p "
        "WHERE p.chat_id=NEW.chat_id AND p.message_id=NEW.message_id)",
        "invalid protective order values",
    )
    _guard_triggers(
        conn, "position_events",
        f"NEW.status NOT IN ({position_states}) OR NOT EXISTS "
        "(SELECT 1 FROM positions_submitted p WHERE p.chat_id=NEW.chat_id "
        "AND p.message_id=NEW.message_id)",
        "invalid position event lifecycle or relationship",
    )
    _guard_triggers(
        conn, "signal_extraction_jobs",
        f"NEW.status NOT IN ({extraction_states}) OR NEW.attempts < 0 "
        "OR NEW.max_attempts <= 0 OR NEW.attempts > NEW.max_attempts "
        "OR NEW.historical NOT IN (0,1) OR NEW.allow_execution NOT IN (0,1)",
        "invalid extraction job lifecycle",
    )
    _guard_triggers(
        conn, "channel_checkpoints",
        "NEW.last_message_id < 0 OR NEW.status NOT IN "
        "('ready','migrated','catching_up','retry_exhausted')",
        "invalid channel checkpoint",
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_client_order_id
        ON positions_submitted(client_order_id) WHERE client_order_id IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_positions_reconcile
        ON positions_submitted(status, updated_at_utc)
        """
    )


MIGRATIONS = (
    (1, "create_core_schema", _execute_schema),
    (2, "add_runtime_columns", _migration_additive_columns),
    (3, "add_integrity_constraints", _migration_integrity_constraints),
)


def init_db(conn: sqlite3.Connection) -> None:
    """Apply repeatable, transactional schema migrations in version order."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=0")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA legacy_alter_table=ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version INTEGER PRIMARY KEY,
          name TEXT NOT NULL UNIQUE,
          applied_at_utc TEXT NOT NULL
        )
        """
    )
    try:
        applied_rows = conn.execute(
            "SELECT version,name FROM schema_migrations"
        ).fetchall()
        applied_names = {int(row[0]): str(row[1]) for row in applied_rows}
        expected_names = {version: name for version, name, _ in MIGRATIONS}
        unknown = sorted(version for version in applied_names if version not in expected_names)
        if unknown:
            raise RuntimeError(
                "database schema is newer than this application; unknown migration(s): "
                + ", ".join(str(value) for value in unknown)
            )
        mismatched = sorted(
            version for version, name in applied_names.items()
            if expected_names.get(version) != name
        )
        if mismatched:
            raise RuntimeError(
                "database migration history does not match this application at version(s): "
                + ", ".join(str(value) for value in mismatched)
            )
        applied = set(applied_names)
        for version, name, migration in MIGRATIONS:
            if version in applied:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                migration(conn)
                conn.execute(
                    "INSERT INTO schema_migrations(version,name,applied_at_utc) VALUES(?,?,?)",
                    (version, name, datetime.now(timezone.utc).isoformat()),
                )
                conn.execute(f"PRAGMA user_version={version}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise sqlite3.IntegrityError(
            f"database migration left {len(violations)} foreign-key violation(s)"
        )
    integrity_result = conn.execute("PRAGMA integrity_check").fetchone()
    if not integrity_result or str(integrity_result[0]).lower() != "ok":
        raise sqlite3.DatabaseError(
            "database integrity check failed after migration; restore the pre-migration backup"
        )


def sql_execute_with_retry(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple = (),
    busy_retries: int = 10,
    busy_sleep_secs: float = 0.2,
) -> None:
    if busy_retries < 0 or busy_sleep_secs < 0:
        raise ValueError("SQLite retry count and delay must be non-negative")
    attempts = 0
    while True:
        try:
            conn.execute(sql, params)
            return
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if (
                ("database is locked" in msg or "database table is locked" in msg)
                and attempts < busy_retries
            ):
                try:
                    import asyncio

                    asyncio.get_running_loop()
                except RuntimeError:
                    pass
                else:
                    raise RuntimeError(
                        "SQLite lock retry reached the asyncio thread; use run_db()"
                    ) from e
                attempts += 1
                # This function is synchronous by design and only retries in a
                # worker thread. Callers on asyncio threads fail visibly above.
                time.sleep(busy_sleep_secs * (2 ** min(attempts - 1, 5)))
                continue
            raise
