"""x.fleet.spare.part — Spare parts issued from inventory against a vehicle."""

import logging
from odoo import models, fields, api
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

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
        'x.project.site.config', string='Site',
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

    # ── GL Journal Entry ──────────────────────────────────────────────────────
    x_account_move_id = fields.Many2one(
        'account.move', string='Journal Entry', readonly=True, copy=False,
        help='GL journal entry created or linked when this spare part is posted.')
    x_gl_posted = fields.Boolean(
        string='GL Posted', compute='_compute_gl_posted', store=False)

    state = fields.Selection([
        ('draft', 'Draft'),
        ('posted', 'Posted'),
    ], default='draft', tracking=True, string='Status')

    @api.depends('quantity', 'x_unit_cost')
    def _compute_total_cost(self):
        for rec in self:
            rec.x_total_cost = rec.quantity * rec.x_unit_cost

    @api.depends('x_account_move_id', 'x_account_move_id.state')
    def _compute_gl_posted(self):
        for rec in self:
            rec.x_gl_posted = bool(
                rec.x_account_move_id and rec.x_account_move_id.state == 'posted')

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

    # ── GL helpers ────────────────────────────────────────────────────────────

    def _get_misc_journal(self):
        """Return the first Miscellaneous (general) journal, or raise."""
        journal = self.env['account.journal'].search(
            [('type', '=', 'general'), ('company_id', '=', self.env.company.id)], limit=1)
        if not journal:
            raise UserError(
                'No Miscellaneous journal found. '
                'Please create a General type journal in Accounting → Configuration → Journals.')
        return journal

    def _link_inventory_picking_move(self, rec):
        """Link and analytically-stamp the stock valuation JE from the picking."""
        account_moves = rec.picking_id.move_ids.mapped('account_move_ids')
        if not account_moves:
            # Perpetual valuation JE not found (periodic valuation in use)
            _logger.info(
                'Fleet spare part %s: picking %s has no account moves (periodic valuation?). '
                'No GL entry linked.', rec.id, rec.picking_id.name)
            return

        # Stamp analytic on expense/cost lines of the stock valuation JE
        analytic = rec.x_analytic_account_id
        if analytic:
            analytic_dist = {str(analytic.id): 100.0}
            for am in account_moves:
                for aml in am.sudo().line_ids:
                    if aml.account_id.account_type and 'expense' in aml.account_id.account_type:
                        try:
                            aml.sudo().with_context(
                                check_move_validity=False,
                            ).write({'analytic_distribution': analytic_dist})
                        except Exception as e:
                            _logger.warning(
                                'Fleet spare part %s: could not stamp analytic on '
                                'account.move.line %s: %s', rec.id, aml.id, e)

        # Link to the first valuation JE
        rec.x_account_move_id = account_moves[0].id

    def _create_cash_purchase_je(self, rec):
        """Create a standalone JE for a cash spare part (no stock picking)."""
        settings = self.env['x.fleet.settings']._get_singleton()

        expense_account = settings.account_spare_parts_id
        if not expense_account:
            raise UserError(
                'No GL expense account configured for Spare Parts. '
                'Go to Fleet → Configuration → Fleet Settings and set '
                '"Spare Parts & Maintenance" account.')

        credit_account = settings.account_spare_parts_credit_id
        if not credit_account:
            raise UserError(
                'No credit account configured for cash spare part purchases. '
                'Go to Fleet → Configuration → Fleet Settings and set '
                '"Spare Parts — Credit Account".')

        analytic = rec.x_analytic_account_id
        analytic_distribution = {str(analytic.id): 100.0} if analytic else {}
        journal = self._get_misc_journal()
        ref = (
            f"Fleet Spare Part — "
            f"{rec.vehicle_id.x_mppl_code or rec.vehicle_id.name} / "
            f"{dict(SPARE_CATEGORIES).get(rec.x_spare_category, '')}"
        )
        line_name = (
            f"{rec.vehicle_id.name} — {rec.product_id.name or ''} "
            f"({dict(SPARE_CATEGORIES).get(rec.x_spare_category, '')})"
        )
        move_vals = {
            'move_type': 'entry',
            'date': rec.date or fields.Date.context_today(rec),
            'ref': ref,
            'journal_id': journal.id,
            'line_ids': [
                (0, 0, {
                    'name': line_name,
                    'account_id': expense_account.id,
                    'debit': rec.x_total_cost,
                    'credit': 0.0,
                    'analytic_distribution': analytic_distribution,
                }),
                (0, 0, {
                    'name': line_name,
                    'account_id': credit_account.id,
                    'debit': 0.0,
                    'credit': rec.x_total_cost,
                    'analytic_distribution': {},
                }),
            ],
        }
        move = self.env['account.move'].create(move_vals)
        move.action_post()
        rec.x_account_move_id = move

    # ── Post / reset ──────────────────────────────────────────────────────────

    def action_post(self):
        settings = self.env['x.fleet.settings']._get_singleton()
        for rec in self:
            if not (rec.description or '').strip():
                raise UserError('Description / Work Done is mandatory.')
            if settings.spare_parts_flow == 'inventory' and not rec.picking_id:
                raise UserError(
                    'Stock picking is required. Please link the inventory transfer.')

            if rec.x_account_move_id:
                # GL already linked — just set state
                rec.state = 'posted'
                continue

            if rec.picking_id:
                # Inventory flow: link and tag the stock valuation JE
                self._link_inventory_picking_move(rec)
            else:
                # Cash purchase: create a standalone JE
                if rec.x_total_cost > 0:
                    self._create_cash_purchase_je(rec)

            rec.state = 'posted'

    def action_reset_draft(self):
        for rec in self:
            rec.state = 'draft'
