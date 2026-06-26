from odoo import models, fields, api, _


class PurchaseOrderSiteOps(models.Model):
    _inherit = 'purchase.order'

    x_vendor_bill_count = fields.Integer(compute='_compute_x_vendor_bill_count')
    x_liability_sheet_count = fields.Integer(compute='_compute_x_liability_sheet_count')

    @api.depends('name')
    def _compute_x_vendor_bill_count(self):
        Bill = self.env['account.move']
        for order in self:
            order.x_vendor_bill_count = Bill.search_count([
                ('move_type', '=', 'in_invoice'),
                ('x_purchase_order_id', '=', order.id),
            ])

    @api.depends('x_project_analytic_account_id')
    def _compute_x_liability_sheet_count(self):
        Sheet = self.env['x.liability.sheet']
        for order in self:
            if order.x_project_analytic_account_id:
                order.x_liability_sheet_count = Sheet.search_count([
                    ('project_analytic_account_id', '=', order.x_project_analytic_account_id.id),
                ])
            else:
                order.x_liability_sheet_count = 0

    def action_view_vendor_bills(self):
        self.ensure_one()
        bills = self.env['account.move'].search([
            ('move_type', '=', 'in_invoice'),
            ('x_purchase_order_id', '=', self.id),
        ])
        return {
            'type': 'ir.actions.act_window',
            'name': _('Vendor Bills'),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('id', 'in', bills.ids)],
            'context': {'default_move_type': 'in_invoice', 'default_x_purchase_order_id': self.id},
        }

    def action_view_liability_sheets(self):
        self.ensure_one()
        domain = [('id', '=', 0)]
        if self.x_project_analytic_account_id:
            domain = [('project_analytic_account_id', '=', self.x_project_analytic_account_id.id)]
        return {
            'type': 'ir.actions.act_window',
            'name': _('Liability Sheets'),
            'res_model': 'x.liability.sheet',
            'view_mode': 'list,form',
            'domain': domain,
        }
