from odoo import models, fields, api, _
from odoo.exceptions import UserError


class PaymentProjectAllocation(models.Model):
    _name = 'x.payment.project.allocation'
    _description = 'Payment Fund Allocation by Project'

    payment_id = fields.Many2one(
        'account.payment', ondelete='cascade', required=True)

    project_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Source Project', required=True)

    project_id = fields.Many2one(
        'project.project',
        string='Source Project (App)',
        compute='_compute_project_id',
        store=False,
    )

    allocation_amount = fields.Monetary(
        string='Allocation Amount',
        currency_field='currency_id',
    )

    # store=True so Odoo includes the computed value in onchange responses
    # for virtual (unsaved) child records — without store=True the value is
    # computed lazily and not sent to the client until after save.
    #
    # @api.depends is kept minimal (only the field that actually drives the
    # value) — including payment_id.state / payment_id.payment_type caused
    # the compute to be skipped for new virtual records whose payment_id is
    # a temporary NewId with no resolvable related state field.
    available_balance = fields.Monetary(
        string='Available Balance',
        compute='_compute_available_balance',
        currency_field='currency_id',
        store=True,
    )

    currency_id = fields.Many2one(
        related='payment_id.currency_id',
        string='Currency',
    )

    @api.depends('project_analytic_account_id')
    def _compute_project_id(self):
        Project = self.env['project.project']
        for alloc in self:
            if alloc.project_analytic_account_id:
                alloc.project_id = Project.search(
                    [('x_analytic_account_id', '=',
                      alloc.project_analytic_account_id.id)],
                    limit=1,
                )
            else:
                alloc.project_id = False

    @api.depends('project_analytic_account_id')
    def _compute_available_balance(self):
        Project = self.env['project.project']
        for alloc in self:
            alloc.available_balance = Project.get_available_balance_for_analytic(
                alloc.project_analytic_account_id)

    @api.onchange('project_analytic_account_id')
    def _onchange_project_analytic_account_id(self):
        """Real-time update when project is selected directly in the list row."""
        self.available_balance = self.env['project.project'].get_available_balance_for_analytic(
            self.project_analytic_account_id)


class PaymentBankAllocation(models.Model):
    """Per-bank fund allocation for a vendor payment.

    Mirrors the project fund allocation pattern but for bank/cash journals.
    Finance HO selects multiple source banks (x_source_journal_ids on the
    payment), and this model tracks how much is contributed from each bank.
    """
    _name = 'x.payment.bank.allocation'
    _description = 'Payment Bank Allocation'
    _order = 'payment_id, id'

    payment_id = fields.Many2one(
        'account.payment', ondelete='cascade', required=True, index=True)

    journal_id = fields.Many2one(
        'account.journal',
        string='Bank / Journal',
        required=True,
        domain="[('type', 'in', ('bank', 'cash'))]",
    )

    currency_id = fields.Many2one(
        related='payment_id.currency_id',
        string='Currency',
    )

    # store=True so the value is computed on creation and included in
    # onchange responses for new (virtual) child records.
    available_balance = fields.Monetary(
        string='Available Balance',
        compute='_compute_available_balance',
        currency_field='currency_id',
        store=True,
    )

    allocation_amount = fields.Monetary(
        string='Allocation Amount',
        currency_field='currency_id',
    )

    x_cheque_number = fields.Char(string='Cheque No.')

    x_account_title = fields.Char(
        string='Account Title',
        help='Bank account holder name for this payment (e.g. as written on the cheque). '
             'Auto-fills from vendor name on the voucher if left blank.',
    )

    @api.depends('journal_id')
    def _compute_available_balance(self):
        for alloc in self:
            alloc.available_balance = self._get_journal_balance(alloc.journal_id)

    @api.onchange('journal_id')
    def _onchange_journal_id(self):
        """Real-time update when journal is selected directly in the list row."""
        self.available_balance = self._get_journal_balance(self.journal_id)

    def action_assign_cheque_number(self):
        """Auto-assign the next cheque number from the active series for this bank."""
        self.ensure_one()
        if not self.journal_id:
            raise UserError(_('Select a bank / journal first.'))
        series = self.env['x.cheque.series'].search([
            ('bank_journal_id', '=', self.journal_id.id),
            ('state', '=', 'active'),
        ], limit=1)
        if not series:
            raise UserError(_(
                'No active cheque series found for bank "%s". '
                'Please set one up under Accounting → Configuration → Cheque Series.'
            ) % self.journal_id.name)
        self.x_cheque_number = series.get_next_cheque_number()
        return False

    @api.model
    def _get_journal_balance(self, journal):
        """Return the current posted balance for a bank/cash journal."""
        if not journal or journal.type not in ('bank', 'cash'):
            return 0.0
        lines = self.env['account.move.line'].sudo().search([
            ('journal_id', '=', journal.id),
            ('parent_state', '=', 'posted'),
            ('account_id.account_type', 'not in', [
                'asset_receivable',
                'liability_payable',
                'off_balance',
            ]),
        ])
        return sum(lines.mapped('balance'))
