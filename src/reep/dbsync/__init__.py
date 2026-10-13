"""Fill Reep IDs and provider IDs into your own database tables.

The database sync reads the tables named in a ``reep.yml`` file, matches each row
exactly on its provider IDs through the local catalogue, and fills empty ID cells.
It needs the ``db`` extra (``pip install 'reep[db]'``) and a database driver
(``reep[postgres]``, ``reep[mysql]`` or ``reep[mssql]``).

Example:
    >>> import reep
    >>> from reep import dbsync
    >>> with reep.Register.local() as reg:  # doctest: +SKIP
    ...     report = dbsync.sync("reep.yml", reg, ["reep"])  # a dry run
    ...     print(report.planned, report.needs_review)
"""

from reep.dbsync._config import (
    Config,
    ProviderColumn,
    TableConfig,
    config_template,
    load_config,
    parse_config,
)
from reep.dbsync._plan import OUTCOMES, REVIEW_OUTCOMES, STATUS_VALUES
from reep.dbsync._run import REVIEW_COLUMNS, SyncReport, TargetReport, review, sync

__all__ = [
    "OUTCOMES",
    "REVIEW_COLUMNS",
    "REVIEW_OUTCOMES",
    "STATUS_VALUES",
    "Config",
    "ProviderColumn",
    "SyncReport",
    "TableConfig",
    "TargetReport",
    "config_template",
    "load_config",
    "parse_config",
    "review",
    "sync",
]
