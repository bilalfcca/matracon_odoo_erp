"""
Migration 1.9.1 -> 1.9.2

Cleans up DB residue left by 'account_counterpart_column', a module that was
built and installed only on the staging_for_test branch (commits c70178c /
780bfa6: "Feat: add Offsetting/Counterpart Account column to General Ledger
+ Journal Items" and the follow-up column-reorder) but was never merged into
Development. When staging_for_test was later reset onto Development, the
module's files disappeared from the codebase while databases that had it
installed (production backups included) kept its data:

  - ir.ui.view  account_counterpart_column.view_move_line_tree_counterpart_account
    An extension of account.view_move_line_tree adding a field
    'x_counterpart_account_display' that no longer exists on
    account.move.line (it was a non-stored compute field defined only in the
    removed module's Python code). This makes the combined account.move.line
    view invalid, which breaks module loading / test runs on every
    subsequent build - the reported failure alternates between whatever
    module happens to be loading when Odoo's view-validation pass runs
    (seen as both "site_operations" and "matracon_fleet" install failures).

  - account.report.expression / account.report.column
    general_ledger_line_counterpart_account / general_ledger_report_counterpart_account
    General Ledger report config pointing at a custom-engine formula
    ('counterpart_account') that was implemented by the removed module's
    account_general_ledger.py override. Dead config now that the code is
    gone; left in place it would error if the General Ledger report is ever
    rendered with default columns.

  - ir_module_module row for 'account_counterpart_column' stuck
    installed/to-upgrade with no matching addon on disk, producing
    "not loaded" / "inconsistent state" errors on every module load.

Uses raw SQL (pre-migrate, no ORM/registry yet) so it runs before Odoo's own
view-validation pass for this upgrade, mirroring the existing 1.9.1
pre-migrate pattern for the same class of problem (stored views referencing
fields removed from code).

NOTE: 'board_resolutions' and 'matracon_admin_tools' show the same kind of
"module missing from disk" warnings in the logs, but they are NOT part of
this codebase's history at all (no trace in git log) - unlike
account_counterpart_column, we have no record of what they contain, so they
are intentionally left untouched here. Clean those up manually via the
Odoo.sh Apps list (or ask for them to be investigated first) if they should
be uninstalled.
"""
import logging

_logger = logging.getLogger(__name__)

_STALE_XMLIDS = [
    ('account_counterpart_column', 'view_move_line_tree_counterpart_account'),
    ('account_counterpart_column', 'general_ledger_line_counterpart_account'),
    ('account_counterpart_column', 'general_ledger_report_counterpart_account'),
]


def migrate(cr, version):
    # 1. Delete the specific orphaned records by their known xmlid, via
    #    ir_model_data so we remove both the data row and its ownership
    #    record cleanly (mirrors what an uninstall would do for this module,
    #    without needing the module's code to be importable).
    for module, name in _STALE_XMLIDS:
        cr.execute(
            """
            SELECT d.id, d.model, d.res_id
              FROM ir_model_data d
             WHERE d.module = %s AND d.name = %s
            """,
            (module, name),
        )
        row = cr.fetchone()
        if not row:
            continue
        data_id, model, res_id = row
        table = model.replace('.', '_')
        cr.execute(f"DELETE FROM {table} WHERE id = %s", (res_id,))
        cr.execute("DELETE FROM ir_model_data WHERE id = %s", (data_id,))
        _logger.warning(
            "site_operations migration 1.9.2: deleted orphaned %s '%s.%s' "
            "(res_id=%s) left over from the removed account_counterpart_column module",
            model, module, name, res_id,
        )

    # 2. Safety net: in case Odoo Studio (rather than the module's own XML)
    #    produced additional views referencing the missing field directly,
    #    catch and delete any remaining account.move.line view mentioning it.
    cr.execute("""
        SELECT id, name FROM ir_ui_view
         WHERE model = 'account.move.line'
           AND arch_db::text ILIKE '%%x_counterpart_account_display%%'
    """)
    rows = cr.fetchall()
    if rows:
        cr.execute(
            "DELETE FROM ir_ui_view WHERE id = ANY(%s)",
            ([r[0] for r in rows],),
        )
        for view_id, name in rows:
            _logger.warning(
                "site_operations migration 1.9.2: deleted additional orphaned "
                "account.move.line view %s (%s) referencing x_counterpart_account_display",
                view_id, name,
            )

    # 3. Neutralize the stale module registration so Odoo stops trying to
    #    include it in the module graph on every load.
    cr.execute(
        "SELECT id, state FROM ir_module_module WHERE name = 'account_counterpart_column'"
    )
    row = cr.fetchone()
    if row and row[1] not in ('uninstalled', 'uninstallable'):
        cr.execute(
            "UPDATE ir_module_module SET state = 'uninstalled' WHERE id = %s",
            (row[0],),
        )
        _logger.warning(
            "site_operations migration 1.9.2: marked stale module "
            "'account_counterpart_column' (was '%s') as uninstalled - "
            "its files no longer exist in the codebase", row[1],
        )
