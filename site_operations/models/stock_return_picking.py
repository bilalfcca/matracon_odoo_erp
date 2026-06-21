from odoo import models, fields, api, _
from odoo.exceptions import UserError


class StockReturnPickingSiteOps(models.TransientModel):
    _inherit = 'stock.return.picking'

    def _create_return(self):
        """After standard return picking is created, tag it with our custom fields."""
        new_picking = super()._create_return()
        orig = self.picking_id
        if orig and orig.x_transfer_purpose in ('material_issuance', 'site_to_site'):
            new_picking.write({
                'x_is_return_transfer': True,
                'x_original_issuance_id': orig.id,
                # Preserve issuance context from original
                'x_contact_id': orig.x_contact_id.id,
                'x_issuance_project_id': orig.x_issuance_project_id.id,
                'x_inventory_type': orig.x_inventory_type,
                'x_issue_type': orig.x_issue_type,
                'x_transfer_purpose': orig.x_transfer_purpose,
            })
        return new_picking


class StockReturnPickingLineSiteOps(models.TransientModel):
    _inherit = 'stock.return.picking.line'

    x_issued_qty = fields.Float(
        string='Issued', compute='_compute_x_outstanding', digits='Product Unit of Measure')
    x_returned_qty = fields.Float(
        string='Returned', compute='_compute_x_outstanding', digits='Product Unit of Measure')
    x_outstanding_qty = fields.Float(
        string='Outstanding', compute='_compute_x_outstanding', digits='Product Unit of Measure')

    @api.depends('product_id', 'wizard_id.picking_id')
    def _compute_x_outstanding(self):
        for line in self:
            orig = line.wizard_id.picking_id
            if not orig or orig.x_transfer_purpose not in ('material_issuance', 'site_to_site'):
                line.x_issued_qty = 0.0
                line.x_returned_qty = 0.0
                line.x_outstanding_qty = 0.0
                continue

            # Qty issued on the original picking for this product
            issued = sum(
                m.quantity for m in orig.move_ids
                if m.product_id == line.product_id and m.state == 'done'
            )

            # Qty already returned in previous done return pickings
            prev_returns = self.env['stock.picking'].search([
                ('x_original_issuance_id', '=', orig.id),
                ('state', '=', 'done'),
            ])
            returned = sum(
                m.quantity for ret in prev_returns
                for m in ret.move_ids
                if m.product_id == line.product_id
            )

            line.x_issued_qty = issued
            line.x_returned_qty = returned
            line.x_outstanding_qty = max(issued - returned, 0.0)
