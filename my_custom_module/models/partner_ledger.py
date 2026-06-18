from odoo import models, api

class PartnerLedgerReportHandler(models.AbstractModel):
    _inherit = 'account.partner.ledger.report.handler'

    def _get_report_line_partners(self, options, partners):
        # Get the base lines from parent
        lines = super()._get_report_line_partners(options, partners)

        # Add tax amount to each line
        for line in lines:
            # Get the account move line
            line_id = line.get('id')
            if line_id:
                move_line = self.env['account.move.line'].browse(line_id)
                if move_line.exists():
                    # Get tax amount
                    if move_line.tax_line_id:
                        # This is a tax line itself
                        line['tax_amount'] = abs(move_line.balance)
                    else:
                        # Check if this move has tax lines
                        tax_amount = 0.0
                        for line2 in move_line.move_id.line_ids:
                            if line2.tax_line_id and line2.account_id == move_line.account_id:
                                tax_amount += abs(line2.balance)
                        line['tax_amount'] = tax_amount

        return lines