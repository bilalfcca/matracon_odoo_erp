from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    # ── PO Officer (Procurement HO signatory) ─────────────────────────────
    x_po_officer_name = fields.Char(
        string='PO Officer Name',
        default='Nasir Swati',
        help='Name rendered in signature font on Purchase Order PDFs.',
    )
    x_po_officer_title = fields.Char(
        string='PO Officer Title',
        default='Procurement Officer',
    )

    # ── CEO ────────────────────────────────────────────────────────────────
    x_ceo_name = fields.Char(
        string='CEO Name',
        default='Jehanzeb Saulat',
        help='Name rendered in signature font on Purchase Order PDFs.',
    )
    x_ceo_title = fields.Char(
        string='CEO Title',
        default='Chief Executive Officer',
    )
