from odoo import models, fields, api, _


class AccountPaymentSiteOps(models.Model):
    _inherit = 'account.payment'

    x_payment_status = fields.Selection([
        ('draft', 'Draft'),
        ('in_process', 'In Process'),
        ('paid', 'Paid'),
    ], string='Payment Status', default='draft', tracking=True)

    x_destination_project_id = fields.Many2one(
        'account.analytic.account', string='Destination Project', tracking=True)

    x_source_project_ids = fields.Many2many(
        'account.analytic.account',
        'payment_source_project_rel',
        'payment_id', 'project_id',
        string='Source Projects')

    x_liability_sheet_id = fields.Many2one(
        'x.liability.sheet', string='Liability Sheet', tracking=True,
        domain=[('state', '=', 'approved')])

    x_total_liability = fields.Float(
        related='x_liability_sheet_id.total_liability',
        string='Total Liability', readonly=True)

    x_total_approved = fields.Float(
        related='x_liability_sheet_id.total_approved',
        string='Total Approved', readonly=True)

    x_vendor_bank_account_id = fields.Many2one(
        'res.partner.bank', string='Vendor Bank Account',
        domain="[('partner_id', '=', partner_id)]")

    x_allocation_ids = fields.One2many(
        'x.payment.project.allocation', 'payment_id',
        string='Fund Allocation')

    x_available_balance = fields.Float(
        string='Available Bank Balance',
        compute='_compute_available_balance', store=False)

    # ─────────────────────────────────────────────────────────────────────────
    # COMPUTE
    # ─────────────────────────────────────────────────────────────────────────

    @api.depends('journal_id')
    def _compute_available_balance(self):
        for payment in self:
            if payment.journal_id and payment.journal_id.default_account_id:
                # Compute balance from account.move.line for this journal's
                # default account
                domain = [
                    ('account_id', '=',
                     payment.journal_id.default_account_id.id),
                    ('parent_state', '=', 'posted'),
                ]
                lines = self.env['account.move.line'].search(domain)
                balance = sum(lines.mapped('debit')) - sum(lines.mapped('credit'))
                payment.x_available_balance = balance
            else:
                payment.x_available_balance = 0.0

    # ─────────────────────────────────────────────────────────────────────────
    # ACTIONS / WORKFLOW
    # ─────────────────────────────────────────────────────────────────────────

    def action_set_in_process(self):
        self.write({'x_payment_status': 'in_process'})
        self.message_post(body=_('Payment set to In Process.'))

    def action_mark_paid(self):
        self.write({'x_payment_status': 'paid'})
        self.message_post(body=_('Payment marked as Paid.'))
