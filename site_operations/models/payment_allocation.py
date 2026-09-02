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


class JEProjectAllocation(models.Model):
    """Fund allocation lines for a Finance-HO Journal Entry.

    Mirrors x.payment.project.allocation for account.payment — each line
    names one source project (whose fund pool is debited) and the amount it
    contributes to the JE.  When the JE is posted:

    1. Available balance for every source project decreases (tracked via this table).
    2. If source ≠ destination project → an inter-project receivable/payable
       GL entry is automatically created (DR Inter-Project Receivable in source,
       CR Inter-Project Payable in destination).
    """
    _name = 'x.je.project.allocation'
    _description = 'Journal Entry Fund Allocation by Project'
    _order = 'move_id, id'

    move_id = fields.Many2one(
        'account.move', ondelete='cascade', required=True, index=True)

    project_analytic_account_id = fields.Many2one(
        'account.analytic.account',
        string='Source Project',
        required=True,
    )

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

    currency_id = fields.Many2one(
        related='move_id.currency_id',
        string='Currency',
    )

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

    x_cheque_leaf_id = fields.Many2one(
        'x.cheque.leaf', string='Cheque No.',
        domain="[('bank_journal_id', '=', journal_id), ('state', '=', 'available')]",
        ondelete='set null',
    )
    # Kept as Char for BPV printing / legacy compatibility;
    # auto-populated from leaf when a leaf is selected.
    x_cheque_number = fields.Char(string='Cheque No. (ref)')

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
        """Clear leaf when journal changes so domain is re-evaluated."""
        self.available_balance = self._get_journal_balance(self.journal_id)
        if self.x_cheque_leaf_id and (
                self.x_cheque_leaf_id.bank_journal_id != self.journal_id):
            old_leaf = self.x_cheque_leaf_id
            self.x_cheque_leaf_id = False
            self.x_cheque_number = False
            # Release the leaf in the same transaction (onchange only, no DB write yet)
            if old_leaf:
                old_leaf.sudo().write({'state': 'available'})

    @api.onchange('x_cheque_leaf_id')
    def _onchange_cheque_leaf_id(self):
        """Auto-fill the cheque number char from the selected leaf."""
        if self.x_cheque_leaf_id:
            self.x_cheque_number = self.x_cheque_leaf_id.cheque_number
        else:
            self.x_cheque_number = False

    # ── ORM: reserve / release leaf on write/create/unlink ───────────────────

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if rec.x_cheque_leaf_id:
                rec.x_cheque_leaf_id.sudo().write({'state': 'used'})
                if not rec.x_cheque_number:
                    rec.x_cheque_number = rec.x_cheque_leaf_id.cheque_number
        return records

    def write(self, vals):
        if 'x_cheque_leaf_id' in vals:
            # Release old leaves before the write changes the FK
            old_leaves = {rec.id: rec.x_cheque_leaf_id for rec in self}
        res = super().write(vals)
        if 'x_cheque_leaf_id' in vals:
            for rec in self:
                old_leaf = old_leaves.get(rec.id)
                if old_leaf and old_leaf != rec.x_cheque_leaf_id:
                    old_leaf.sudo().write({'state': 'available'})
                if rec.x_cheque_leaf_id:
                    rec.x_cheque_leaf_id.sudo().write({'state': 'used'})
                    if not rec.x_cheque_number:
                        rec.x_cheque_number = rec.x_cheque_leaf_id.cheque_number
        return res

    def unlink(self):
        # Release leaves that are not yet linked to a posted payment
        for rec in self:
            if rec.x_cheque_leaf_id and not rec.x_cheque_leaf_id.payment_id:
                rec.x_cheque_leaf_id.sudo().write({'state': 'available'})
        return super().unlink()

    def action_discard_leaf(self):
        """Discard the assigned cheque (spoiled/faulty) and clear the field."""
        self.ensure_one()
        if not self.x_cheque_leaf_id:
            raise UserError(_('No cheque assigned — nothing to discard.'))
        leaf = self.x_cheque_leaf_id
        self.write({'x_cheque_leaf_id': False, 'x_cheque_number': False})
        leaf.sudo().write({
            'state': 'discarded',
            'discarded_date': fields.Date.today(),
        })
        return False

    @api.model
    def _get_journal_balance(self, journal):
        """Return the current posted balance of the bank GL account for this journal.

        Queries journal.default_account_id (e.g. account 112606 BankIslami) directly
        so the balance matches exactly what the Chart of Accounts shows.

        The previous approach (sum all non-AP/non-payable journal lines) incorrectly
        included outstanding-payment transit account lines, which are also booked in
        the bank journal and carry large negative balances for unreconciled payments —
        producing a wildly different figure from the real bank balance.
        """
        if not journal or journal.type not in ('bank', 'cash'):
            return 0.0
        account = journal.default_account_id
        if not account:
            return 0.0
        lines = self.env['account.move.line'].sudo().search([
            ('account_id', '=', account.id),
            ('parent_state', '=', 'posted'),
        ])
        return sum(lines.mapped('balance'))


class BatchPaymentLineProjectAllocation(models.Model):
    """Fund allocation by source project for a batch payment line.

    Mirrors x.payment.project.allocation but linked to x.batch.payment.line
    instead of account.payment.  On batch post, rows are copied verbatim
    into the created account.payment's x_allocation_ids.
    """
    _name = 'x.batch.payment.line.project.allocation'
    _description = 'Batch Payment Line — Fund Allocation by Project'

    batch_line_id = fields.Many2one(
        'x.batch.payment.line', ondelete='cascade', required=True, index=True)

    project_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Source Project', required=True)

    allocation_amount = fields.Monetary(
        string='Allocation Amount',
        currency_field='currency_id',
    )

    available_balance = fields.Monetary(
        string='Available Balance',
        compute='_compute_available_balance',
        currency_field='currency_id',
        store=True,
    )

    currency_id = fields.Many2one(
        related='batch_line_id.currency_id',
        string='Currency',
    )

    @api.depends('project_analytic_account_id')
    def _compute_available_balance(self):
        Project = self.env['project.project']
        for alloc in self:
            alloc.available_balance = Project.get_available_balance_for_analytic(
                alloc.project_analytic_account_id)

    @api.onchange('project_analytic_account_id')
    def _onchange_project_analytic_account_id(self):
        self.available_balance = self.env['project.project'].get_available_balance_for_analytic(
            self.project_analytic_account_id)
