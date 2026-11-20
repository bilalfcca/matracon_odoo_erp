from odoo import api, fields, models


class PartnerWhtExemption(models.Model):
    """WHT Exemption Certificates linked to a vendor/partner.

    Each record holds one certificate that grants a reduced WHT rate for a
    specific tax year / date range.  The batch payment dialog reads these to
    auto-populate the WHT deduction line when a vendor with an active exemption
    is selected.
    """

    _name = 'x.partner.wht.exemption'
    _description = 'Partner WHT Exemption Certificate'
    _order = 'period_to desc, id desc'

    partner_id = fields.Many2one(
        'res.partner', string='Vendor',
        required=True, ondelete='cascade', index=True,
    )

    # ── Certificate details ───────────────────────────────────────────────────

    tax_year = fields.Char(
        string='Tax Year',
        help='e.g. "2024-25"',
    )
    period_from = fields.Date(
        string='Period From',
        help='Start date of the exemption validity.  Leave blank for open-ended.',
    )
    period_to = fields.Date(
        string='Period To',
        help='End date of the exemption validity.  Leave blank for open-ended.',
    )
    barcode_number = fields.Char(
        string='Barcode Number',
        help='FBR barcode / PSID printed on the exemption certificate.',
    )
    description = fields.Char(
        string='Description',
        help='Short label shown on the WHT deduction line (e.g. "WHT @ 3.5% — FBR cert 2024").',
    )
    rate = fields.Float(
        string='Rate %',
        digits=(5, 2),
        help='Reduced WHT percentage granted by this certificate.',
    )

    # ── Payment defaults ──────────────────────────────────────────────────────

    x_wht_payable_account_id = fields.Many2one(
        'account.account', string='WHT Payable Account',
        domain="[('account_type', 'like', 'liability')]",
        help='GL account credited when WHT is deducted.  Auto-fills "WHT Payable Account" '
             'in the batch payment dialog when this exemption is selected.',
    )

    # ── Attachments ───────────────────────────────────────────────────────────

    attachment_ids = fields.Many2many(
        'ir.attachment', string='Attachments',
        help='Scanned copy of the FBR exemption certificate.',
    )

    # ── Computed helpers ──────────────────────────────────────────────────────

    is_active_today = fields.Boolean(
        string='Active',
        compute='_compute_is_active_today',
        store=False,
        help='True when today falls within the certificate period (or period is open-ended).',
    )

    @api.depends('period_from', 'period_to')
    def _compute_is_active_today(self):
        today = fields.Date.today()
        for rec in self:
            from_ok = (not rec.period_from) or (rec.period_from <= today)
            to_ok = (not rec.period_to) or (rec.period_to >= today)
            rec.is_active_today = from_ok and to_ok
