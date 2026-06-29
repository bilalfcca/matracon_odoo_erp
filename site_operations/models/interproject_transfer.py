"""Inter-Project Transfer register.

Captures every balance created between two projects — both types:

  • CASH: Finance HO pays a vendor for Project A using Project B's funds.
          Project A owes Project B the payment amount.

  • INVENTORY: Site-to-site material transfer. Source project sends materials
               to destination project. Destination owes source the material value
               (qty × standard_price). GL entry on 13100/21100.

The register is the source-of-truth for "who owes whom" across projects since
the GL accounts (13100/21100) are asset_current / liability_current — correctly
excluded from aged AR/AP, which should only show real external vendor payables.
"""

from odoo import models, fields, api, _
from odoo.exceptions import UserError


class InterprojectTransfer(models.Model):
    _name = 'x.interproject.transfer'
    _description = 'Inter-Project Transfer'
    _inherit = ['mail.thread']
    _order = 'date desc, id desc'
    _rec_name = 'name'

    name = fields.Char(
        string='Reference', readonly=True,
        default=lambda self: _('New'), copy=False)

    date = fields.Date(
        string='Date', required=True, readonly=True,
        default=fields.Date.context_today)

    transfer_type = fields.Selection([
        ('cash', 'Cash / Vendor Payment'),
        ('inventory', 'Material Transfer (S2S)'),
    ], string='Type', required=True, readonly=True, default='cash',
        help='Cash: FO funded one project\'s vendor using another project\'s funds.\n'
             'Inventory: Site sent materials to another site — destination owes source the material value.')

    # ── Cash transfer link ─────────────────────────────────────────────────
    payment_id = fields.Many2one(
        'account.payment',
        string='Vendor Payment',
        readonly=True, ondelete='set null', index=True,
        help='The outbound vendor payment that triggered this cash inter-project transfer.')

    # ── Inventory transfer link ────────────────────────────────────────────
    picking_id = fields.Many2one(
        'stock.picking',
        string='Site-to-Site Transfer',
        readonly=True, ondelete='set null', index=True,
        help='The outbound site-to-site stock transfer that generated this balance.')

    # ── GL entry link ──────────────────────────────────────────────────────
    move_id = fields.Many2one(
        'account.move',
        string='Journal Entry',
        readonly=True, ondelete='set null',
        help='The inter-project GL entry (DR 13100 / CR 21100).')

    # ── Projects ───────────────────────────────────────────────────────────
    source_analytic_id = fields.Many2one(
        'account.analytic.account',
        string='Lending / Sending Project',
        required=True, readonly=True, index=True,
        help='Cash: project whose funds paid the vendor.\n'
             'Inventory: project that sent the materials.')

    dest_analytic_id = fields.Many2one(
        'account.analytic.account',
        string='Receiving / Beneficiary Project',
        required=True, readonly=True, index=True,
        help='Cash: project whose vendor was paid — it owes the lending project.\n'
             'Inventory: project that received the materials — it owes the sending project.')

    # ── Value ─────────────────────────────────────────────────────────────
    amount = fields.Monetary(
        string='Amount',
        currency_field='currency_id',
        required=True, readonly=True,
        help='Cash: allocation amount paid.\nInventory: total material value (qty × standard_price).')

    currency_id = fields.Many2one(
        'res.currency',
        default=lambda self: self.env.company.currency_id,
        readonly=True)

    # ── Settlement ────────────────────────────────────────────────────────
    state = fields.Selection([
        ('open', 'Outstanding'),
        ('settled', 'Settled'),
    ], string='Status', default='open', tracking=True,
        help='Outstanding = the receiving/beneficiary project has not yet reimbursed the sender.\n'
             'Settled = Finance HO confirmed the balance has been cleared.')

    settlement_date = fields.Date(
        string='Settlement Date', readonly=True, copy=False)

    settlement_note = fields.Text(
        string='Settlement Note', copy=False)

    notes = fields.Text(string='Notes')

    # ── Computed helpers ──────────────────────────────────────────────────
    vendor_id = fields.Many2one(
        related='payment_id.partner_id',
        string='Vendor Paid',
        store=True, readonly=True)

    source_project_id = fields.Many2one(
        'project.project',
        string='Sending Project (record)',
        compute='_compute_project_links', store=True)

    dest_project_id = fields.Many2one(
        'project.project',
        string='Receiving Project (record)',
        compute='_compute_project_links', store=True)

    @api.depends('source_analytic_id', 'dest_analytic_id')
    def _compute_project_links(self):
        Project = self.env['project.project']
        for rec in self:
            rec.source_project_id = Project.search(
                [('x_analytic_account_id', '=', rec.source_analytic_id.id)],
                limit=1) if rec.source_analytic_id else False
            rec.dest_project_id = Project.search(
                [('x_analytic_account_id', '=', rec.dest_analytic_id.id)],
                limit=1) if rec.dest_analytic_id else False

    @api.model_create_multi
    def create(self, vals_list):
        seq = self.env['ir.sequence'].next_by_code('x.interproject.transfer')
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = seq or _('New')
        return super().create(vals_list)

    # ── Actions ───────────────────────────────────────────────────────────

    def action_mark_settled(self):
        """Finance HO marks this inter-project balance as settled/reimbursed."""
        for rec in self:
            if rec.state == 'settled':
                raise UserError(_('This transfer is already settled.'))
            rec.state = 'settled'
            rec.settlement_date = fields.Date.context_today(self)
            rec.message_post(body=_(
                'Marked as settled by %(user)s on %(date)s.'
            ) % {'user': self.env.user.name, 'date': rec.settlement_date})

    def action_reopen(self):
        """Re-open a settlement if it was marked in error."""
        for rec in self:
            if rec.state != 'settled':
                raise UserError(_('Only settled transfers can be re-opened.'))
            rec.state = 'open'
            rec.settlement_date = False
            rec.message_post(body=_(
                'Re-opened by %s.') % self.env.user.name)

    def action_view_payment(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.payment',
            'view_mode': 'form',
            'res_id': self.payment_id.id,
        }

    def action_view_picking(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'view_mode': 'form',
            'res_id': self.picking_id.id,
        }

    def action_view_journal_entry(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': self.move_id.id,
        }
