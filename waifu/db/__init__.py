"""The database package — one database, one class.

``from waifu.db import Database`` is the whole public story: two methods to
access the data (``tx`` for read/write, ``query`` for read-only) plus
``backup`` / ``restore`` so the entire database can be saved to — and
replaced from — one file. Everything else in this package is implementation
detail:

* ``models``       — the tables (the shape of the data)
* ``migrations``   — ordered schema upgrades (re-run safe, recorded)
* ``repositories`` — the SQL per domain (users, economy, gacha, …)
* ``cache`` / ``redis_client`` / ``seed`` — hot, non-source-of-truth state
  and the optional starter roster
"""

from waifu.db.database import BACKUP_MAGIC, BackupError, Database, prune_backups

__all__ = ["BACKUP_MAGIC", "BackupError", "Database", "prune_backups"]
