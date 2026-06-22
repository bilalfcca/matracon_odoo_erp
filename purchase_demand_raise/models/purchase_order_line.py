from odoo import models, fields, api, _


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    @api.model_create_multi
    def create(self, vals_list):
        """When Odoo creates alternative RFQ lines (via native Alternatives feature),
        it copies product_qty but does NOT copy x_requested_qty (which stays 0.0).
        We auto-populate it here so the requested qty is visible on alternatives too.
        """
        for vals in vals_list:
            if not vals.get('x_requested_qty') and vals.get('product_qty'):
                vals['x_requested_qty'] = vals['product_qty']
        lines = super().create(vals_list)
        for line in lines:
            order = line.order_id
            if (
                order.x_quote_tax_ids
                and line.product_id
                and not line.display_type
            ):
                line.tax_ids = order.x_quote_tax_ids
        return lines

    # ── Quantity Fields ───────────────────────────────────────────────────────
    x_requested_qty = fields.Float(
        string='Requested Qty', digits='Product Unit of Measure', default=0.0,
        help='Quantity requested by Site Store. Drives the actual PO quantity.'
    )
    x_recommended_qty = fields.Float(
        string='Recommended Qty', digits='Product Unit of Measure', default=0.0,
        help='Quantity recommended by Procurement HO.'
    )
    x_qty_on_hand = fields.Float(
        string='Qty On Hand', digits='Product Unit of Measure',
        compute='_compute_qty_on_hand',
        help='Current stock at the site warehouse (live).'
    )
    x_approved_qty = fields.Float(
        string='Approved Qty', digits='Product Unit of Measure',
        compute='_compute_approved_qty',
        store=True, readonly=False,
        help='Final quantity approved by CEO. Feeds the locked PO.'
    )

    # ── CEO Decision ──────────────────────────────────────────────────────────
    x_decision = fields.Selection([
        ('full', 'Full'),
        ('manual', 'Manual'),
        ('25', '25%'),
        ('50', '50%'),
        ('75', '75%'),
    ], string='Decision', default='manual',
        help='CEO approval tier. Auto-computes Approved Qty (except Manual).'
    )

    # ── Exceeds Recommended Warning ───────────────────────────────────────────
    x_exceeds_recommended = fields.Boolean(
        compute='_compute_exceeds_recommended', string='Exceeds Recommended',
    )

    # ── Sync x_requested_qty → product_qty (the real Odoo PO quantity) ───────
    @api.onchange('x_requested_qty')
    def _onchange_x_requested_qty(self):
        """Keep standard product_qty in sync with what Site Store requests."""
        if self.x_requested_qty:
            self.product_qty = self.x_requested_qty

    @api.onchange('x_recommended_qty')
    def _onchange_x_recommended_qty(self):
        """When HO sets recommended qty → update product_qty so subtotals refresh."""
        if self.x_recommended_qty and self.x_recommended_qty > 0:
            self.product_qty = self.x_recommended_qty
        elif self.x_requested_qty:
            self.product_qty = self.x_requested_qty

    @api.onchange('product_qty')
    def _onchange_product_qty_sync(self):
        """If someone edits product_qty directly (HO/CEO), keep x_requested_qty consistent on new records."""
        if not self.x_requested_qty and self.product_qty:
            self.x_requested_qty = self.product_qty

    # ── On Hand Computation ───────────────────────────────────────────────────
    @api.depends('product_id', 'order_id.picking_type_id')
    def _compute_qty_on_hand(self):
        for line in self:
            if not line.product_id:
                line.x_qty_on_hand = 0.0
                continue
            location = line.order_id.picking_type_id.default_location_dest_id
            if location:
                quants = self.env['stock.quant'].search([
                    ('product_id', '=', line.product_id.id),
                    ('location_id', 'child_of', location.id),
                ])
                line.x_qty_on_hand = sum(quants.mapped('quantity'))
            else:
                line.x_qty_on_hand = line.product_id.qty_available

    # ── Approved Qty: auto-compute from Decision + Recommended Qty ────────────
    @api.depends('x_decision', 'x_recommended_qty')
    def _compute_approved_qty(self):
        pct = {'full': 1.0, '25': 0.25, '50': 0.50, '75': 0.75}
        for line in self:
            if line.x_decision in pct:
                line.x_approved_qty = line.x_recommended_qty * pct[line.x_decision]
            # 'manual': leave whatever is stored

    @api.onchange('x_decision')
    def _onchange_decision(self):
        """Instant UI recompute when Decision changes."""
        pct = {'full': 1.0, '25': 0.25, '50': 0.50, '75': 0.75}
        if self.x_decision in pct:
            self.x_approved_qty = self.x_recommended_qty * pct[self.x_decision]

    @api.onchange('x_approved_qty')
    def _onchange_approved_qty(self):
        """If CEO manually edits Approved Qty away from the % result → switch to Manual."""
        pct = {'full': 1.0, '25': 0.25, '50': 0.50, '75': 0.75}
        if self.x_decision in pct:
            expected = self.x_recommended_qty * pct[self.x_decision]
            if abs((self.x_approved_qty or 0.0) - expected) > 0.001:
                self.x_decision = 'manual'

    @api.depends('x_approved_qty', 'x_recommended_qty')
    def _compute_exceeds_recommended(self):
        for line in self:
            line.x_exceeds_recommended = (
                line.x_approved_qty > line.x_recommended_qty > 0
            )

    # ── Role flags (computed per user) — used in view readonly/invisible ─────
    # Odoo 19 does not allow groups() inside readonly expressions on line columns;
    # we expose the user's role as a plain boolean field instead.
    x_is_site_store = fields.Boolean(compute='_compute_role_flags')
    x_is_ho = fields.Boolean(compute='_compute_role_flags')
    x_is_ceo = fields.Boolean(compute='_compute_role_flags')

    def _compute_role_flags(self):
        is_ss = self.env.user.has_group('purchase_demand_raise.group_site_store')
        is_ho = self.env.user.has_group('purchase_demand_raise.group_procurement_ho')
        is_ceo = self.env.user.has_group('purchase_demand_raise.group_ceo_approval')
        for line in self:
            line.x_is_site_store = is_ss
            line.x_is_ho = is_ho
            line.x_is_ceo = is_ceo

    # ── Auto-set analytic distribution when product added ─────────────────────
    @api.onchange('product_id')
    def _onchange_product_id_analytic(self):
        """Auto-fill analytic distribution from the order's project analytic account."""
        if self.product_id and self.order_id.x_project_analytic_account_id:
            acc_id = str(self.order_id.x_project_analytic_account_id.id)
            self.analytic_distribution = {acc_id: 100.0}

    # ── Product domain: category filter ──────────────────────────────────────
    @api.onchange('product_id')
    def _onchange_product_id_category_check(self):
        if self.order_id.x_category_id and self.product_id:
            if self.product_id.categ_id != self.order_id.x_category_id:
                self.product_id = False
                return {
                    'warning': {
                        'title': _('Category Restriction'),
                        'message': _(
                            'This product does not belong to the selected category "%s". '
                            'Please choose a product from the correct category.'
                        ) % self.order_id.x_category_id.name,
                    }
                }
