from markupsafe import Markup, escape
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class ComparativeStatement(models.Model):
    _name = 'x.comparative.statement'
    _description = 'Comparative Statement'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(string='Reference', required=True, tracking=True)
    x_purchase_order_id = fields.Many2one(
        'purchase.order', string='Purchase Requisition',
        required=True, tracking=True, ondelete='cascade',
    )
    x_state = fields.Selection([
        ('draft', 'Draft'),
        ('confirmed', 'Confirmed'),
    ], default='draft', string='State', tracking=True)

    x_vendor_line_ids = fields.One2many(
        'x.cs.vendor', 'x_cs_id', string='Vendor Comparisons',
    )

    # ── Winner selection ──────────────────────────────────────────────────────
    x_recommended_vendor_line_id = fields.Many2one(
        'x.cs.vendor', string='Winner',
        domain="[('x_cs_id', '=', id)]",
        tracking=True, copy=False,
        help='The selected winning vendor. '
             'Use the "⭐ Select as Winner" button on a vendor row to set this.',
    )
    x_recommended_vendor_id = fields.Many2one(
        'res.partner', string='Recommended Vendor',
        related='x_recommended_vendor_line_id.x_partner_id',
        store=True, readonly=True, tracking=True,
    )

    x_negotiation_notes = fields.Text(string='Negotiation Notes / Audit Trail')

    # ── Cross-vendor comparison matrix (computed HTML) ────────────────────────
    x_comparison_html = fields.Html(
        string='Comparison Matrix',
        compute='_compute_comparison_html',
        sanitize=False,
    )

    @api.depends(
        'x_vendor_line_ids.x_partner_id',
        'x_vendor_line_ids.x_is_recommended',
        'x_vendor_line_ids.x_total_price',
        'x_vendor_line_ids.x_line_ids.x_product_id',
        'x_vendor_line_ids.x_line_ids.x_qty',
        'x_vendor_line_ids.x_line_ids.x_unit_price',
        'x_vendor_line_ids.x_line_ids.x_total_price',
    )
    def _compute_comparison_html(self):
        for cs in self:
            vendors = cs.x_vendor_line_ids
            if not vendors:
                cs.x_comparison_html = Markup(
                    '<p class="text-muted fst-italic mt-2 mb-2">'
                    'Add vendors in the <strong>Vendors &amp; Terms</strong> tab '
                    'to see the price comparison matrix here.'
                    '</p>'
                )
                continue

            # Collect all unique products (ordered by first appearance)
            prod_map = {}
            for vendor in vendors:
                for line in vendor.x_line_ids:
                    p = line.x_product_id
                    if p and p.id not in prod_map:
                        prod_map[p.id] = p

            if not prod_map:
                cs.x_comparison_html = Markup(
                    '<p class="text-muted fst-italic mt-2 mb-2">'
                    'Add product lines to each vendor in the '
                    '<strong>Quote Lines</strong> tab to populate this matrix.'
                    '</p>'
                )
                continue

            # Build vendor → product → line lookup
            vp_map = {}
            for vendor in vendors:
                vp_map[vendor.id] = {}
                for line in vendor.x_line_ids:
                    if line.x_product_id:
                        vp_map[vendor.id][line.x_product_id.id] = line

            # ── Build HTML table ──────────────────────────────────────────────
            parts = []
            parts.append(Markup(
                '<div style="overflow-x:auto;margin-top:4px;">'
                '<table style="width:100%;border-collapse:collapse;font-size:11px;">'
                '<thead>'
            ))

            # ── Vendor header row ─────────────────────────────────────────────
            parts.append(Markup('<tr>'))
            _hdr_base = 'border:1px solid #1a3a5c;padding:6px 8px;color:#fff;'
            _hdr_reg = _hdr_base + 'background:#1a3a5c;text-align:center;'
            _hdr_win = _hdr_base + 'background:#155724;text-align:center;font-weight:bold;font-size:12px;'

            for label in ('#', 'Product', 'UoM', 'Qty'):
                parts.append(
                    Markup('<th rowspan="2" style="') + Markup(_hdr_reg) + Markup('">')
                    + Markup(label)
                    + Markup('</th>')
                )

            for vendor in vendors:
                style = _hdr_win if vendor.x_is_recommended else _hdr_reg
                star = Markup('&#9733; ') if vendor.x_is_recommended else Markup('')
                parts.append(
                    Markup('<th colspan="2" style="') + Markup(style) + Markup('">')
                    + star
                    + escape(vendor.x_partner_id.name or 'Vendor')
                    + Markup('</th>')
                )
            parts.append(Markup('</tr>'))

            # ── Rate / Total sub-header row ───────────────────────────────────
            parts.append(Markup('<tr>'))
            for vendor in vendors:
                style = _hdr_win if vendor.x_is_recommended else _hdr_reg
                parts.append(
                    Markup('<th style="') + Markup(style) + Markup('">Rate</th>')
                )
                parts.append(
                    Markup('<th style="') + Markup(style) + Markup('">Total</th>')
                )
            parts.append(Markup('</tr></thead><tbody>'))

            # ── Product rows ──────────────────────────────────────────────────
            _td = 'border:1px solid #ccc;padding:4px 7px;'
            _tdr = _td + 'text-align:right;'
            _tdc = _td + 'text-align:center;'

            for seq, (prod_id, product) in enumerate(prod_map.items(), 1):
                # Grab qty/uom from first vendor that has this product
                qty = 0.0
                uom_name = ''
                for vendor in vendors:
                    line = vp_map[vendor.id].get(prod_id)
                    if line:
                        qty = line.x_qty
                        uom_name = line.x_uom_id.name or ''
                        break

                # Lowest total for highlight
                totals = [
                    vp_map[v.id][prod_id].x_total_price
                    for v in vendors
                    if prod_id in vp_map[v.id] and vp_map[v.id][prod_id].x_total_price > 0
                ]
                min_price = min(totals) if totals else 0.0

                # Row background alternating
                row_bg = '#ffffff' if seq % 2 else '#f9f9f9'
                parts.append(Markup('<tr style="background:') + Markup(row_bg) + Markup(';">'))

                parts.append(Markup('<td style="') + Markup(_tdc) + Markup('">')
                             + Markup(str(seq)) + Markup('</td>'))
                parts.append(Markup('<td style="') + Markup(_td) + Markup('">')
                             + escape(product.display_name) + Markup('</td>'))
                parts.append(Markup('<td style="') + Markup(_tdc) + Markup('">')
                             + escape(uom_name) + Markup('</td>'))
                parts.append(Markup('<td style="') + Markup(_tdr) + Markup('">')
                             + Markup('%.3g' % qty) + Markup('</td>'))

                for vendor in vendors:
                    line = vp_map[vendor.id].get(prod_id)
                    if line and (line.x_unit_price or line.x_total_price):
                        is_best = min_price > 0 and abs(line.x_total_price - min_price) < 0.001
                        if is_best:
                            cell_bg = '#c3e6cb'
                            fw = 'bold'
                        elif vendor.x_is_recommended:
                            cell_bg = '#e8f5e9'
                            fw = 'normal'
                        else:
                            cell_bg = row_bg
                            fw = 'normal'
                        cell_style = (
                            _tdr
                            + 'background:' + cell_bg + ';'
                            + 'font-weight:' + fw + ';'
                        )
                        parts.append(
                            Markup('<td style="') + Markup(cell_style) + Markup('">')
                            + Markup('{:,.2f}'.format(line.x_unit_price))
                            + Markup('</td>')
                        )
                        parts.append(
                            Markup('<td style="') + Markup(cell_style) + Markup('">')
                            + Markup('{:,.2f}'.format(line.x_total_price))
                            + Markup('</td>')
                        )
                    else:
                        parts.append(
                            Markup('<td colspan="2" style="') + Markup(_tdc)
                            + Markup('" style="color:#999;font-style:italic;">'
                                     '<span style="color:#bbb;">— not quoted —</span></td>')
                        )
                parts.append(Markup('</tr>'))

            # ── Footer totals ─────────────────────────────────────────────────
            parts.append(Markup(
                '<tfoot>'
                '<tr style="font-weight:bold;border-top:2px solid #333;">'
            ))
            parts.append(
                Markup('<td colspan="4" style="') + Markup(_tdr)
                + Markup('background:#e8f0fe;">GRAND TOTAL</td>')
            )
            for vendor in vendors:
                foot_bg = '#c3e6cb' if vendor.x_is_recommended else '#e8f0fe'
                foot_style = _tdr + 'background:' + foot_bg + ';font-weight:bold;'
                parts.append(
                    Markup('<td colspan="2" style="') + Markup(foot_style) + Markup('">')
                    + Markup('{:,.2f}'.format(vendor.x_total_price))
                    + Markup('</td>')
                )
            parts.append(Markup('</tr></tfoot></table></div>'))

            cs.x_comparison_html = Markup('').join(parts)

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_confirm(self):
        """Lock CS and route PR to CEO via the HO-approval path."""
        for cs in self:
            if not cs.x_recommended_vendor_line_id:
                raise UserError(_(
                    'Please select a winning vendor first.\n\n'
                    'Click "⭐ Select as Winner" on the vendor row in the '
                    '"Vendors & Terms" tab, then confirm.'
                ))
            if not cs.x_recommended_vendor_line_id.x_partner_id:
                raise UserError(_(
                    'The selected winning vendor must have a partner set.'
                ))

            cs.x_state = 'confirmed'

            if cs.x_purchase_order_id:
                cs.x_purchase_order_id.action_confirm_cs(cs)

            cs.message_post(
                body=_(
                    'Comparative Statement confirmed. '
                    'Selected vendor: %s.'
                ) % cs.x_recommended_vendor_line_id.x_partner_id.name,
                subtype_xmlid='mail.mt_log_note',
            )

    def action_print(self):
        """Print CS PDF report."""
        self.ensure_one()
        return self.env.ref(
            'purchase_demand_raise.action_report_cs_from_cs'
        ).report_action(self)


class CSVendor(models.Model):
    _name = 'x.cs.vendor'
    _description = 'CS Vendor Comparison'
    _order = 'sequence, id'

    x_cs_id = fields.Many2one(
        'x.comparative.statement', string='CS', ondelete='cascade', index=True,
    )
    sequence = fields.Integer(string='Sequence', default=10)

    x_partner_id = fields.Many2one(
        'res.partner', string='Vendor', required=True,
        domain="[('category_id.name', '=', 'Vendor')]",
    )
    x_is_recommended = fields.Boolean(
        string='Winner', default=False, copy=False,
        help='Mark this vendor as the selected winner.',
    )

    # Commercial terms
    x_rfq_reference = fields.Char(string='Quotation Reference')
    x_quote_validity = fields.Date(string='Quote Validity')
    x_delivery_basis = fields.Selection([
        ('ex_works', 'Ex-Works'),
        ('for', 'FOR'),
        ('ddp', 'DDP'),
    ], string='Delivery Basis')
    x_delivery_period = fields.Char(string='Delivery Period')
    # Payment Terms — Odoo native dropdown (account.payment.term)
    x_payment_term_id = fields.Many2one(
        'account.payment.term',
        string='Payment Terms',
        help='Select from Odoo standard payment terms. '
             'Use "Custom Payment Notes" below for any extra conditions.',
    )
    x_payment_terms = fields.Char(
        string='Custom Payment Notes',
        help='Free-text payment conditions not covered by the standard terms above '
             '(e.g. "50% advance, balance after delivery").',
    )
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

    # Computed totals
    x_total_price = fields.Float(
        string='Total Price', compute='_compute_totals', store=True,
    )
    x_gst_amount = fields.Float(
        string='GST Amount', compute='_compute_totals', store=True,
    )
    x_net_total = fields.Float(
        string='Net Total', compute='_compute_totals', store=True,
    )
    x_is_lowest = fields.Boolean(
        string='Best Value', compute='_compute_is_lowest', store=True,
    )
    x_savings_vs_highest = fields.Float(
        string='Saving vs Most Expensive', compute='_compute_savings', store=True,
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
                prices = [v.x_total_price for v in others if v.x_total_price > 0]
                min_price = min(prices) if prices else 0.0
                vendor.x_is_lowest = (
                    vendor.x_total_price > 0 and vendor.x_total_price == min_price
                )
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

    # ── Winner selection ──────────────────────────────────────────────────────

    def action_select_as_recommended(self):
        """Mark this vendor as the CS winner; unmark all others."""
        self.ensure_one()
        cs = self.x_cs_id
        if cs.x_state == 'confirmed':
            raise UserError(_('Cannot change the winner on a confirmed Comparative Statement.'))
        # Unset all
        cs.x_vendor_line_ids.write({'x_is_recommended': False})
        # Set this one
        self.x_is_recommended = True
        cs.x_recommended_vendor_line_id = self
        return True

    # ── Auto-create product lines from PR on vendor addition ──────────────────

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records:
            if record.x_cs_id and record.x_cs_id.x_purchase_order_id:
                record._auto_create_lines_from_pr()
        return records

    def _auto_create_lines_from_pr(self):
        """Create x.cs.vendor.line for each product in the linked PR."""
        self.ensure_one()
        pr = self.x_cs_id.x_purchase_order_id
        if not pr:
            return
        existing_products = self.x_line_ids.mapped('x_product_id')
        to_create = []
        for pr_line in pr.order_line.filtered(lambda l: l.product_id and not l.display_type):
            if pr_line.product_id in existing_products:
                continue
            qty = (
                pr_line.x_requested_qty
                or pr_line.x_recommended_qty
                or pr_line.product_qty
                or 1.0
            )
            to_create.append({
                'x_cs_vendor_id': self.id,
                'x_product_id': pr_line.product_id.id,
                'x_qty': qty,
                'x_unit_price': 0.0,
                'x_pr_line_id': pr_line.id,
            })
        if to_create:
            self.env['x.cs.vendor.line'].create(to_create)


class CSVendorLine(models.Model):
    _name = 'x.cs.vendor.line'
    _description = 'CS Vendor Line Item'

    x_cs_vendor_id = fields.Many2one('x.cs.vendor', string='Vendor', ondelete='cascade', index=True)
    # Link to original PR line (for traceability — set during auto-creation)
    x_pr_line_id = fields.Many2one(
        'purchase.order.line', string='PR Line',
        ondelete='set null', copy=False,
        help='Original PR line this quote line was created from.',
    )
    x_product_id = fields.Many2one('product.product', string='Product', required=True)
    x_uom_id = fields.Many2one('uom.uom', string='UoM', related='x_product_id.uom_id', readonly=True)
    x_qty = fields.Float(string='Quantity', digits='Product Unit of Measure')
    x_unit_price = fields.Float(string='Unit Price', digits='Product Price')
    x_total_price = fields.Float(
        string='Total Price', compute='_compute_line_totals', store=True,
    )
    x_gst_rate = fields.Float(string='GST %', default=0.0)
    x_gst_amount = fields.Float(
        string='GST Amount', compute='_compute_line_totals', store=True,
    )
    x_net_price = fields.Float(
        string='Net Price', compute='_compute_line_totals', store=True,
    )
    x_remarks = fields.Char(string='Remarks')

    @api.depends('x_qty', 'x_unit_price', 'x_gst_rate')
    def _compute_line_totals(self):
        for line in self:
            line.x_total_price = line.x_qty * line.x_unit_price
            line.x_gst_amount = line.x_total_price * (line.x_gst_rate / 100.0)
            line.x_net_price = line.x_total_price + line.x_gst_amount
