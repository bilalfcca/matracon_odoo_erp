from odoo import api, fields, models


class POSignatureConfig(models.Model):
    _name = 'x.po.signature.config'
    _description = 'PO Signature Configuration'
    _rec_name = 'company_id'

    company_id = fields.Many2one(
        'res.company',
        string='Company',
        required=True,
        ondelete='cascade',
        default=lambda self: self.env.company,
    )

    # Note: a "PO Officer" signature section used to live here too
    # (x_po_officer_name/title/signature) but was never read by any report —
    # removed 2026-09 as dead code. The RFQ report's Procurement Officer
    # signature is sourced from purchase.order.x_rfq_prepared_by_id instead
    # (the actual person who processed/sent that specific RFQ), not a single
    # company-wide configured name.

    # ── CEO ───────────────────────────────────────────────────────────────
    x_ceo_name = fields.Char(
        string='CEO Name',
        default='Jehanzeb Saulat',
    )
    x_ceo_title = fields.Char(
        string='Title / Designation',
        default='Chief Executive Officer',
    )
    x_ceo_signature = fields.Binary(
        string='Signature Image (optional)',
        attachment=True,
        help='If uploaded, the image is used on the PO PDF. Otherwise the name is shown in signature font.',
    )

    @api.model
    def get_or_create_for_company(self):
        """Return the config record for the current company, creating it if needed."""
        config = self.search([('company_id', '=', self.env.company.id)], limit=1)
        if not config:
            config = self.create({'company_id': self.env.company.id})
        return config
