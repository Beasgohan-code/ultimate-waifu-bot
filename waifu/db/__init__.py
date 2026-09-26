"""The database package — one database, one class.

``from waifu.db import Database`` is the whole public story: two methods to
access the data (``tx`` for read/write, ``query`` for read-only) plus
``backup`` / ``restore`` so the entire database can be saved to — and
replaced from — one file. Everything else in this package is implementation
detail:

* ``models``       — the tables (the shape of the data)
* ``migrations``   — ordered schema upgrades (re-run safe, recorded)
* ``repo``         — the SQL per domain (users, economy, gacha, …) — one file,
  namespace classes per domain: ``from waifu.db.repo import users as user_repo``
* ``state``        — hot, non-source-of-truth state (the catalogue cache and
  the optional Redis: cooldowns, queues, leaderboards)
* ``seed``         — the tier ladders + the optional starter roster
"""

from waifu.db.database import BACKUP_MAGIC, BackupError, Database, prune_backups

__all__ = ["BACKUP_MAGIC", "BackupError", "Database", "prune_backups"]
