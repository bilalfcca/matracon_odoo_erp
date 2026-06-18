from odoo import models

class PartnerLedgerReportHandler(models.AbstractModel):
    _inherit = 'account.partner.ledger.report.handler'

    def _get_report_line_partners(self, options, partner, partner_values, **kwargs):
        # Call the parent method with all arguments
        line = super()._get_report_line_partners(options, partner, partner_values, **kwargs)
        
        # Add tax amount to the line
        if line and 'id' in line:
            move_line = self.env['account.move.line'].browse(line.get('id'))
            if move_line.exists():
                if move_line.tax_line_id:
                    line['tax_amount'] = abs(move_line.balance)
                else:
                    # Find related tax lines for this move
                    tax_amount = 0.0
                    for line2 in move_line.move_id.line_ids:
                        if line2.tax_line_id and line2.account_id == move_line.account_id:
                            tax_amount += abs(line2.balance)
                    line['tax_amount'] = tax_amount
        
        return line

    # Also override the column generation to display the tax column
    def _get_columns_name(self, options):
        columns = super()._get_columns_name(options)
        columns.append({
            'name': 'tax_amount',
            'string': 'Tax',
            'class': 'text-right',
            'style': 'white-space: nowrap;',
        })
        return columns

    def _get_columns_value(self, options, line):
        values = super()._get_columns_value(options, line)
        values.append({
            'name': line.get('tax_amount', 0.0),
            'class': 'text-right',
        })
        return values
