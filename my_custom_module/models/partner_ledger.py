from collections import defaultdict

from odoo import models


class PartnerLedgerReportHandler(models.AbstractModel):
    _inherit = 'account.partner.ledger.report.handler'

    def _get_tax_amounts_by_move(self, moves):
        """Return total tax amount grouped by account.move (all tax types)."""
        result = defaultdict(lambda: {'taxes_amount': 0.0})
        for move in moves:
            total = 0.0
            for line in move.line_ids:
                if line.tax_line_id:
                    total += abs(line.balance)
            result[move.id] = {'taxes_amount': total}
        return result

    def _inject_tax_columns(self, aml_results):
        """Add combined taxes_amount to partner ledger AML rows."""
        if not aml_results:
            return

        if isinstance(aml_results, dict):
            items = [
                (aml_id, values)
                for aml_id, values in aml_results.items()
                if isinstance(aml_id, int) and isinstance(values, dict)
            ]
        elif isinstance(aml_results, list):
            items = [
                (values.get('id'), values)
                for values in aml_results
                if isinstance(values, dict) and values.get('id')
            ]
        else:
            return

        if not items:
            return

        move_lines = self.env['account.move.line'].browse([aml_id for aml_id, _ in items])
        tax_by_move = self._get_tax_amounts_by_move(move_lines.move_id)

        for aml_id, values in items:
            move_line = move_lines.browse(aml_id)
            if not move_line.exists():
                continue

            if move_line.tax_line_id:
                values['taxes_amount'] = abs(move_line.balance)
                continue

            taxes = tax_by_move.get(move_line.move_id.id, {})
            values['taxes_amount'] = taxes.get('taxes_amount', 0.0)

    def _get_aml_values(self, options, partner_ids, offset=0, limit=None):
        rslt = super()._get_aml_values(options, partner_ids, offset=offset, limit=limit)
        if isinstance(rslt, dict):
            for partner_id in partner_ids:
                if partner_id in rslt:
                    self._inject_tax_columns(rslt[partner_id])
        return rslt

    def _custom_unfold_all_batch_data_generator(self, report, options, lines_to_expand_by_function):
        batch_data = super()._custom_unfold_all_batch_data_generator(
            report, options, lines_to_expand_by_function,
        )
        if isinstance(batch_data, dict):
            aml_values = batch_data.get('aml_values')
            if isinstance(aml_values, dict):
                for partner_aml_results in aml_values.values():
                    self._inject_tax_columns(partner_aml_results)
        return batch_data
