from odoo import models, fields, api


class StockMoveSiteOps(models.Model):
    _inherit = 'stock.move'

    x_qty_on_hand = fields.Float(
        string='On Hand',
        compute='_compute_x_qty_on_hand',
        digits='Product Unit of Measure',
        help='Current quantity on hand at the source location. Not printed on reports.',
    )

    @api.depends('product_id', 'location_id')
    def _compute_x_qty_on_hand(self):
        for move in self:
            if move.product_id and move.location_id:
                move.x_qty_on_hand = self.env['stock.quant']._get_available_quantity(
                    move.product_id, move.location_id)
            else:
                move.x_qty_on_hand = 0.0

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
            # If x_unit_cost is 0/False, leave existing value (allows manual override)

    @api.onchange('product_id')
    def _onchange_product_id_cost(self):
        if self.product_id:
            self.x_unit_cost = self.product_id.standard_price

    @api.onchange('product_uom_qty', 'x_unit_cost')
    def _onchange_qty_cost(self):
        if self.x_unit_cost:
            self.x_line_backcharge_amount = self.x_unit_cost * self.product_uom_qty
