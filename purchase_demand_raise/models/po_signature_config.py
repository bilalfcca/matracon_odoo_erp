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

    # ── PO Officer ────────────────────────────────────────────────────────
    x_po_officer_name = fields.Char(
        string='PO Officer Name',
        default='Nasir Swati',
    )
    x_po_officer_title = fields.Char(
        string='Title / Designation',
        default='Procurement Officer',
    )
    x_po_officer_signature = fields.Binary(
        string='Signature Image (optional)',
        attachment=True,
        help='If uploaded, the image is used on the PO PDF. Otherwise the name is shown in signature font.',
    )

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
