from odoo import models, api

class PartnerLedgerReportHandler(models.AbstractModel):
    _inherit = 'account.partner.ledger.report.handler'

    def _get_report_line_partners(self, options, partners):
        # Get the base lines from parent method
        lines = super()._get_report_line_partners(options, partners)

        # For each line, find associated tax lines
        for line in lines:
            # Get the account.move.line from the line data
            move_line = self.env['account.move.line'].browse(line.get('id'))

            if move_line:
                # Get tax amount from the line (tax_line_id means it's a tax line)
                if move_line.tax_line_id:
                    # This is a tax line - get the tax amount
                    line['tax_amount'] = move_line.balance
                    line['base_amount'] = move_line.tax_base_amount
                    line['is_tax_line'] = True
                else:
                    # This is a base line - find its tax lines
                    tax_lines = move_line.tax_line_ids.filtered(
                        lambda t: t.move_id == move_line.move_id
                    )
                    line['tax_amount'] = sum(tax_lines.mapped('balance')) if tax_lines else 0.0
                    line['base_amount'] = move_line.balance
                    line['is_tax_line'] = False

        return lines