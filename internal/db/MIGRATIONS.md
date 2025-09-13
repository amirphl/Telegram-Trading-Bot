# SQLite migration and rollback procedure

`init_db` applies numbered migrations in one transaction per version and records
them in `schema_migrations`. It also updates `PRAGMA user_version`. Re-running the
same application version is safe because recorded migrations are skipped.

Before deploying a version that contains a new migration:

1. Stop the bot cleanly so its WAL is checkpointed and all worker threads exit.
2. Create a consistent backup with SQLite's backup command:

   ```sh
   sqlite3 tg_channel.db ".backup 'tg_channel.before-migration.db'"
   ```

3. Start the new version and verify `PRAGMA foreign_key_check`,
   `PRAGMA integrity_check`, and the rows in `schema_migrations`.

Migrations never log or copy API credentials. Migration 3 retains a position whose
source message is missing by creating an explicit `repair_required` placeholder
message instead of dropping the financial audit record.

To roll back after a failed deployment, stop the bot, preserve the failed database
for diagnosis, and restore the backup as a whole database file. Do not delete rows
from `schema_migrations` to simulate a rollback: schema changes and data transforms
must be reversed together.
