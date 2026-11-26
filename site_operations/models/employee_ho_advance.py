"""HO Advance to Employee — advances paid by Head Office, separate from Petty Cash.

Flow:
  Draft ──Create Payment──▶ Payment Created ──payment posted──▶ Paid
  * The payment is an HO outbound payment (Finance HO picks the bank/cash
    journal); its counterpart is the Advances-to-Employees account with the
    employee as partner:  Dr Advances to Employees / Cr Bank.  Never Petty Cash.
  * On posting, the employee's Advance Balance increases ONCE (flag
    ``balance_applied``); salary sheets then recover it as today.
  * If the payment is reset to draft / reversed / cancelled the balance is
    reduced again and the advance goes back to Payment Created / Draft.
"""
from markupsafe import Markup

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class EmployeeHOAdvance(models.Model):
    _name = 'x.employee.advance'
    _description = 'HO Advance to Employee'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'advance_date desc, id desc'

    name = fields.Char(string='Reference', readonly=True, copy=False, default=lambda s: _('New'))
    employee_id = fields.Many2one('hr.employee', string='Employee', required=True,
                                  tracking=True, index=True)
    project_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Project', tracking=True,
        compute='_compute_project', store=True, readonly=False, precompute=True,
        help='Defaults to the employee\'s site project; used as the payment\'s project.')
    company_id = fields.Many2one('res.company', required=True, default=lambda s: s.env.company)
    currency_id = fields.Many2one(related='company_id.currency_id')
    amount = fields.Monetary(string='Amount', required=True, tracking=True)
    advance_date = fields.Date(string='Date', required=True, tracking=True,
                               default=fields.Date.context_today)
    purpose = fields.Text(string='Purpose', required=True)
    payment_reference = fields.Char(string='Payment Reference / Cheque No.', tracking=True)
    journal_id = fields.Many2one(
        'account.journal', string='Pay From (Bank / Cash)',
        domain="[('type', 'in', ('bank', 'cash')), ('company_id', '=', company_id)]",
        help='HO bank or cash journal. Can also be chosen on the payment.')
    advance_account_id = fields.Many2one(
        'account.account', string='Advance Account', required=True, tracking=True,
        default=lambda s: s.env.company.x_employee_advance_account_id,
        domain="[('x_is_main', '=', False)]",
        help='Debited by the payment (e.g. Advances to Employees). Default is set on the Company.')
    payment_id = fields.Many2one('account.payment', string='Payment', readonly=True, copy=False)
    payment_state = fields.Selection(related='payment_id.state', string='Payment Status')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('payment', 'Payment Created'),
        ('paid', 'Paid'),
        ('cancelled', 'Cancelled'),
    ], default='draft', required=True, readonly=True, tracking=True, copy=False, index=True)
    balance_applied = fields.Boolean(
        readonly=True, copy=False,
        help='Technical: the amount has been added to the employee\'s Advance Balance.')
    applied_amount = fields.Monetary(readonly=True, copy=False)
    employee_advance_balance = fields.Monetary(
        related='employee_id.x_advance_balance', string='Employee Advance Balance')

    @api.depends('employee_id')
    def _compute_project(self):
        for rec in self:
            if rec.employee_id and not rec.project_analytic_account_id:
                rec.project_analytic_account_id = rec.employee_id.x_project_analytic_account_id

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('x.employee.advance') or _('New')
        return super().create(vals_list)

    def write(self, vals):
        locked = {'employee_id', 'amount', 'advance_account_id', 'company_id'}
        if locked & set(vals) and not self.env.context.get('_emp_adv_internal'):
            if self.filtered(lambda r: r.state != 'draft'):
                raise UserError(_(
                    'Employee, amount and account can only be changed while the advance '
                    'is in Draft. Change the amount on the payment before posting, or '
                    'cancel the payment first.'))
        return super().write(vals)

    def unlink(self):
        if self.filtered(lambda r: r.state != 'draft' or r.payment_id):
            raise UserError(_('Only draft advances without a payment can be deleted.'))
        return super().unlink()

    # ── Actions ──────────────────────────────────────────────────────────────
    def action_create_payment(self):
        self.ensure_one()
        if self.payment_id and self.payment_id.state != 'canceled':
            return self.action_open_payment()   # never a second payment
        if self.state != 'draft':
            raise UserError(_('A payment can only be created for a draft advance.'))
        if self.amount <= 0:
            raise UserError(_('Advance amount must be greater than zero.'))
        if not self.advance_account_id:
            raise UserError(_('Set the Advance Account first.'))
        partner = self.employee_id.sudo()._get_or_create_partner()
        if not partner:
            raise UserError(_('Could not find or create a contact for this employee.'))
        vals = {
            'payment_type': 'outbound',
            'partner_type': 'supplier',
            'partner_id': partner.id,
            'amount': self.amount,
            'date': self.advance_date,
            'x_destination_project_id': self.project_analytic_account_id.id or False,
            'x_payment_category': 'vendor',
            'x_ceo_approval_state': 'approved',   # no CEO approval for employee advances
            'x_expense_account_id': self.advance_account_id.id,
            'x_employee_advance_id': self.id,
            'memo': _('Employee Advance %(ref)s — %(emp)s — %(purpose)s') % {
                'ref': self.name, 'emp': self.employee_id.name,
                'purpose': (self.purpose or '')[:60]},
        }
        if self.journal_id:
            vals['journal_id'] = self.journal_id.id
        if self.payment_reference:
            vals['x_cheque_number'] = self.payment_reference
        payment = self.env['account.payment'].create(vals)
        self.with_context(_emp_adv_internal=True).write({'payment_id': payment.id, 'state': 'payment'})
        self.message_post(body=Markup(_('Payment <b>%s</b> created; post it to pay the advance.'))
                          % (payment.name or _('Draft')))
        return self.action_open_payment()

    def action_open_payment(self):
        self.ensure_one()
        if not self.payment_id:
            return False
        return {
            'type': 'ir.actions.act_window', 'name': _('Payment'),
            'res_model': 'account.payment', 'view_mode': 'form',
            'res_id': self.payment_id.id, 'target': 'current',
        }

    def action_cancel(self):
        for rec in self:
            if rec.state == 'paid' or rec.balance_applied:
                raise UserError(_(
                    'This advance is already paid. Reverse its payment first '
                    '(Finance HO → Reverse Payment), then cancel.'))
            if rec.payment_id and rec.payment_id.state not in ('draft', 'canceled'):
                raise UserError(_('Cancel or reset the linked payment first.'))
            if rec.payment_id and rec.payment_id.state == 'draft':
                rec.payment_id.action_cancel()
            rec.with_context(_emp_adv_internal=True).write({'state': 'cancelled'})

    def action_reset_to_draft(self):
        for rec in self:
            if rec.state != 'cancelled':
                raise UserError(_('Only cancelled advances can be reset to draft.'))
            rec.with_context(_emp_adv_internal=True).write({'state': 'draft', 'payment_id': False})

    def action_delete_draft(self):
        self.unlink()
        return {'type': 'ir.actions.act_window_close'}

    # ── Called from account.payment ──────────────────────────────────────────
    def _on_payment_posted(self, payment):
        """Add the paid amount to the employee's Advance Balance — exactly once."""
        for rec in self.sudo():
            if rec.balance_applied:
                continue
            amount = payment.amount
            emp = rec.employee_id
            before = emp.x_advance_balance or 0.0
            emp.write({'x_advance_balance': before + amount})
            vals = {'state': 'paid', 'balance_applied': True, 'applied_amount': amount}
            if rec.currency_id.compare_amounts(amount, rec.amount):
                vals['amount'] = amount
            rec.with_context(_emp_adv_internal=True).write(vals)
            rec.message_post(body=Markup(_(
                'Payment <b>%(pay)s</b> posted. %(emp)s\'s advance balance: '
                '%(before)s → <b>%(after)s</b>.')) % {
                    'pay': payment.name, 'emp': emp.name,
                    'before': '{:,.2f}'.format(before),
                    'after': '{:,.2f}'.format(emp.x_advance_balance)})

    def _on_payment_unposted(self, payment, cancelled=False):
        """Payment reset to draft / reversed / cancelled: take the amount back off."""
        for rec in self.sudo():
            if rec.balance_applied:
                emp = rec.employee_id
                before = emp.x_advance_balance or 0.0
                emp.write({'x_advance_balance': max(before - rec.applied_amount, 0.0)})
                rec.message_post(body=Markup(_(
                    'Payment <b>%(pay)s</b> %(what)s. %(emp)s\'s advance balance: '
                    '%(before)s → <b>%(after)s</b>.')) % {
                        'pay': payment.name, 'emp': emp.name,
                        'what': _('cancelled') if cancelled else _('reset to draft / reversed'),
                        'before': '{:,.2f}'.format(before),
                        'after': '{:,.2f}'.format(emp.x_advance_balance)})
            vals = {'balance_applied': False, 'applied_amount': 0.0,
                    'state': 'draft' if cancelled else 'payment'}
            if cancelled:
                vals['payment_id'] = False
            rec.with_context(_emp_adv_internal=True).write(vals)


class AccountPaymentEmployeeAdvance(models.Model):
    _inherit = 'account.payment'

    x_employee_advance_id = fields.Many2one(
        'x.employee.advance', string='Employee Advance', readonly=True, copy=False, index=True)

    def action_post(self):
        res = super().action_post()
        for payment in self.filtered(lambda p: p.x_employee_advance_id and p.state not in ('draft', 'canceled')):
            payment.x_employee_advance_id._on_payment_posted(payment)
        return res

    def action_draft(self):
        applied = self.filtered(lambda p: p.x_employee_advance_id.balance_applied)
        res = super().action_draft()
        for payment in applied:
            payment.x_employee_advance_id._on_payment_unposted(payment)
        return res

    def action_cancel(self):
        linked = self.filtered('x_employee_advance_id')
        advances = {p.id: p.x_employee_advance_id for p in linked}
        res = super().action_cancel()
        for payment in linked:
            adv = advances[payment.id]
            if adv.payment_id == payment:
                adv._on_payment_unposted(payment, cancelled=True)
        return res


class HrEmployeeHOAdvance(models.Model):
    _inherit = 'hr.employee'

    x_ho_advance_count = fields.Integer(compute='_compute_ho_advance_count',
                                        groups='site_operations.group_finance_ho,purchase_demand_raise.group_matracon_admin,purchase_demand_raise.group_ceo_approval,purchase_demand_raise.group_head_office')

    def _compute_ho_advance_count(self):
        data = self.env['x.employee.advance'].sudo()._read_group(
            [('employee_id', 'in', self.ids), ('state', '!=', 'cancelled')],
            ['employee_id'], ['__count'])
        counts = {emp.id: n for emp, n in data}
        for emp in self:
            emp.x_ho_advance_count = counts.get(emp.id, 0)

    def action_view_ho_advances(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('HO Advances'),
            'res_model': 'x.employee.advance', 'view_mode': 'list,form',
            'domain': [('employee_id', '=', self.id)],
            'context': {'default_employee_id': self.id},
        }


class ResCompanyEmployeeAdvance(models.Model):
    _inherit = 'res.company'

    x_employee_advance_account_id = fields.Many2one(
        'account.account', string='Default Employee Advance Account',
        help='Default account debited by HO Advances to Employees '
             '(e.g. Advances to Employees / Advances against Salary).')
