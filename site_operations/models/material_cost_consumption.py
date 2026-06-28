from odoo import models, fields, api, _


class MaterialCostConsumption(models.Model):
    """
    Read-only transient dashboard model: aggregates material issuance costs
    per project. Populated fresh on each action_refresh call.
    """
    _name = 'x.material.cost.report'
    _description = 'Material Cost Consumption Report'
    _order = 'project_analytic_account_id, product_category_id'

    project_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Project', readonly=True)
    product_category_id = fields.Many2one(
        'product.category', string='Category', readonly=True)
    product_id = fields.Many2one(
        'product.product', string='Product', readonly=True)
    product_uom = fields.Many2one(
        'uom.uom', string='Unit', readonly=True)
    issued_qty = fields.Float(
        string='Issued Qty', digits=(16, 3), readonly=True)
    returned_qty = fields.Float(
        string='Returned Qty', digits=(16, 3), readonly=True)
    net_qty = fields.Float(
        string='Net Consumed', digits=(16, 3), readonly=True)
    unit_cost = fields.Monetary(
        string='Unit Cost', currency_field='currency_id', readonly=True)
    total_cost = fields.Monetary(
        string='Total Cost', currency_field='currency_id', readonly=True)
    currency_id = fields.Many2one(
        'res.currency', readonly=True,
        default=lambda self: self.env.company.currency_id)

    @api.model
    def action_refresh_report(self):
        """Rebuild the material cost report from stock moves."""
        self.search([]).unlink()

        StockMove = self.env['stock.move'].sudo()
        # Issuances: done moves on material issuance pickings (not returns)
        # x_issuance_project_id is the analytic field on site_operations stock.picking
        issuance_moves = StockMove.search([
            ('state', '=', 'done'),
            ('picking_id.x_transfer_purpose', '=', 'material_issuance'),
            ('picking_id.x_is_return_transfer', '=', False),
            ('picking_id.x_issuance_project_id', '!=', False),
        ])
        # Returns: return picking moves (same purpose, is_return_transfer=True)
        return_moves = StockMove.search([
            ('state', '=', 'done'),
            ('picking_id.x_transfer_purpose', '=', 'material_issuance'),
            ('picking_id.x_is_return_transfer', '=', True),
            ('picking_id.x_issuance_project_id', '!=', False),
        ])

        # Aggregate by project + product
        data = {}
        for move in issuance_moves:
            project = move.picking_id.x_issuance_project_id
            product = move.product_id
            key = (project.id, product.id)
            if key not in data:
                data[key] = {
                    'project_analytic_account_id': project.id,
                    'product_id': product.id,
                    'product_category_id': product.categ_id.id,
                    'product_uom': move.product_uom.id,
                    'issued_qty': 0.0,
                    'returned_qty': 0.0,
                    'unit_cost': move.price_unit or 0.0,
                    'currency_id': self.env.company.currency_id.id,
                }
            data[key]['issued_qty'] += move.quantity

        for move in return_moves:
            project = move.picking_id.x_issuance_project_id
            product = move.product_id
            key = (project.id, product.id)
            if key in data:
                data[key]['returned_qty'] += move.quantity

        records = []
        for entry in data.values():
            net = entry['issued_qty'] - entry['returned_qty']
            entry['net_qty'] = max(net, 0.0)
            entry['total_cost'] = entry['net_qty'] * entry['unit_cost']
            records.append(entry)

        if records:
            self.create(records)

        return {
            'type': 'ir.actions.act_window',
            'name': _('Material Cost Consumption'),
            'res_model': 'x.material.cost.report',
            'view_mode': 'list',
            'target': 'current',
        }
