"""HO Advance to Subcontractor.

Finance HO creates an advance record when head-office disburses funds directly
to a subcontractor for a specific site project.  The advance is separate from
the Mob Advance field on the IPC — it represents any ad-hoc cash advance given
by HO, charged to the destination project's analytic account.

On the IPC the site accountant sees:
  • Total HO Advance till Previous IPC  (cumulative, historical)
  • Total HO Advance till This IPC       (cumulative, up to this IPC date)
  • New HO Advance this Period           (difference = what needs recovering now)
  • HO Advance Recovery (editable)      → flows into IPC deductions
"""

from odoo import models, fields, api, _
from odoo.exceptions import UserError


class SubcontractorHOAdvance(models.Model):
    _name = 'x.subcontractor.ho.advance'
    _description = 'HO Advance to Subcontractor'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'advance_date desc, id desc'

    name = fields.Char(
        string='Reference', readonly=True, copy=False, default='New')

    subcontractor_id = fields.Many2one(
        'res.partner', string='Subcontractor',
        required=True, tracking=True,
        domain=[('category_id.name', '=', 'Subcontractor')],
    )

    project_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Destination Project',
        required=True, tracking=True,
        help='Site project to which this advance is charged. '
             'The analytic account is set automatically from the project.',
    )

    amount = fields.Monetary(
        string='Advance Amount', required=True, tracking=True,
        currency_field='currency_id',
        help='Amount disbursed by Head Office to the subcontractor.',
    )

    advance_date = fields.Date(
        string='Advance Date', required=True,
        default=fields.Date.today, tracking=True,
        help='Date on which the advance was actually paid/disbursed.',
    )

    payment_id = fields.Many2one(
        'account.payment', string='Linked Payment',
        tracking=True, readonly=True, copy=False,
        help='Vendor payment created from this advance record.',
    )
    payment_state = fields.Selection(
        related='payment_id.state', string='Payment Status', store=False,
    )
    payment_name = fields.Char(
        related='payment_id.name', string='Payment Ref', store=False,
    )

    state = fields.Selection([
        ('draft',     'Draft'),
        ('confirmed', 'Confirmed'),
        ('cancelled', 'Cancelled'),
    ], default='draft', tracking=True, required=True)

    notes = fields.Text(string='Notes / Purpose')

    currency_id = fields.Many2one(
        'res.currency',
        default=lambda self: self.env.company.currency_id,
    )

    # ── IPC back-reference (computed for smart button) ────────────────────────
    ipc_ids = fields.Many2many(
        'x.subcontractor.ipc',
        compute='_compute_ipc_ids',
        string='Related IPCs',
    )
    ipc_count = fields.Integer(compute='_compute_ipc_ids')

    # ─────────────────────────────────────────────────────────────────────────
    # ORM
    # ─────────────────────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        Analytic = self.env['account.analytic.account']
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                site_code = Analytic._matracon_site_code_for_id(
                    vals.get('project_analytic_account_id'))
                vals['name'] = Analytic._matracon_ref_with_site(
                    'x.subcontractor.ho.advance', site_code)
        return super().create(vals_list)

    # ─────────────────────────────────────────────────────────────────────────
    # COMPUTES
    # ─────────────────────────────────────────────────────────────────────────

    def _compute_ipc_ids(self):
        IPC = self.env['x.subcontractor.ipc'].sudo()
        for rec in self:
            ipcs = IPC.search([
                ('subcontractor_id', '=', rec.subcontractor_id.id),
                ('project_analytic_account_id', '=',
                 rec.project_analytic_account_id.id),
                ('state', '!=', 'draft'),
            ])
            rec.ipc_ids = ipcs.ids
            rec.ipc_count = len(ipcs)

    # ─────────────────────────────────────────────────────────────────────────
    # ACTIONS
    # ─────────────────────────────────────────────────────────────────────────

    def action_create_payment(self):
        """Create a pre-filled vendor payment and link it to this advance.
        FO fills in journal / bank details on the payment; CEO approves it
        through the standard payment flow.  The advance auto-confirms when
        the payment is posted.
        """
        self.ensure_one()
        if self.payment_id:
            # Payment already exists — just open it
            return {
                'type': 'ir.actions.act_window',
                'name': _('Vendor Payment'),
                'res_model': 'account.payment',
                'view_mode': 'form',
                'res_id': self.payment_id.id,
                'target': 'current',
            }
        if self.amount <= 0:
            raise UserError(_('Set an advance amount before creating a payment.'))
        if not self.subcontractor_id:
            raise UserError(_('Select a subcontractor first.'))
        if not self.project_analytic_account_id:
            raise UserError(_('Select a destination project first.'))

        payment = self.env['account.payment'].sudo().create({
            'payment_type': 'outbound',
            'partner_type': 'supplier',
            'partner_id': self.subcontractor_id.id,
            'amount': self.amount,
            'date': self.advance_date or fields.Date.today(),
            'x_destination_project_id': self.project_analytic_account_id.id,
            'x_payment_category': 'vendor',
            'x_ceo_approval_state': 'pending',
            'memo': _('HO Advance — %s — %s') % (
                self.subcontractor_id.name, self.name),
        })
        self.payment_id = payment.id
        self.message_post(
            body=_('Vendor payment <b>%s</b> created and linked to this advance.')
            % payment.name
        )
        return {
            'type': 'ir.actions.act_window',
            'name': _('Vendor Payment'),
            'res_model': 'account.payment',
            'view_mode': 'form',
            'res_id': payment.id,
            'target': 'current',
        }

    def action_open_payment(self):
        self.ensure_one()
        if not self.payment_id:
            return
        return {
            'type': 'ir.actions.act_window',
            'name': _('Vendor Payment'),
            'res_model': 'account.payment',
            'view_mode': 'form',
            'res_id': self.payment_id.id,
            'target': 'current',
        }

    def action_confirm(self):
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_('Only draft advances can be confirmed.'))
            if rec.amount <= 0:
                raise UserError(_('Advance amount must be greater than zero.'))
            # Block confirmation until CEO approves the linked payment.
            if (rec.payment_id
                    and rec.payment_id.x_ceo_approval_state == 'pending'):
                raise UserError(_(
                    'CEO approval is required before confirming this advance.\n\n'
                    'Payment "%s" is still awaiting CEO approval. '
                    'Ask the CEO to approve it first.'
                ) % (rec.payment_id.name or _('(draft)')))
            rec.state = 'confirmed'
            rec.message_post(
                body=_('Advance <b>%s</b> confirmed — %s %s to %s.') % (
                    rec.name,
                    rec.currency_id.symbol,
                    f'{rec.amount:,.2f}',
                    rec.subcontractor_id.name,
                ))

    def action_reset_draft(self):
        for rec in self:
            if rec.state == 'cancelled':
                raise UserError(_(
                    'A cancelled advance cannot be reset. Create a new one.'))
            rec.state = 'draft'

    def action_cancel(self):
        for rec in self:
            if rec.state == 'confirmed':
                raise UserError(_(
                    'Reset to draft before cancelling a confirmed advance.'))
            rec.state = 'cancelled'

    def action_view_ipcs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Related IPCs'),
            'res_model': 'x.subcontractor.ipc',
            'view_mode': 'list,form',
            'domain': [
                ('subcontractor_id', '=', self.subcontractor_id.id),
                ('project_analytic_account_id', '=',
                 self.project_analytic_account_id.id),
            ],
        }

    def unlink(self):
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_('Only draft HO Advances can be deleted.'))
        return super().unlink()

    def action_delete_draft(self):
        self.unlink()
        return {'type': 'ir.actions.act_window_close'}
