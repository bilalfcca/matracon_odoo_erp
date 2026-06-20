from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError


class ComparativeStatement(models.Model):
    _name = 'x.comparative.statement'
    _description = 'Comparative Statement'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(string='Reference', required=True, tracking=True)
    x_purchase_order_id = fields.Many2one(
        'purchase.order', string='Purchase Requisition', required=True, tracking=True
    )
    x_state = fields.Selection([
        ('draft', 'Draft'),
        ('confirmed', 'Confirmed'),
    ], default='draft', string='State', tracking=True)

    x_vendor_line_ids = fields.One2many(
        'x.cs.vendor', 'x_cs_id', string='Vendor Comparisons'
    )
    x_recommended_vendor_id = fields.Many2one(
        'res.partner', string='Recommended Vendor', tracking=True,
        help='Only one vendor can be marked as recommended. Set this after reviewing all quotes.'
    )
    x_negotiation_notes = fields.Text(string='Negotiation Notes / Audit Trail')

    # ── Compute best value ────────────────────────────────────────────────
    @api.constrains('x_recommended_vendor_id')
    def _check_single_recommended_vendor(self):
        """Only one vendor can be recommended per CS."""
        pass  # enforced by Many2one (single value by nature)

    def action_confirm(self):
        """Lock CS and route PR to CEO final approval."""
        for cs in self:
            if not cs.x_recommended_vendor_id:
                raise UserError(_('Please select a recommended vendor before confirming.'))
            cs.x_state = 'confirmed'
            if cs.x_purchase_order_id:
                cs.x_purchase_order_id.action_confirm_cs()
            cs.message_post(
                body=_('Comparative Statement confirmed. Recommended vendor: %s.') % cs.x_recommended_vendor_id.name,
                subtype_xmlid='mail.mt_log_note',
            )


class CSVendor(models.Model):
    _name = 'x.cs.vendor'
    _description = 'CS Vendor Comparison'
    _order = 'x_total_price asc'

    x_cs_id = fields.Many2one('x.comparative.statement', string='CS', ondelete='cascade')
    x_partner_id = fields.Many2one('res.partner', string='Vendor', required=True)
    x_rfq_reference = fields.Char(string='Quotation Reference')
    x_quote_validity = fields.Date(string='Quote Validity')
    x_delivery_basis = fields.Selection([
        ('ex_works', 'Ex-Works'),
        ('for', 'FOR'),
        ('ddp', 'DDP'),
    ], string='Delivery Basis')
    x_delivery_period = fields.Char(string='Delivery Period')
    x_payment_terms = fields.Char(string='Payment Terms')
    x_warranty = fields.Char(string='Warranty')
    x_tax_treatment = fields.Char(string='Tax Treatment')
    x_brand_origin = fields.Char(string='Brand / Origin')
    x_ancillary_included = fields.Boolean(string='Ancillary Items Included?')
    x_remarks = fields.Text(string='Remarks')
    x_attachment_ids = fields.Many2many(
        'ir.attachment', string='Vendor Quotation Documents',
        relation='x_cs_vendor_attachment_rel',
        column1='vendor_id', column2='attachment_id',
    )
    x_line_ids = fields.One2many('x.cs.vendor.line', 'x_cs_vendor_id', string='Line Items')
    x_total_price = fields.Float(
        string='Total Price', compute='_compute_totals', store=True
    )
    x_gst_amount = fields.Float(
        string='GST Amount', compute='_compute_totals', store=True
    )
    x_net_total = fields.Float(
        string='Net Total', compute='_compute_totals', store=True
    )
    x_is_lowest = fields.Boolean(
        string='Lowest / Best Value', compute='_compute_is_lowest', store=True
    )
    x_savings_vs_highest = fields.Float(
        string='Saving vs Most Expensive', compute='_compute_savings', store=True
    )

    @api.depends('x_line_ids.x_total_price', 'x_line_ids.x_gst_amount')
    def _compute_totals(self):
        for vendor in self:
            vendor.x_total_price = sum(vendor.x_line_ids.mapped('x_total_price'))
            vendor.x_gst_amount = sum(vendor.x_line_ids.mapped('x_gst_amount'))
            vendor.x_net_total = vendor.x_total_price + vendor.x_gst_amount

    @api.depends('x_cs_id.x_vendor_line_ids.x_total_price')
    def _compute_is_lowest(self):
        for vendor in self:
            others = vendor.x_cs_id.x_vendor_line_ids
            if others:
                min_price = min(others.mapped('x_total_price') or [0])
                vendor.x_is_lowest = (vendor.x_total_price == min_price and vendor.x_total_price > 0)
            else:
                vendor.x_is_lowest = False

    @api.depends('x_cs_id.x_vendor_line_ids.x_total_price', 'x_total_price')
    def _compute_savings(self):
        for vendor in self:
            others = vendor.x_cs_id.x_vendor_line_ids
            if others:
                max_price = max(others.mapped('x_total_price') or [0])
                vendor.x_savings_vs_highest = max_price - vendor.x_total_price
            else:
                vendor.x_savings_vs_highest = 0.0


class CSVendorLine(models.Model):
    _name = 'x.cs.vendor.line'
    _description = 'CS Vendor Line Item'

    x_cs_vendor_id = fields.Many2one('x.cs.vendor', string='Vendor', ondelete='cascade')
    x_product_id = fields.Many2one('product.product', string='Product', required=True)
    x_uom_id = fields.Many2one('uom.uom', string='UoM', related='x_product_id.uom_id', readonly=True)
    x_qty = fields.Float(string='Quantity', digits='Product Unit of Measure')
    x_unit_price = fields.Float(string='Unit Price', digits='Product Price')
    x_total_price = fields.Float(
        string='Total Price', compute='_compute_line_totals', store=True
    )
    x_gst_rate = fields.Float(string='GST %', default=0.0)
    x_gst_amount = fields.Float(
        string='GST Amount', compute='_compute_line_totals', store=True
    )
    x_net_price = fields.Float(
        string='Net Price', compute='_compute_line_totals', store=True
    )
    x_remarks = fields.Char(string='Remarks')

    @api.depends('x_qty', 'x_unit_price', 'x_gst_rate')
    def _compute_line_totals(self):
        for line in self:
            line.x_total_price = line.x_qty * line.x_unit_price
            line.x_gst_amount = line.x_total_price * (line.x_gst_rate / 100.0)
            line.x_net_price = line.x_total_price + line.x_gst_amount
