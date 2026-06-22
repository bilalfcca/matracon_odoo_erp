"""Per-vendor quotation terms for Comparative Statement (native alternatives)."""

from odoo import models, fields


class PurchaseOrderQuoteTerms(models.Model):
    _inherit = 'purchase.order'

    x_quote_brand = fields.Char(
        string='Brand / Make',
        help='Manufacturer or brand offered by this vendor (if applicable).')
    x_quote_delivery_basis = fields.Char(
        string='Delivery Basis', help='e.g. Ex-Works, FOR Site, DDP')
    x_quote_delivery_period = fields.Char(string='Delivery / Lead Time')
    x_quote_payment_terms = fields.Char(string='Payment Terms')
    x_quote_warranty = fields.Char(string='Warranty / Defect Liability')
    x_quote_tax_treatment = fields.Char(
        string='GST / Tax Treatment', help='e.g. 18% extra, inclusive, exempt')
    x_quote_validity = fields.Date(string='Quote Valid Until')
    x_quote_additional_terms = fields.Html(
        string='Additional Terms & Conditions',
        sanitize_attributes=False,
        help='Free-form vendor terms for any product or scope.',
    )
