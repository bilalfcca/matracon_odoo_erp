# -*- coding: utf-8 -*-
from odoo import api, models


class PurchaseRequisitionCreateAlternative(models.TransientModel):
    _inherit = 'purchase.requisition.create.alternative'

    def _matracon_header_vals_from_origin(self, origin_po):
        """Copy PR / RFQ header context onto vendor alternative RFQs."""
        if not origin_po:
            return {}
        vals = {
            'x_project_analytic_account_id': origin_po.x_project_analytic_account_id.id,
            'x_category_id': origin_po.x_category_id.id,
            'x_initiator_id': origin_po.x_initiator_id.id,
            'picking_type_id': origin_po.picking_type_id.id,
            'notes': origin_po.notes,
            'origin': origin_po.origin or origin_po.name,
        }
        return {k: v for k, v in vals.items() if v}

    def _get_alternative_values(self):
        vals_list = super()._get_alternative_values()
        origin_po = self.origin_po_id
        if not origin_po:
            return vals_list
        header_extra = self._matracon_header_vals_from_origin(origin_po)
        for vals in vals_list:
            vals.update(header_extra)
        return vals_list

    @api.model
    def _get_alternative_line_value(self, order_line, product_tmpl_ids_with_description):
        vals = super()._get_alternative_line_value(
            order_line, product_tmpl_ids_with_description)
        qty = order_line.x_requested_qty or order_line.product_qty
        vals.update({
            'product_qty': qty,
            'x_requested_qty': qty,
            'x_recommended_qty': order_line.x_recommended_qty or qty,
            'date_planned': order_line.date_planned,
        })
        if order_line.analytic_distribution:
            vals['analytic_distribution'] = order_line.analytic_distribution
        return vals

    def action_create_alternative(self):
        action = super().action_create_alternative()
        if self.origin_po_id:
            self.origin_po_id._sync_tender_project_from_root()
        return action
