"""Product Summary Report — purchased, consumed and remaining per product.

Uses the same movement rules as the Stock Report (Excel)
(x.stock.report.xlsx.wizard._compute_rows), so both reports always agree:
  Total Purchased  = receipts from vendors
  Total Consumed   = material issued − material returned
  Remaining        = closing stock (equals on-hand) at the 'To' date
'From' empty = since the beginning.
"""
from datetime import date

from odoo import api, fields, models, _

BEGINNING = date(2000, 1, 1)


class ProductSummaryWizard(models.TransientModel):
    _name = 'x.product.summary.wizard'
    _description = 'Product Summary Report'

    date_from = fields.Date(string='From', help='Leave empty for "since the beginning".')
    date_to = fields.Date(string='To', required=True, default=fields.Date.context_today)
    product_ids = fields.Many2many('product.product', string='Products')
    categ_ids = fields.Many2many('product.category', string='Categories')
    project_ids = fields.Many2many('account.analytic.account', string='Projects')
    line_ids = fields.One2many('x.product.summary.line', 'wizard_id')

    def _stock_wizard(self):
        self.ensure_one()
        return self.env['x.stock.report.xlsx.wizard'].create({
            'date_from': self.date_from or BEGINNING,
            'date_to': self.date_to,
            'product_ids': [(6, 0, self.product_ids.ids)],
            'categ_ids': [(6, 0, self.categ_ids.ids)],
            'project_ids': [(6, 0, self.project_ids.ids)],
            'include_issued': True,
        })

    def action_show(self):
        self.ensure_one()
        rows, _issued = self._stock_wizard()._compute_rows()
        self.line_ids.unlink()
        self.env['x.product.summary.line'].create([{
            'wizard_id': self.id,
            'product_id': r['product'].id,
            'warehouse_id': r['warehouse'].id,
            'project_id': r['project'].id or False,
            'opening_qty': r['opening'],
            'purchased_qty': r['purchased'],
            'issued_qty': r['issued'],
            'returned_qty': r['returns_in'],
            'consumed_qty': r['issued'] - r['returns_in'],
            'transfer_in_qty': r['transfer_in'],
            'transfer_out_qty': r['transfer_out'],
            'adjustment_qty': r['other_in'] - r['other_out'],
            'remaining_qty': r['closing'],
        } for r in rows])
        period = (_('%(f)s to %(t)s') % {'f': self.date_from.strftime('%d/%m/%Y'),
                                          't': self.date_to.strftime('%d/%m/%Y')}
                  if self.date_from else _('since the beginning to %s') % self.date_to.strftime('%d/%m/%Y'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Product Summary — %s') % period,
            'res_model': 'x.product.summary.line',
            'view_mode': 'list,pivot',
            'domain': [('wizard_id', '=', self.id)],
            'context': {'search_default_group_product': 1, 'summary_wizard_id': self.id},
            'target': 'current',
        }

    def action_export(self):
        self.ensure_one()
        return self._stock_wizard().action_export()


class ProductSummaryLine(models.TransientModel):
    _name = 'x.product.summary.line'
    _description = 'Product Summary Line'
    _order = 'product_id, project_id'

    wizard_id = fields.Many2one('x.product.summary.wizard', ondelete='cascade', index=True)
    product_id = fields.Many2one('product.product', string='Product', readonly=True)
    default_code = fields.Char(related='product_id.default_code', string='Internal Ref')
    categ_id = fields.Many2one(related='product_id.categ_id', store=True, string='Category')
    uom_id = fields.Many2one(related='product_id.uom_id', string='UoM')
    project_id = fields.Many2one('account.analytic.account', string='Project', readonly=True)
    warehouse_id = fields.Many2one('stock.warehouse', string='Warehouse', readonly=True)
    opening_qty = fields.Float(string='Opening', readonly=True, digits='Product Unit of Measure')
    purchased_qty = fields.Float(string='Total Purchased', readonly=True, digits='Product Unit of Measure')
    issued_qty = fields.Float(string='Issued', readonly=True, digits='Product Unit of Measure')
    returned_qty = fields.Float(string='Returned', readonly=True, digits='Product Unit of Measure')
    consumed_qty = fields.Float(string='Total Consumed', readonly=True, digits='Product Unit of Measure',
                                help='Issued − Returned.')
    transfer_in_qty = fields.Float(string='Transfers In', readonly=True, digits='Product Unit of Measure')
    transfer_out_qty = fields.Float(string='Transfers Out', readonly=True, digits='Product Unit of Measure')
    adjustment_qty = fields.Float(string='Adjustments (±)', readonly=True, digits='Product Unit of Measure')
    remaining_qty = fields.Float(string='Remaining Available', readonly=True, digits='Product Unit of Measure')
    utilization_pct = fields.Float(string='Consumed %', compute='_compute_utilization', aggregator=None,
                                   help='Consumed as a share of (Opening + Purchased + Transfers In).')

    @api.depends('consumed_qty', 'opening_qty', 'purchased_qty', 'transfer_in_qty')
    def _compute_utilization(self):
        for line in self:
            base = line.opening_qty + line.purchased_qty + line.transfer_in_qty
            line.utilization_pct = (line.consumed_qty / base * 100.0) if base > 0 else 0.0

    def action_download_excel(self):
        wiz = self.env['x.product.summary.wizard'].browse(self.env.context.get('summary_wizard_id'))
        if not wiz.exists():
            wiz = self.wizard_id[:1]
        return wiz.action_export()
