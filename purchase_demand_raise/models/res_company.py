from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    # ── PO Officer (Procurement HO signatory) ─────────────────────────────
    x_po_officer_name = fields.Char(
        string='PO Officer Name',
        default='Nasir Swati',
        help='Name printed below the PO Officer signature on Purchase Order PDFs.',
    )
    x_po_officer_title = fields.Char(
        string='PO Officer Title',
        default='Procurement Officer',
    )
    x_po_officer_signature = fields.Binary(
        string='PO Officer Signature',
        attachment=True,
        help='Upload the digitized signature image (PNG/JPG, transparent background recommended).',
    )

    # ── CEO ────────────────────────────────────────────────────────────────
    x_ceo_name = fields.Char(
        string='CEO Name',
        default='Jehanzeb Saulat',
        help='Name printed below the CEO signature on Purchase Order PDFs.',
    )
    x_ceo_title = fields.Char(
        string='CEO Title',
        default='Chief Executive Officer',
    )
    x_ceo_signature = fields.Binary(
        string='CEO Signature',
        attachment=True,
        help='Upload the digitized signature image (PNG/JPG, transparent background recommended).',
    )
