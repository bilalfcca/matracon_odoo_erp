from odoo import models

class PartnerLedgerReportHandler(models.AbstractModel):
    _inherit = 'account.partner.ledger.report.handler'

    def _get_report_line_partners(self, options, partner, partner_values, **kwargs):
        """Add tax amount to each line"""
        line = super()._get_report_line_partners(options, partner, partner_values, **kwargs)

        # Get tax amount
        tax_amount = 0.0
        move_line_id = line.get('id')
        if isinstance(move_line_id, int):
            move_line = self.env['account.move.line'].browse(move_line_id)
            if move_line.exists():
                if move_line.tax_line_id:
                    tax_amount = abs(move_line.balance)
                else:
                    # Check if this move has tax lines
                    for line2 in move_line.move_id.line_ids:
                        if line2.tax_line_id and line2.account_id == move_line.account_id:
                            tax_amount += abs(line2.balance)

        line['tax_amount'] = tax_amount
        return line

    def _get_dynamic_lines(self, report, options, all_column_groups_expression_totals, warnings=None):
        """Add tax column to the report lines"""
        lines = super()._get_dynamic_lines(report, options, all_column_groups_expression_totals, warnings=warnings)

        # Add tax column to each line
        for line in lines:
            line['columns'].append({
                'name': line.get('tax_amount', 0.0),
                'class': 'text-right',
                'style': 'white-space: nowrap;',
            })
        return lines