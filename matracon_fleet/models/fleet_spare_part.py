"""x.fleet.spare.part — Spare parts issued from inventory against a vehicle."""

from odoo import models, fields, api
from odoo.exceptions import UserError, ValidationError


SPARE_CATEGORIES = [
    ('engine', 'Engine Parts'),
    ('tyres', 'Tyres & Wheels'),
    ('electrical', 'Electrical'),
    ('filters', 'Filters'),
    ('body', 'Body & Frame'),
    ('other', 'Other'),
]


class FleetSparePart(models.Model):
    _name = 'x.fleet.spare.part'
    _description = 'Fleet Spare Part Log'
    _order = 'date desc, id desc'
    _inherit = ['mail.thread']

    vehicle_id = fields.Many2one(
        'fleet.vehicle', string='Vehicle', required=True,
        ondelete='cascade', index=True, tracking=True)
    x_mppl_code = fields.Char(
        string='MPPL Code', related='vehicle_id.x_mppl_code', store=True, readonly=True)
    x_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Project Site',
        related='vehicle_id.x_analytic_account_id', store=True, readonly=True)
    x_site_config_id = fields.Many2one(
        'project.site.config', string='Site',
        related='vehicle_id.x_site_config_id', store=True, readonly=True)

    date = fields.Date(
        string='Date', required=True, default=fields.Date.context_today, tracking=True)
    product_id = fields.Many2one(
        'product.product', string='Spare Part / Product', required=True,
        domain=[('type', 'in', ('product', 'consu'))])
    x_spare_category = fields.Selection(
        SPARE_CATEGORIES, string='Category', required=True, default='other', tracking=True)
    description = fields.Text(
        string='Description / Work Done', required=True,
        help='Mandatory: describe the repair or replacement performed.')
    quantity = fields.Float(string='Quantity', required=True, default=1.0, digits=(16, 3))
    x_unit_cost = fields.Float(string='Unit Cost (PKR)', digits=(16, 2))
    x_total_cost = fields.Monetary(
        string='Total Cost (PKR)', compute='_compute_total_cost',
        currency_field='currency_id', store=True)
    currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id)

    # ── Inventory link ────────────────────────────────────────────────────────
    picking_id = fields.Many2one(
        'stock.picking', string='Stock Picking',
        domain=[('state', '=', 'done')],
        help='Inventory transfer that issued this spare part.')
    picking_required = fields.Boolean(
        string='Picking Required', compute='_compute_picking_required')

    # ── Meter at service ──────────────────────────────────────────────────────
    x_meter_at_service = fields.Float(
        string='Meter at Service', digits=(16, 2),
        help='Odometer / hour-meter reading at time of replacement.')
    x_meter_unit = fields.Selection(
        related='vehicle_id.x_meter_unit', string='Unit', readonly=True)

    state = fields.Selection([
        ('draft', 'Draft'),
        ('posted', 'Posted'),
    ], default='draft', tracking=True, string='Status')

    @api.depends('quantity', 'x_unit_cost')
    def _compute_total_cost(self):
        for rec in self:
            rec.x_total_cost = rec.quantity * rec.x_unit_cost

    def _compute_picking_required(self):
        settings = self.env['x.fleet.settings']._get_singleton()
        req = settings.spare_parts_flow == 'inventory'
        for rec in self:
            rec.picking_required = req

    @api.onchange('picking_id')
    def _onchange_picking_fill_cost(self):
        """Pull unit cost from the stock move valuation."""
        if self.picking_id and self.product_id:
            move = self.picking_id.move_ids.filtered(
                lambda m: m.product_id == self.product_id)
            if move:
                self.x_unit_cost = move[0].price_unit

    @api.onchange('product_id')
    def _onchange_product_fill_cost(self):
        if self.product_id:
            self.x_unit_cost = self.product_id.standard_price

    @api.constrains('description')
    def _check_description(self):
        for rec in self:
            if not (rec.description or '').strip():
                raise ValidationError('Description / Work Done is mandatory for spare parts.')

    @api.constrains('picking_id', 'state')
    def _check_picking_required(self):
        settings = self.env['x.fleet.settings']._get_singleton()
        if settings.spare_parts_flow == 'inventory':
            for rec in self:
                if rec.state == 'posted' and not rec.picking_id:
                    raise ValidationError(
                        'A stock picking is required '
                        '(Fleet Settings → Spare Parts Flow = Always from Inventory).')

    def action_post(self):
        settings = self.env['x.fleet.settings']._get_singleton()
        for rec in self:
            if not (rec.description or '').strip():
                raise UserError('Description / Work Done is mandatory.')
            if settings.spare_parts_flow == 'inventory' and not rec.picking_id:
                raise UserError(
                    'Stock picking is required. Please link the inventory transfer.')
            rec.state = 'posted'

    def action_reset_draft(self):
        for rec in self:
            rec.state = 'draft'
