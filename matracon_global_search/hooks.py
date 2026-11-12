"""Speed up "contains" searches on the busiest free-text columns.

Odoo already has trigram indexes on most name fields. These cover the large
tables whose memo / reference columns don't have one. Creation is skipped
quietly when the pg_trgm extension is not available, and every index is
dropped again when the module is uninstalled.
"""

import logging

_logger = logging.getLogger(__name__)

INDEX_PREFIX = 'mtr_gs_trgm_'

# (table, column) — only created when both exist and the column is plain text.
TRIGRAM_COLUMNS = [
    ('account_move_line', 'name'),
    ('account_move', 'ref'),
    ('account_move', 'payment_reference'),
    ('account_payment', 'memo'),
    ('stock_picking', 'origin'),
    ('stock_move', 'reference'),
    ('purchase_order', 'partner_ref'),
    ('purchase_order', 'origin'),
    ('mail_message', 'subject'),
]


def _has_trigram(cr):
    cr.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
    if cr.fetchone():
        return True
    try:
        with cr.savepoint():
            cr.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')
        return True
    except Exception:  # noqa: BLE001 - no permission on this server
        _logger.info('Global search: pg_trgm not available, no extra indexes created.')
        return False


def post_init_hook(env):
    cr = env.cr
    if not _has_trigram(cr):
        return
    for table, column in TRIGRAM_COLUMNS:
        cr.execute("""
            SELECT data_type FROM information_schema.columns
             WHERE table_name = %s AND column_name = %s
        """, [table, column])
        row = cr.fetchone()
        if not row or row[0] not in ('character varying', 'text'):
            continue
        name = f'{INDEX_PREFIX}{table}_{column}'
        try:
            with cr.savepoint():
                cr.execute(
                    f'CREATE INDEX IF NOT EXISTS "{name}" '
                    f'ON "{table}" USING gin ("{column}" gin_trgm_ops)')
        except Exception:  # noqa: BLE001
            _logger.warning('Global search: could not create index %s', name, exc_info=True)


def uninstall_hook(env):
    cr = env.cr
    cr.execute("SELECT indexname FROM pg_indexes WHERE indexname LIKE %s",
               [INDEX_PREFIX + '%'])
    for (name,) in cr.fetchall():
        cr.execute(f'DROP INDEX IF EXISTS "{name}"')
