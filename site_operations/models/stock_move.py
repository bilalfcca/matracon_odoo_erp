from odoo import models, fields, api


class StockMoveSiteOps(models.Model):
    _inherit = 'stock.move'

    x_unit_cost = fields.Float(
        string='Unit Cost',
        help='Cost per unit for backcharge calculation. Defaults to product standard price.')

    x_line_backcharge_amount = fields.Float(
        string='Line Backcharge Amount',
        compute='_compute_line_backcharge',
        store=True,
        readonly=False,
        help='Unit Cost × Quantity. Can be overridden manually.')

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
