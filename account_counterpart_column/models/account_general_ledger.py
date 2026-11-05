import json

from odoo import models


class AccountGeneralLedgerReportHandler(models.AbstractModel):
    _inherit = 'account.general.ledger.report.handler'

    def _report_custom_engine_general_ledger(self, expressions, options, date_scope, current_groupby, next_groupby, offset=0, limit=None, warnings=None):
        """Extend the General Ledger custom engine to also return a
        'counterpart_account' value per row, consumed by the
        'counterpart_account' column/expression declared in
        data/general_ledger_counterpart_column.xml.

        The base method already builds one row per journal item (grouped by
        'id_with_accumulated_balance') and already joins account_move, but
        does not expose the other line(s) of the move. Rather than touching
        the base _get_query() SQL (large and easy to break), we call
        super() first and then enrich the resulting rows with one extra
        batched query via account.move.line._get_counterpart_account_display,
        keyed off the account.move.line ids already present in the result.
        This covers both the "unfold all" and the per-click "expand" render
        paths, since both funnel through this same method.
        """
        result = super()._report_custom_engine_general_ledger(
            expressions, options, date_scope, current_groupby, next_groupby,
            offset=offset, limit=limit, warnings=warnings,
        )

        # Note: use None (not False) for "no counterpart" - the report's
        # _format_value() renders None as a blank cell but would otherwise
        # str()-format a falsy value like False into the literal text "False"
        # for a 'string' figure_type column.
        if not current_groupby:
            # Total line: aggregates every account, no single counterpart applies.
            result['counterpart_account'] = None
            return result

        if current_groupby != 'id_with_accumulated_balance':
            # Account-level subtotal rows aggregate many unrelated journal
            # entries; a single "counterpart account" would be meaningless.
            for _key, entry in result:
                entry['counterpart_account'] = None
            return result

        # At this groupby level, each key is either json.dumps([date, aml_id])
        # for a real journal item, or "balance_line_<account_id>" for the
        # synthetic opening-balance row (see _report_custom_engine_general_ledger
        # in the base handler) - the latter has no single move to look up.
        aml_id_by_key = {}
        for key, _entry in result:
            if not key or key.startswith('balance_line_'):
                continue
            try:
                _date_str, aml_id = json.loads(key)
            except (ValueError, TypeError):
                continue
            if aml_id:
                aml_id_by_key[key] = aml_id

        counterpart_by_aml_id = self.env['account.move.line']._get_counterpart_account_display(
            list(aml_id_by_key.values())
        )

        for key, entry in result:
            aml_id = aml_id_by_key.get(key)
            entry['counterpart_account'] = counterpart_by_aml_id.get(aml_id) if aml_id else None

        return result
