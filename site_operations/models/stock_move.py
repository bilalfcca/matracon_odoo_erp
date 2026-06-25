from odoo import models, fields, api


class StockMoveSiteOps(models.Model):
    _inherit = 'stock.move'

    x_qty_on_hand = fields.Float(
        string='On Hand',
        compute='_compute_x_qty_on_hand',
        digits='Product Unit of Measure',
        help='Current quantity on hand at the site warehouse stock location.',
    )

    def _get_on_hand_location(self):
        """Resolve warehouse main stock location for on-hand display.

        Prefer the picking/move source location (where stock is issued from),
        then the site warehouse stock location.
        """
        self.ensure_one()
        picking = self.picking_id
        # Priority 1: explicit source on the transfer (where stock is taken from)
        if picking and picking.location_id and picking.location_id.usage == 'internal':
            return picking.location_id
        if self.location_id and self.location_id.usage == 'internal':
            return self.location_id
        # Priority 2: warehouse's main stock location via picking type
        if picking and picking.picking_type_id:
            wh = picking.picking_type_id.warehouse_id
            if wh and wh.lot_stock_id:
                return wh.lot_stock_id
        # Priority 3: user's default warehouse stock location
        user = self.env.user
        if hasattr(user, 'x_default_warehouse_id') and user.x_default_warehouse_id:
            if user.x_default_warehouse_id.lot_stock_id:
                return user.x_default_warehouse_id.lot_stock_id
        # Priority 4: company's main warehouse stock location
        wh = self.env['stock.warehouse'].search([
            ('company_id', '=', self.env.company.id),
            ('code', '=', 'WH'),
        ], limit=1)
        if not wh:
            wh = self.env['stock.warehouse'].search(
                [('company_id', '=', self.env.company.id)], order='id asc', limit=1)
        if wh and wh.lot_stock_id:
            return wh.lot_stock_id
        return self.env['stock.location']

    def _qty_on_hand_at_location(self, product, location):
        if not product or not location:
            return 0.0
        quants = self.env['stock.quant'].sudo().search([
            ('product_id', '=', product.id),
            ('location_id', 'child_of', location.id),
        ])
        return sum(quants.mapped('quantity'))

    @api.depends(
        'product_id', 'location_id', 'picking_id.location_id',
        'picking_id.picking_type_id', 'picking_id.x_transfer_purpose',
    )
    def _compute_x_qty_on_hand(self):
        for move in self:
            if not move.product_id:
                move.x_qty_on_hand = 0.0
                continue
            location = move._get_on_hand_location()
            qty = move._qty_on_hand_at_location(move.product_id, location)
            if qty <= 0:
                qty = move.product_id.qty_available
            move.x_qty_on_hand = qty

    x_unit_cost = fields.Float(
        string='Unit Cost',
        help='Cost per unit for backcharge calculation. Defaults to product standard price.')

    x_line_backcharge_amount = fields.Float(
        string='Line Backcharge Amount',
        compute='_compute_line_backcharge',
        store=True,
        readonly=False,
        help='Unit Cost × Quantity. Can be overridden manually.')

    x_return_condition = fields.Selection([
        ('new', 'New'),
        ('used', 'Used'),
        ('repairable', 'Repairable'),
        ('scrap', 'Scrap'),
    ], string='Return Condition',
        help='Condition of returned material — scrap routes to scrap location.')

    x_damage_amount = fields.Float(
        string='Damage Charge',
        help='Backcharge for damaged / incomplete asset returns.')

    @api.depends('x_unit_cost', 'product_uom_qty')
    def _compute_line_backcharge(self):
        for move in self:
            if move.x_unit_cost:
                move.x_line_backcharge_amount = move.x_unit_cost * move.product_uom_qty

    @api.onchange('product_id')
    def _onchange_product_id_cost(self):
        if self.product_id:
            self.x_unit_cost = self.product_id.standard_price
            location = self._get_on_hand_location()
            qty = self._qty_on_hand_at_location(self.product_id, location)
            self.x_qty_on_hand = qty if qty > 0 else self.product_id.qty_available
        if self.product_id and self.picking_id:
            loc = self.picking_id.location_id or self.picking_id.picking_type_id.default_location_src_id
            if loc:
                self.location_id = loc

    @api.onchange('product_uom_qty', 'x_unit_cost')
    def _onchange_qty_cost(self):
        if self.x_unit_cost:
            self.x_line_backcharge_amount = self.x_unit_cost * self.product_uom_qty
