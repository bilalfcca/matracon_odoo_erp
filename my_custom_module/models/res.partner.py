from odoo import models

class PartnerLedgerReportHandler(models.AbstractModel):
    _inherit = 'account.partner.ledger.report.handler'

    def _get_report_line_partners(self, options, partners):
        # Get the base data from the parent method
        lines = super()._get_report_line_partners(options, partners)

        # For each line, add tax data from your source
        for line in lines:
            # Replace this with your actual tax-fetching logic
            line['tax_amount'] = self._get_tax_amount_for_line(line, options)

        return lines

    def _get_tax_amount_for_line(self, line, options):
        # Your logic to fetch tax amount for this partner/entries
        # This could come from account.move.line records with tax_ids
        return 0.0  # Placeholder