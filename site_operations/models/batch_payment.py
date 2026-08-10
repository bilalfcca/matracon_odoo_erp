from odoo import models, fields, api, _
from odoo.exceptions import UserError

from . import matracon_notifications as matracon_notify


class BatchPayment(models.Model):
    """Batch Vendor Payment — process multiple vendor payments in one operation.

    Finance HO creates a batch, adds one line per vendor (with banks + tax),
    then clicks "Post All Payments".  Each line creates one account.payment
    and posts it immediately, triggering all existing accounting logic:
    JE, WHT deduction entry, WHT companion payment, BPV reference, analytic
    tagging, etc.
    """
    _name = 'x.batch.payment'
    _description = 'Batch Vendor Payment'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date desc, id desc'
    _rec_name = 'name'

    name = fields.Char(
        string='Batch Reference', readonly=True, copy=False,
        default='New', tracking=True,
    )
    date = fields.Date(
        string='Payment Date', required=True,
        default=fields.Date.today, tracking=True,
    )
    memo = fields.Char(string='Narration / Memo', tracking=True)

    state = fields.Selection([
        ('draft', 'Draft'),
        ('posted', 'Posted'),
        ('cancelled', 'Cancelled'),
    ], string='Status', default='draft', readonly=True, tracking=True)

    # ── CEO Approval (only required for batches NOT created from a liability sheet) ──
    x_ceo_approval_state = fields.Selection([
        ('not_required', 'Not Required'),
        ('pending', 'Pending CEO'),
        ('submitted', 'Awaiting CEO'),
        ('approved', 'CEO Approved'),
    ], string='CEO Approval', default='pending', readonly=True, tracking=True,
       help='CEO must approve directly-created batches before Finance HO can post them.\n'
            'Batches created from a liability sheet are already CEO-approved at the sheet level.')

    x_from_liability_sheet = fields.Boolean(
        string='From Liability Sheet',
        compute='_compute_from_liability_sheet',
        store=True,
        copy=False,
        help='True when this batch was created from a CEO-approved liability sheet.',
    )

    line_ids = fields.One2many(
        'x.batch.payment.line', 'batch_id',
        string='Payment Lines', copy=True,
    )

    # ── Totals ────────────────────────────────────────────────────────────────

    total_gross = fields.Monetary(
        string='Total Gross',
        compute='_compute_totals', store=True,
        currency_field='currency_id',
    )
    total_tax = fields.Monetary(
        string='Total Tax / Deductions',
        compute='_compute_totals', store=True,
        currency_field='currency_id',
    )
    total_net = fields.Monetary(
        string='Total Net Payable',
        compute='_compute_totals', store=True,
        currency_field='currency_id',
    )
    payment_count = fields.Integer(
        string='Payments Created',
        compute='_compute_payment_count', store=False,
    )

    x_destination_project_id = fields.Many2one(
        'account.analytic.account',
        string='Project',
        default=lambda self: self.env.user.sudo().x_default_analytic_account_id,
        help='Apply this project to all payment lines. '
             'Changing it here propagates to every line (overwriting individual values).',
        tracking=True,
    )

    company_id = fields.Many2one(
        'res.company', string='Company',
        default=lambda self: self.env.company, required=True,
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id',
        string='Currency', readonly=True,
    )

    # ── Compute ───────────────────────────────────────────────────────────────

    @api.depends('line_ids.x_liability_sheet_id')
    def _compute_from_liability_sheet(self):
        for batch in self:
            batch.x_from_liability_sheet = bool(
                any(l.x_liability_sheet_id for l in batch.line_ids)
            )

    @api.depends(
        'line_ids.gross_amount',
        'line_ids.x_total_tax',
        'line_ids.x_net_payable',
    )
    def _compute_totals(self):
        for batch in self:
            batch.total_gross = sum(batch.line_ids.mapped('gross_amount'))
            batch.total_tax = sum(batch.line_ids.mapped('x_total_tax'))
            batch.total_net = sum(batch.line_ids.mapped('x_net_payable'))

    def _compute_payment_count(self):
        for batch in self:
            batch.payment_count = len(
                batch.line_ids.filtered(lambda l: l.payment_id)
            )

    @api.onchange('x_destination_project_id')
    def _onchange_project_propagate_to_lines(self):
        """Propagate header project to all existing lines."""
        for line in self.line_ids:
            line.x_destination_project_id = self.x_destination_project_id

    # ── ORM hooks ─────────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') in ('New', False, ''):
                vals['name'] = (
                    self.env['ir.sequence'].next_by_code('x.batch.payment')
                    or 'BATCH/NEW'
                )
        return super().create(vals_list)

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_post(self):
        """Validate and post all lines — creates one account.payment per line."""
        for batch in self:
            if batch.state != 'draft':
                raise UserError(_('Only draft batches can be posted.'))
            if not batch.line_ids:
                raise UserError(
                    _('Add at least one payment line before posting.'))

            # Direct batches (not from a liability sheet) require CEO approval first.
            if not batch.x_from_liability_sheet and batch.x_ceo_approval_state != 'approved':
                raise UserError(_(
                    'CEO approval is required before posting this batch.\n\n'
                    'Please click "Submit to CEO" and wait for the CEO to approve '
                    'the batch before posting.'
                ))

            # Step 1: validate everything up-front so we don't partially post
            for line in batch.line_ids:
                if line.payment_id:
                    continue  # Already posted (re-entrant safety)
                line._validate()

            # Step 2: create + post each payment
            for line in batch.line_ids:
                if line.payment_id:
                    continue
                line._create_and_post_payment()

            batch.state = 'posted'
            matracon_notify.close_activities(batch)
            batch.message_post(
                body=_(
                    'Batch posted. <b>%d</b> payment(s) created.'
                ) % len(batch.line_ids)
            )

    def action_submit_to_ceo(self):
        """Finance HO: submit a direct batch to the CEO for approval.

        Only applicable for batches NOT created from a liability sheet.
        Liability-sheet batches are already CEO-approved at the sheet level.
        """
        for batch in self:
            if batch.x_from_liability_sheet:
                raise UserError(_(
                    'This batch was created from a CEO-approved liability sheet — '
                    'no separate CEO approval is needed. You can post it directly.'
                ))
            if batch.state != 'draft':
                raise UserError(_('Only draft batches can be submitted for approval.'))
            if batch.x_ceo_approval_state == 'approved':
                raise UserError(_('This batch is already CEO-approved.'))
            if not batch.line_ids:
                raise UserError(_(
                    'Add at least one payment line before submitting for approval.'
                ))

            ceo_users = self.env['res.users'].search([
                ('group_ids', 'in', self.env.ref(
                    'purchase_demand_raise.group_ceo_approval').id),
            ])
            total_str = '{:,.2f}'.format(batch.total_net)
            currency_sym = batch.currency_id.symbol or ''
            matracon_notify.notify_users(
                batch,
                ceo_users,
                _('Batch payment <b>%(name)s</b> (%(currency)s %(total)s) '
                  'requires CEO approval before Finance HO can post it. '
                  'Please open the batch and click <b>"Approve (CEO)"</b>.') % {
                    'name': batch.name,
                    'currency': currency_sym,
                    'total': total_str,
                },
                summary=_('Batch Payment CEO Approval'),
            )
            matracon_notify.schedule_activity(
                batch,
                ceo_users,
                _('Approve Batch Payment %s (%s %s)') % (
                    batch.name, currency_sym, total_str),
            )
            batch.x_ceo_approval_state = 'submitted'
            batch.message_post(
                body=_('Batch submitted to CEO for approval by <b>%s</b>.') % self.env.user.name,
            )

    def action_ceo_approve_batch(self):
        """CEO approves the batch — Finance HO can then post it."""
        for batch in self:
            if batch.x_ceo_approval_state not in ('pending', 'submitted'):
                raise UserError(_('This batch is not pending CEO approval.'))
            if batch.state != 'draft':
                raise UserError(_('Only draft batches can be approved.'))

            batch.x_ceo_approval_state = 'approved'
            # Close the CEO activity that was scheduled on submission
            matracon_notify.close_activities(batch, summary_contains='Approve Batch Payment')
            total_str = '{:,.2f}'.format(batch.total_net)
            currency_sym = batch.currency_id.symbol or ''
            batch.message_post(
                body=_('Batch payment <b>%(name)s</b> (%(currency)s %(total)s) '
                       'approved by CEO <b>%(ceo)s</b>. '
                       'Finance HO can now post all payments.') % {
                    'name': batch.name,
                    'currency': currency_sym,
                    'total': total_str,
                    'ceo': self.env.user.name,
                }
            )
            fo_users = self.env['res.users'].search([
                ('group_ids', 'in', self.env.ref(
                    'site_operations.group_finance_ho').id),
            ])
            matracon_notify.notify_users(
                batch,
                fo_users,
                _('CEO approved batch payment <b>%(name)s</b> (%(currency)s %(total)s) — '
                  'please add bank/cheque details and post all payments.') % {
                    'name': batch.name,
                    'currency': currency_sym,
                    'total': total_str,
                },
                summary=_('Batch Payment Approved — Ready to Post'),
            )
            matracon_notify.schedule_activity(
                batch,
                fo_users,
                _('Post batch payment %s') % batch.name,
            )

    def action_ceo_reverse_batch_approval(self):
        """CEO reverses approval — Finance HO must re-submit."""
        for batch in self:
            if batch.x_ceo_approval_state != 'approved':
                raise UserError(_('Only approved batches can have approval reversed.'))
            if batch.state != 'draft':
                raise UserError(_(
                    'Cannot reverse approval on a posted batch. '
                    'Cancel the batch first if needed.'
                ))
            batch.x_ceo_approval_state = 'pending'
            # Close any FO 'Post batch payment' activity that was created on CEO approval
            matracon_notify.close_activities(batch, summary_contains='Post batch payment')
            batch.message_post(
                body=_('CEO reversed approval — batch returned to Pending status. '
                       'Finance HO must re-submit for a fresh approval.')
            )

    def action_cancel(self):
        for batch in self:
            if batch.state == 'posted':
                raise UserError(_(
                    'A posted batch cannot be cancelled here. '
                    'Use "Reverse Batch" to unwind all accounting entries.'
                ))
            batch.state = 'cancelled'
            matracon_notify.close_activities(batch)
            batch.message_post(body=_('Batch cancelled.'))

    def action_matracon_reverse_batch(self):
        """Finance HO: fully reverse a posted batch payment.

        Reverses every individual payment in the batch (including WHT
        companions, tax deduction JEs, auto-clear JEs, and bank entries),
        then cancels the batch.  Each payment returns to Draft state so
        Finance HO can edit and re-post individually if needed.
        """
        _POSTED = frozenset(('in_process', 'paid', 'partial', 'posted'))
        for batch in self:
            user = self.env.user
            if not (user.has_group('site_operations.group_finance_ho')
                    or user._matracon_is_admin()):
                raise UserError(_(
                    'Only Finance HO or Administrator can reverse batches.'
                ))
            if batch.state != 'posted':
                raise UserError(_('Only posted batches can be reversed.'))

            posted_lines = batch.line_ids.filtered(
                lambda l: l.payment_id and l.payment_id.state in _POSTED
            )
            if not posted_lines:
                raise UserError(_(
                    'No posted payments found in this batch to reverse.'
                ))

            reversed_count = 0
            for line in posted_lines:
                line.payment_id.action_matracon_reverse_payment()
                reversed_count += 1

            batch.state = 'cancelled'
            matracon_notify.close_activities(batch)
            batch.message_post(body=_(
                'Batch reversed by <b>%(user)s</b>. '
                '<b>%(count)d</b> payment(s) reversed and reset to Draft. '
                'All journal entries, WHT deductions, and bank entries cancelled.'
            ) % {'user': self.env.user.name, 'count': reversed_count})

    def action_reset_to_draft(self):
        for batch in self:
            if batch.state != 'cancelled':
                raise UserError(_('Only cancelled batches can be reset to Draft.'))
            batch.state = 'draft'

    def action_view_payments(self):
        self.ensure_one()
        payment_ids = self.line_ids.mapped('payment_id').ids
        if not payment_ids:
            raise UserError(_('No payments have been created yet.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Payments — %s') % self.name,
            'res_model': 'account.payment',
            'view_mode': 'list,form',
            'domain': [('id', 'in', payment_ids)],
        }

    def action_print_all_bpvs(self):
        """Download one PDF containing all BPVs for this batch (one per page)."""
        self.ensure_one()
        payment_ids = self.line_ids.mapped('payment_id').ids
        if not payment_ids:
            raise UserError(_(
                'Post the batch first — BPVs are generated when payments are posted.'
            ))
        ids_str = ','.join(str(i) for i in payment_ids)
        return {
            'type': 'ir.actions.act_url',
            'url': '/site_operations/print/account.payment/%s' % ids_str,
            'target': 'new',
        }


class BatchPaymentLine(models.Model):
    """One vendor payment within a batch.

    Carries its own bank allocations and tax (WHT/Retention) lines.
    On batch posting, _create_and_post_payment() materialises each line
    into an account.payment and posts it immediately, triggering all
    existing Matracon accounting hooks (JE, analytic tagging, WHT, BPV ref).
    """
    _name = 'x.batch.payment.line'
    _description = 'Batch Payment Line'
    _order = 'sequence, id'

    batch_id = fields.Many2one(
        'x.batch.payment', string='Batch',
        required=True, ondelete='cascade', index=True,
    )
    sequence = fields.Integer(default=10)

    # ── Default helpers ───────────────────────────────────────────────────────

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        # Pre-fill Destination Project from the parent batch header (if already
        # set), falling back to the current user's default analytic account.
        if 'x_destination_project_id' in fields_list and not res.get('x_destination_project_id'):
            batch_id = res.get('batch_id') or self.env.context.get('default_batch_id')
            if batch_id:
                batch = self.env['x.batch.payment'].browse(batch_id)
                if batch.x_destination_project_id:
                    res['x_destination_project_id'] = batch.x_destination_project_id.id
            if not res.get('x_destination_project_id'):
                user_analytic = self.env.user.sudo().x_default_analytic_account_id
                if user_analytic:
                    res['x_destination_project_id'] = user_analytic.id
        return res

    # ── Core payment fields ───────────────────────────────────────────────────

    partner_id = fields.Many2one(
        'res.partner', string='Vendor', required=True,
        domain="[('active', '=', True)]",
    )
    x_expense_account_id = fields.Many2one(
        'account.account',
        string='Expense / Payable Account',
        help='GL account to debit on the payment JE (clears the vendor payable). '
             'Leave blank to use the vendor\'s default payable account.',
    )
    gross_amount = fields.Monetary(
        string='Gross Amount', required=True,
        currency_field='currency_id',
        help='Total amount owed to the vendor before any tax deductions.',
    )
    x_destination_project_id = fields.Many2one(
        'account.analytic.account',
        string='Destination Project',
        help='Project for which this payment is being made. '
             'Stamped on the JE for analytic tracking.',
    )
    x_allocation_ids = fields.One2many(
        'x.batch.payment.line.project.allocation', 'batch_line_id',
        string='Fund Allocation',
        help='Source project fund pools for this payment. '
             'Copied to the created account.payment on batch post.',
        copy=True,
    )
    memo = fields.Char(
        string='Narration / Memo',
        help='Per-vendor narration. If blank, the batch-level memo is used.',
    )
    x_account_title = fields.Char(
        string='Account Title',
        help='Bank account holder name. Printed on Bank Payment Voucher. '
             'Falls back to vendor name if left blank.',
    )
    x_ipc_id = fields.Many2one(
        'x.subcontractor.ipc', string='IPC Reference',
        help='Link to the Interim Payment Certificate (optional).',
    )

    # ── Bank allocations ─────────────────────────────────────────────────────

    bank_line_ids = fields.One2many(
        'x.batch.payment.line.bank', 'batch_line_id',
        string='Bank Allocations', copy=True,
    )

    # ── Tax / WHT lines ───────────────────────────────────────────────────────

    tax_line_ids = fields.One2many(
        'x.batch.payment.line.tax', 'batch_line_id',
        string='Tax Lines', copy=True,
    )

    # ── Computed totals ───────────────────────────────────────────────────────

    x_total_tax = fields.Monetary(
        string='Total Tax', compute='_compute_tax_totals', store=True,
        currency_field='currency_id',
    )
    x_net_payable = fields.Monetary(
        string='Net Payable', compute='_compute_tax_totals', store=True,
        currency_field='currency_id',
        help='Gross amount minus all tax deductions — this is the amount sent to the bank.',
    )

    # ── Liability sheet link (set when created from CEO approval) ────────────

    x_liability_sheet_id = fields.Many2one(
        'x.liability.sheet', string='Liability Sheet',
        readonly=True, copy=False,
        help='Linked when this batch line was created from a liability sheet approval.',
    )
    x_liability_sheet_line_id = fields.Many2one(
        'x.liability.sheet.line', string='Liability Line',
        readonly=True, copy=False,
    )

    # ── WHT / FBR companion payment fields ───────────────────────────────────
    # When a WHT deduction line is present Finance HO must issue two cheques:
    #   1. Net amount   → Original vendor  (handled by bank_line_ids above)
    #   2. WHT amount   → FBR / Federal Board of Revenue  (fields below)
    # These fields drive the auto-created WHT companion payment on batch post.

    x_has_wht = fields.Boolean(
        'Has WHT Deduction',
        compute='_compute_has_wht', store=True,
        help='True when at least one WHT deduction tax line is present.',
    )
    # Exemption certificate linked to the first WHT deduction line (if any).
    # Computed so the view can display exemption details as a styled card.
    # NOTE: stored=True so Odoo 19 can track reverse dependencies for the
    # x_exc_* related fields below. Uses its own compute method (separate from
    # x_has_wht) to satisfy Odoo 19's consistent-store requirement.
    x_active_exemption_id = fields.Many2one(
        'x.partner.wht.exemption', string='WHT Exemption',
        compute='_compute_active_exemption_id', store=True,
        help='Auto-filled from the WHT tax line when vendor has an exemption certificate.',
    )
    x_exc_tax_year = fields.Char(related='x_active_exemption_id.tax_year', string='Tax Year', store=False)
    x_exc_period_from = fields.Date(related='x_active_exemption_id.period_from', string='Period From', store=False)
    x_exc_period_to = fields.Date(related='x_active_exemption_id.period_to', string='Period To', store=False)
    x_exc_barcode = fields.Char(related='x_active_exemption_id.barcode_number', string='Barcode No.', store=False)
    x_exc_description = fields.Char(related='x_active_exemption_id.description', string='Description', store=False)
    x_exc_rate = fields.Float(related='x_active_exemption_id.rate', string='Rate %', digits=(5, 2), store=False)
    x_exc_period_display = fields.Char(
        string='Period', compute='_compute_exc_period_display', store=False,
        help='Formatted period range for compact display in the payment dialog.',
    )

    @api.depends('x_exc_period_from', 'x_exc_period_to')
    def _compute_exc_period_display(self):
        for line in self:
            frm = line.x_exc_period_from.strftime('%d %b %Y') if line.x_exc_period_from else '—'
            to = line.x_exc_period_to.strftime('%d %b %Y') if line.x_exc_period_to else '—'
            line.x_exc_period_display = '%s – %s' % (frm, to)
    x_fbr_partner_id = fields.Many2one(
        'res.partner', string='FBR Payee',
        help='Payee for the WHT cheque — usually "Federal Board of Revenue (FBR)". '
             'Auto-filled when a WHT line is added.',
    )
    x_fbr_journal_id = fields.Many2one(
        'account.journal', string='Bank / Journal (FBR)',
        domain="[('type', 'in', ('bank', 'cash'))]",
        help='Bank journal from which the FBR WHT cheque will be issued.',
    )
    x_fbr_cheque_leaf_id = fields.Many2one(
        'x.cheque.leaf', string='Cheque No. (FBR)',
        domain="[('bank_journal_id', '=', x_fbr_journal_id), ('state', '=', 'available')]",
        ondelete='set null',
    )
    # Char kept for companion payment creation / BPV; auto-filled from leaf
    x_fbr_cheque_number = fields.Char(string='Cheque No. (FBR ref)')
    x_fbr_account_title = fields.Char(
        string='Account Title (FBR)',
        help='Account holder name on the FBR cheque.',
    )
    x_fbr_expense_account_id = fields.Many2one(
        'account.account', string='WHT Payable Account',
        help='GL account to debit on the FBR payment JE (clears WHT Payable liability). '
             'Auto-filled from the WHT Payable account configured in Chart of Accounts.',
    )
    x_fbr_destination_project_id = fields.Many2one(
        'account.analytic.account',
        string='Tax Source Project',
        help='Project charged for the WHT payment to FBR. '
             'Auto-filled from the vendor payment project but can be changed independently.',
    )
    x_fbr_payment_id = fields.Many2one(
        'account.payment', string='WHT Payment (FBR)',
        readonly=True, copy=False,
        help='Auto-created companion payment to FBR on batch posting.',
    )
    x_fbr_bpv_ref = fields.Char(
        string='FBR BPV Ref',
        related='x_fbr_payment_id.x_bpv_ref', readonly=True, store=False,
    )

    # ── After posting ─────────────────────────────────────────────────────────

    payment_id = fields.Many2one(
        'account.payment', string='Payment',
        readonly=True, copy=False,
    )
    x_bpv_ref = fields.Char(
        string='BPV Ref',
        related='payment_id.x_bpv_ref', readonly=True, store=False,
    )
    payment_state = fields.Selection(
        related='payment_id.state', string='Payment Status',
        readonly=True, store=False,
    )

    # ── Related ───────────────────────────────────────────────────────────────

    currency_id = fields.Many2one(
        related='batch_id.currency_id', string='Currency',
    )

    # ── Compute ───────────────────────────────────────────────────────────────

    @api.depends('tax_line_ids.tax_type', 'tax_line_ids.effect')
    def _compute_has_wht(self):
        """Stored Boolean — True when at least one WHT deduction tax line exists."""
        for line in self:
            line.x_has_wht = any(
                t.tax_type == 'wht' and t.effect == 'deduct'
                for t in line.tax_line_ids
            )

    @api.depends('tax_line_ids.tax_type', 'tax_line_ids.effect', 'tax_line_ids.x_exemption_id')
    def _compute_active_exemption_id(self):
        """Non-stored Many2one — picks the first WHT deduction line with an exemption cert."""
        for line in self:
            wht_lines = [
                t for t in line.tax_line_ids
                if t.tax_type == 'wht' and t.effect == 'deduct'
            ]
            exc_line = next((t for t in wht_lines if t.x_exemption_id), None)
            line.x_active_exemption_id = exc_line.x_exemption_id.id if exc_line else False

    @api.depends(
        'tax_line_ids.amount', 'tax_line_ids.effect', 'gross_amount',
    )
    def _compute_tax_totals(self):
        for line in self:
            deductions = sum(
                t.amount for t in line.tax_line_ids if t.effect == 'deduct'
            )
            additions = sum(
                t.amount for t in line.tax_line_ids if t.effect == 'add'
            )
            line.x_total_tax = deductions
            line.x_net_payable = max(line.gross_amount - deductions + additions, 0.0)

    # ── Onchange helpers ──────────────────────────────────────────────────────

    @api.onchange('partner_id')
    def _onchange_partner_id_wht_exemption(self):
        """When vendor changes, replace WHT exemption tax lines for the new vendor.

        Always clears old exemption-driven lines first (even when new vendor has none).
        Looks for an exemption whose period covers today; falls back to the latest row.
        """
        # ── Step 1: always clear old exemption-driven lines on partner change ──
        to_remove = self.tax_line_ids.filtered(lambda t: t.x_exemption_id)
        self.tax_line_ids -= to_remove

        if not self.partner_id:
            return

        # ── Step 2: find active exemption for new partner ──
        today = fields.Date.today()
        exemption = self.env['x.partner.wht.exemption'].search([
            ('partner_id', '=', self.partner_id.id),
            '|', ('period_from', '=', False), ('period_from', '<=', today),
            '|', ('period_to', '=', False), ('period_to', '>=', today),
        ], order='period_to desc, id desc', limit=1)
        if not exemption:
            # Fallback: latest record regardless of period
            exemption = self.env['x.partner.wht.exemption'].search([
                ('partner_id', '=', self.partner_id.id),
            ], order='period_to desc, id desc', limit=1)
        if not exemption:
            return

        # ── Step 3: add new WHT line from the exemption ──
        self.tax_line_ids = [(0, 0, {
            'tax_type': 'wht',
            'effect': 'deduct',
            'name': exemption.description or ('WHT %.2f%%' % exemption.rate),
            'x_exemption_id': exemption.id,
            'x_exemption_rate': exemption.rate,
            'sequence': 10,
        })]

        # ── Step 4: auto-fill WHT Payable Account from exemption ──
        if exemption.x_wht_payable_account_id and not self.x_fbr_expense_account_id:
            self.x_fbr_expense_account_id = exemption.x_wht_payable_account_id

    @api.onchange('tax_line_ids')
    def _onchange_tax_lines_autofill_fbr(self):
        """When a WHT line is added, pre-fill FBR partner and tax source project."""
        has_wht = any(
            t.tax_type == 'wht' and t.effect == 'deduct'
            for t in self.tax_line_ids
        )
        if not has_wht:
            return
        if not self.x_fbr_partner_id:
            fbr = self.env['res.partner'].search(
                ['|', ('name', 'ilike', 'Federal Board of Revenue'),
                       ('name', 'ilike', 'FBR')],
                limit=1,
            )
            if fbr:
                self.x_fbr_partner_id = fbr
                if not self.x_fbr_account_title:
                    self.x_fbr_account_title = fbr.name
        # Auto-fill tax source project: prefer fund allocation project, fall back to destination
        if not self.x_fbr_destination_project_id:
            alloc_project = (
                self.x_allocation_ids[0].project_analytic_account_id
                if self.x_allocation_ids else False
            )
            self.x_fbr_destination_project_id = alloc_project or self.x_destination_project_id

    @api.onchange('x_destination_project_id')
    def _onchange_vendor_project_sync_fbr(self):
        """Keep tax source project in sync with fund allocation project unless already overridden."""
        if self.x_has_wht and not self.x_fbr_destination_project_id:
            alloc_project = (
                self.x_allocation_ids[0].project_analytic_account_id
                if self.x_allocation_ids else False
            )
            self.x_fbr_destination_project_id = alloc_project or self.x_destination_project_id

    def _compute_net_for_onchange(self):
        """Compute net payable inline — safe to call in onchange context.

        ``t.amount`` is a *stored computed* field on child records. In Odoo's
        onchange context, newly-added virtual child records (e.g. a WHT line
        added by ``_onchange_partner_id_wht_exemption``) may not have their
        stored computed ``amount`` evaluated yet — the value reads as 0.

        We therefore mirror ``x.batch.payment.line.tax._compute_amount`` here,
        computing directly from the raw rate fields, so the correct WHT amount
        is always available immediately after the tax line is added.
        """
        gross = self.gross_amount or 0.0
        deductions = 0.0
        additions = 0.0
        for t in self.tax_line_ids:
            # Inline mirror of _compute_amount on x.batch.payment.line.tax
            if t.x_fixed_amount:
                line_amount = t.x_fixed_amount
            elif t.x_exemption_rate:
                line_amount = gross * (t.x_exemption_rate / 100.0)
            elif t.tax_id and gross:
                line_amount = self._matracon_tax_amount(t.tax_id, gross)
            else:
                # Fallback: use stored value (may be 0 for brand-new virtual lines)
                line_amount = t.amount or 0.0
            if t.effect == 'deduct':
                deductions += line_amount
            else:
                additions += line_amount
        return max(gross - deductions + additions, 0.0)

    @api.onchange('tax_line_ids', 'gross_amount')
    def _onchange_sync_allocations_to_net(self):
        """Auto-scale bank and fund allocations to match the net payable.

        When WHT (or any deduction) changes the amount the vendor actually
        receives, both the bank allocation lines (which bank / how much) and
        the fund allocation lines (which project funds / how much) must total
        the NET amount — not the gross.  If they don't, ``_validate()`` will
        refuse to post with "bank allocation total must equal net payable".

        Scaling strategy:
          • 1 line  → set directly to net.
          • N lines → distribute proportionally, fix rounding on the last line.
        """
        net = self._compute_net_for_onchange()

        def _scale_lines(lines, amount_attr):
            """Scale a list of allocation records to sum to ``net``."""
            current_total = sum(getattr(l, amount_attr) or 0.0 for l in lines)
            if not lines or abs(current_total - net) < 0.01:
                return  # already in sync — nothing to do
            if current_total <= 0:
                # All-zero split: divide evenly
                share = round(net / len(lines), 2)
                for line in lines[:-1]:
                    setattr(line, amount_attr, share)
                setattr(lines[-1], amount_attr,
                        round(net - share * (len(lines) - 1), 2))
                return
            # Proportional rescale — preserves the user's bank/project split ratio
            distributed = 0.0
            for line in lines[:-1]:
                new_val = round(
                    (getattr(line, amount_attr) or 0.0) / current_total * net, 2)
                setattr(line, amount_attr, new_val)
                distributed += new_val
            # Last line absorbs rounding error
            setattr(lines[-1], amount_attr, round(net - distributed, 2))

        _scale_lines(list(self.bank_line_ids), 'allocation_amount')
        _scale_lines(list(self.x_allocation_ids), 'allocation_amount')

    @api.onchange('bank_line_ids')
    def _onchange_bank_lines_autofill(self):
        """Auto-fill amount on newly added (zero-amount) bank allocation lines.

        Scenario: vendor selected → WHT auto-loaded → user enters gross →
        then clicks ＋ to add a bank line.  At that point gross + WHT are
        already set, but ``_onchange_sync_allocations_to_net`` only runs on
        ``tax_line_ids`` / ``gross_amount`` changes — it does nothing when
        there are no existing lines yet.

        Here we detect new blank lines (allocation_amount == 0) and fill
        each one with an equal share of the unallocated net balance so the
        user never has to enter the net figure manually.
        """
        net = self._compute_net_for_onchange()
        all_lines = list(self.bank_line_ids)
        blank = [l for l in all_lines if not (l.allocation_amount or 0.0)]
        if not blank:
            return
        already = sum(
            l.allocation_amount or 0.0
            for l in all_lines
            if (l.allocation_amount or 0.0)
        )
        remaining = max(net - already, 0.0)
        if remaining < 0.01:
            return  # fully allocated — leave the new line at 0
        share = round(remaining / len(blank), 2)
        for line in blank[:-1]:
            line.allocation_amount = share
        blank[-1].allocation_amount = round(
            remaining - share * (len(blank) - 1), 2)

    @api.onchange('x_allocation_ids')
    def _onchange_fund_lines_autofill(self):
        """Auto-fill amount on newly added (zero-amount) fund/project allocation lines.

        Same logic as ``_onchange_bank_lines_autofill`` — see that method's
        docstring.  Operates on ``x_allocation_ids`` (source-project funding
        lines) independently from the bank side.
        """
        net = self._compute_net_for_onchange()
        all_lines = list(self.x_allocation_ids)
        blank = [l for l in all_lines if not (l.allocation_amount or 0.0)]
        if not blank:
            return
        already = sum(
            l.allocation_amount or 0.0
            for l in all_lines
            if (l.allocation_amount or 0.0)
        )
        remaining = max(net - already, 0.0)
        if remaining < 0.01:
            return
        share = round(remaining / len(blank), 2)
        for line in blank[:-1]:
            line.allocation_amount = share
        blank[-1].allocation_amount = round(
            remaining - share * (len(blank) - 1), 2)

    @api.onchange('x_fbr_cheque_leaf_id')
    def _onchange_fbr_cheque_leaf_id(self):
        if self.x_fbr_cheque_leaf_id:
            self.x_fbr_cheque_number = self.x_fbr_cheque_leaf_id.cheque_number
        else:
            self.x_fbr_cheque_number = False

    @api.onchange('x_fbr_journal_id')
    def _onchange_fbr_journal_clear_leaf(self):
        if self.x_fbr_cheque_leaf_id and (
                self.x_fbr_cheque_leaf_id.bank_journal_id != self.x_fbr_journal_id):
            old_leaf = self.x_fbr_cheque_leaf_id
            self.x_fbr_cheque_leaf_id = False
            self.x_fbr_cheque_number = False
            if old_leaf:
                old_leaf.sudo().write({'state': 'available'})

    def action_discard_fbr_leaf(self):
        """Discard the assigned FBR cheque (spoiled/faulty)."""
        self.ensure_one()
        if not self.x_fbr_cheque_leaf_id:
            raise UserError(_('No FBR cheque assigned — nothing to discard.'))
        leaf = self.x_fbr_cheque_leaf_id
        self.write({'x_fbr_cheque_leaf_id': False, 'x_fbr_cheque_number': False})
        leaf.sudo().write({
            'state': 'discarded',
            'discarded_date': fields.Date.today(),
        })
        return False

    def action_view_fbr_payment(self):
        """Open the WHT companion payment to FBR."""
        self.ensure_one()
        if not self.x_fbr_payment_id:
            raise UserError(_('No FBR WHT payment has been created for this line yet.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('WHT Payment — FBR'),
            'res_model': 'account.payment',
            'view_mode': 'form',
            'res_id': self.x_fbr_payment_id.id,
        }

    def action_print_fbr_bpv(self):
        """Print the Bank Payment Voucher for the FBR WHT payment."""
        self.ensure_one()
        if not self.x_fbr_payment_id:
            raise UserError(_(
                'Post the batch first — the FBR BPV is generated on posting.'
            ))
        return {
            'type': 'ir.actions.act_url',
            'url': '/site_operations/print/account.payment/%d' % self.x_fbr_payment_id.id,
            'target': 'new',
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _matracon_tax_amount(self, tax, base_amount):
        """Compute a single tax amount (mirrors account.payment._matracon_tax_amount)."""
        if not tax or base_amount <= 0:
            return 0.0
        res = tax.compute_all(
            base_amount,
            currency=self.currency_id,
            quantity=1.0,
            partner=self.partner_id,
        )
        return abs(sum(t.get('amount', 0.0) for t in res.get('taxes', [])))

    # ── Validation ───────────────────────────────────────────────────────────

    def _validate(self):
        """Raise UserError if this line cannot be posted."""
        self.ensure_one()
        if not self.partner_id:
            raise UserError(_('Every payment line must have a Vendor.'))
        if not self.gross_amount or self.gross_amount <= 0:
            raise UserError(_(
                'Vendor "%s": Gross Amount must be positive.'
            ) % (self.partner_id.name or ''))
        if not self.bank_line_ids:
            raise UserError(_(
                'Vendor "%s": add at least one bank / fund allocation.'
            ) % self.partner_id.name)
        # Bank total must equal net payable (allow tiny rounding slack)
        bank_total = sum(self.bank_line_ids.mapped('allocation_amount'))
        if abs(bank_total - self.x_net_payable) > 0.02:
            raise UserError(_(
                'Vendor "%(vendor)s": bank allocation total '
                '(%(alloc).2f) must equal the net payable (%(net).2f).'
            ) % {
                'vendor': self.partner_id.name,
                'alloc': bank_total,
                'net': self.x_net_payable,
            })
        # WHT present → FBR payment details are required
        wht_lines = self.tax_line_ids.filtered(
            lambda t: t.tax_type == 'wht' and t.effect == 'deduct' and t.amount > 0
        )
        if wht_lines:
            if not self.x_fbr_journal_id:
                raise UserError(_(
                    'Vendor "%s": WHT deduction is present — select a Bank / Journal '
                    'for the FBR payment (Tax / WHT tab → FBR Payment section).'
                ) % self.partner_id.name)
            if not self.x_fbr_expense_account_id:
                raise UserError(_(
                    'Vendor "%s": WHT deduction is present — set the WHT Payable Account '
                    'for the FBR payment (Tax / WHT tab → FBR Payment section).'
                ) % self.partner_id.name)

    # ── Posting ───────────────────────────────────────────────────────────────

    def _create_and_post_payment(self):
        """Create one account.payment for this line and post it immediately.

        Journal Entry (created by action_post via existing hooks):
          Dr  Expense / Payable Account  (x_expense_account_id or vendor default)
              Amount = net_payable  [clears vendor AP for the net settled]
          Cr  Bank Account(s)
              Amount = net_payable  [one line per bank when multiple banks used]

        WHT Deduction Entry (auto by _create_tax_deduction_entries):
          Dr  Vendor AP Account          WHT amount  [further clears gross AP]
          Cr  WHT Payable to FBR         WHT amount

        Total Dr on Vendor AP = net_payable + WHT = gross_amount  ✓
        """
        self.ensure_one()
        if self.payment_id:
            return  # Idempotent — already created

        Payment = self.env['account.payment']
        batch = self.batch_id

        # Primary bank journal comes from the first bank allocation line.
        first_bank = self.bank_line_ids[:1]
        if not first_bank or not first_bank.journal_id:
            raise UserError(_(
                'Cannot determine payment journal for "%s". '
                'Please add at least one bank allocation.'
            ) % self.partner_id.name)
        journal = first_bank.journal_id

        net_amount = self.x_net_payable or self.gross_amount

        # ── Create the payment ────────────────────────────────────────────────
        payment_vals = {
            'payment_type': 'outbound',
            'partner_type': 'supplier',
            'partner_id': self.partner_id.id,
            'amount': net_amount,
            'currency_id': self.currency_id.id,
            'journal_id': journal.id,
            'date': batch.date,
            # Per-vendor memo overrides batch-level memo
            'memo': self.memo or batch.memo or batch.name or '',
            'company_id': batch.company_id.id,
            # Category
            'x_payment_category': 'vendor',
            # CEO approval state:
            #   • Liability sheet line → CEO already approved at sheet level;
            #     _validate_ceo_payment_approval() skips the check when
            #     x_liability_sheet_id is set, so we must NOT force 'approved'
            #     here — let create() leave it as 'not_required'.
            #   • Direct batch line → CEO approved the batch itself before
            #     Finance HO could click "Post All Payments"; stamp 'approved'
            #     so _validate_ceo_payment_approval() passes.
            **({'x_ceo_approval_state': 'approved'} if not self.x_liability_sheet_id else {}),
            # Project / accounting
            'x_destination_project_id': (
                self.x_destination_project_id.id or False
            ),
            'x_allocation_ids': [
                (0, 0, {
                    'project_analytic_account_id': a.project_analytic_account_id.id,
                    'allocation_amount': a.allocation_amount,
                })
                for a in self.x_allocation_ids
            ] or False,
            'x_account_title': self.x_account_title or False,
            'x_expense_account_id': (
                self.x_expense_account_id.id or False
            ),
            # Lock gross so tax lines compute off the correct base
            'x_gross_approved_amount': self.gross_amount,
            # IPC link (optional)
            'x_ipc_id': self.x_ipc_id.id if self.x_ipc_id else False,
            # Liability sheet link — propagated from batch line when created via
            # CEO approval flow; ensures paid_amount on the line is updated on post
            'x_liability_sheet_id': (
                self.x_liability_sheet_id.id if self.x_liability_sheet_id else False
            ),
            'x_liability_sheet_line_id': (
                self.x_liability_sheet_line_id.id
                if self.x_liability_sheet_line_id else False
            ),
        }

        payment = Payment.create(payment_vals)

        # ── Bank allocation lines ─────────────────────────────────────────────
        BankAlloc = self.env['x.payment.bank.allocation']
        for bl in self.bank_line_ids:
            BankAlloc.create({
                'payment_id': payment.id,
                'journal_id': bl.journal_id.id,
                'allocation_amount': bl.allocation_amount,
                'x_cheque_leaf_id': bl.x_cheque_leaf_id.id or False,
                'x_cheque_number': bl.x_cheque_number or False,
                'x_account_title': bl.x_account_title or False,
                'available_balance': bl.available_balance,
            })

        # ── Tax lines ────────────────────────────────────────────────────────
        # Copy computed amounts as fixed amounts so they are stable during posting.
        TaxLine = self.env['x.payment.tax.line']
        for tl in self.tax_line_ids:
            TaxLine.create({
                'payment_id': payment.id,
                'sequence': tl.sequence,
                'name': tl.name or False,
                'tax_type': tl.tax_type,
                'tax_id': tl.tax_id.id if tl.tax_id else False,
                'effect': tl.effect,
                # Stamp the computed amount as a fixed amount so it does not
                # re-derive from the payment base after we set x_gross_approved_amount.
                'x_fixed_amount': tl.amount,
                # Carry exemption tracking fields onto the payment tax line
                # so the BPV report can print exemption certificate details.
                'x_exemption_id': tl.x_exemption_id.id if tl.x_exemption_id else False,
                'x_exemption_rate': tl.x_exemption_rate or 0.0,
            })

        # ── Post the payment (all Matracon hooks fire here) ──────────────────
        # This also fires _create_wht_payment_if_needed() which auto-creates a
        # DRAFT WHT companion to FBR when WHT deduction lines are present.
        payment.action_post()

        # ── Auto-advance main vendor payment to 'paid' ────────────────────
        # Tier 1: reconcile AP debit lines (payment + WHT deduction JE) with
        #   outstanding invoice AP credits → vendor balance cleared in ledger.
        # Tier 2: clear the outstanding-payment transit (Cr) via a synthetic
        #   JE — this is what bank-statement reconciliation normally does.
        # Together they give payment.state = 'paid' immediately after posting
        # without any manual bank-reconciliation step.
        self._matracon_auto_pay_vendor_payment(payment)

        # ── Link back ────────────────────────────────────────────────────────
        self.payment_id = payment.id

        # ── WHT companion to FBR — complete and post ─────────────────────────
        # _create_wht_payment_if_needed() (called inside action_post above) already
        # created a DRAFT payment to FBR.  If the batch line carries FBR details
        # (journal, expense account), we update the companion with those details
        # and post it immediately so Finance HO gets a complete BPV for FBR as well.
        wht_companion = payment.x_wht_payment_id
        if wht_companion and self.x_fbr_journal_id:
            # Stamp FBR-specific fields on the companion (while still draft)
            companion_vals = {
                'journal_id': self.x_fbr_journal_id.id,
            }
            if self.x_fbr_partner_id:
                companion_vals['partner_id'] = self.x_fbr_partner_id.id
                companion_vals['x_payee_id'] = self.x_fbr_partner_id.id
            if self.x_fbr_expense_account_id:
                companion_vals['x_expense_account_id'] = self.x_fbr_expense_account_id.id
            if self.x_fbr_account_title:
                companion_vals['x_account_title'] = self.x_fbr_account_title
            # Use the FBR-specific project if set; prefer fund allocation project; fall back to vendor project
            fbr_project = (
                self.x_fbr_destination_project_id
                or (self.x_allocation_ids[0].project_analytic_account_id if self.x_allocation_ids else False)
                or self.x_destination_project_id
            )
            if fbr_project:
                companion_vals['x_destination_project_id'] = fbr_project.id
            wht_companion.write(companion_vals)

            # Create one bank allocation line carrying the cheque details for BPV
            self.env['x.payment.bank.allocation'].create({
                'payment_id': wht_companion.id,
                'journal_id': self.x_fbr_journal_id.id,
                'allocation_amount': wht_companion.amount,
                'x_cheque_leaf_id': self.x_fbr_cheque_leaf_id.id or False,
                'x_cheque_number': self.x_fbr_cheque_number or False,
                'x_account_title': (
                    self.x_fbr_account_title
                    or (self.x_fbr_partner_id.name if self.x_fbr_partner_id else 'FBR')
                ),
            })
            # Mark FBR leaf's payment for audit trail
            if self.x_fbr_cheque_leaf_id:
                self.x_fbr_cheque_leaf_id.sudo().write({
                    'payment_id': wht_companion.id,
                })

            # Post the WHT companion — BPV ref is auto-assigned inside action_post.
            # The companion has x_origin_payment_id set (auto-approved) and no tax
            # lines, so _create_tax_deduction_entries and _create_wht_payment_if_needed
            # both return early without side effects.
            wht_companion.action_post()
            self.x_fbr_payment_id = wht_companion.id

            # ── Auto-advance FBR companion to 'paid' ─────────────────────────
            # WHT is deducted at source — the FBR cheque goes out at the same
            # time as the vendor cheque.  There is no separate bank-statement
            # event to trigger reconciliation, so we reconcile programmatically.
            #
            # The WHT account (x_expense_account_id, e.g. 420402) has:
            #   Credit from the tax-deduction JE  (liability created)
            #   Debit  from the companion payment JE  (liability cleared)
            #
            # Tier 1: reconcile those two lines directly (works when the account
            #   has reconcile=True — standard for any payable/tax account).
            # Tier 2: if Tier 1 didn't make the payment 'paid', clear the
            #   companion's outstanding-payment transit line via a synthetic JE
            #   (mirrors what bank-statement reconciliation does automatically).
            self._matracon_auto_pay_wht_companion(payment, wht_companion)

    def _matracon_auto_pay_vendor_payment(self, payment):
        """Auto-advance the main vendor payment from 'in_process' to 'paid'.

        Posting a batch creates ``account.payment`` records in state
        ``in_process`` (outstanding bank transit unreconciled). Finance HO
        would otherwise have to manually clear each payment via bank-statement
        reconciliation.  This method does that automatically in two tiers.

        **Tier 1 — Reconcile AP lines with open invoices**
        The payment JE debits the vendor's AP account (reduces payable balance).
        The WHT tax-deduction JE also debits AP for the WHT portion.
        Together they equal the gross bill amount.  We reconcile those Dr AP
        lines against all outstanding Cr AP lines (posted vendor bills / MISC
        JEs) for the same partner.
        • Vendor bills are marked as ``paid`` in Odoo ✓
        • Vendor's AP balance is zeroed for the paid amount ✓
        • Payment itself still ``in_process`` (outstanding transit intact) — fixed by Tier 2.

        **Tier 2 — Clear the outstanding-payment transit**
        ``payment_state = 'paid'`` on the underlying ``account.move`` is
        triggered by the outstanding-payment account line (Cr) being
        reconciled — identical to what bank-statement matching does.  We create
        one clearing JE: ``Dr Outstanding-Payments / Cr Bank`` and immediately
        reconcile it against the companion's Cr transit line.
        • ``payment_state → 'paid'`` → ``payment.state → 'paid'`` ✓
        • Bank account reduced by the net payment amount ✓
        • Outstanding-payments account zeroed ✓

        Both tiers together produce complete, correct accounting — no manual
        steps needed after the batch is posted.
        """
        # ── Tier 1: reconcile all AP Dr lines with outstanding invoice Cr lines ─
        try:
            # Payment's own AP Dr line (net amount)
            ap_dr_lines = payment.move_id.line_ids.filtered(
                lambda l: l.account_id.account_type == 'liability_payable'
                and l.debit > 0 and not l.reconciled
            )
            # WHT / retention deduction JE AP Dr lines (WHT + retention amounts)
            if payment.x_tax_deduction_move_id:
                ap_dr_lines |= payment.x_tax_deduction_move_id.line_ids.filtered(
                    lambda l: l.account_id.account_type == 'liability_payable'
                    and l.debit > 0 and not l.reconciled
                )

            if ap_dr_lines:
                # Find all unreconciled AP credit lines (posted bills / MISC JEs)
                # for this partner in the same payable account(s).
                ap_account_ids = ap_dr_lines.mapped('account_id').ids
                invoice_cr_lines = self.env['account.move.line'].search([
                    ('account_id', 'in', ap_account_ids),
                    ('partner_id', '=', payment.partner_id.id),
                    ('reconciled', '=', False),
                    ('credit', '>', 0),
                    ('move_id.state', '=', 'posted'),
                ], order='date asc')
                if invoice_cr_lines:
                    (ap_dr_lines | invoice_cr_lines).reconcile()
        except Exception:
            pass  # Non-fatal — fall through to Tier 2

        # ── Tier 2: clear the outstanding-payment transit line ────────────────
        # This is what bank-statement reconciliation would do; we do it now.
        payment.invalidate_recordset()
        if payment.state == 'paid':
            return  # Both tiers done — nothing left to do

        try:
            outstanding_account = payment.journal_id.payment_credit_account_id
            if not outstanding_account or not outstanding_account.reconcile:
                return

            outstanding_line = payment.move_id.line_ids.filtered(
                lambda l: l.account_id == outstanding_account
                and not l.reconciled
            )[:1]
            if not outstanding_line:
                return

            bank_account = payment.journal_id.default_account_id
            if not bank_account:
                return

            # Create: Dr Outstanding-Payments / Cr Bank
            clearing_move = self.env['account.move'].sudo().create({
                'move_type': 'entry',
                'journal_id': payment.journal_id.id,
                'date': payment.date,
                'ref': _('Auto-clear outstanding — %s') % payment.name,
                'company_id': payment.company_id.id,
                'line_ids': [
                    (0, 0, {
                        'account_id': outstanding_account.id,
                        'partner_id': payment.partner_id.id or False,
                        'debit': outstanding_line.credit,
                        'credit': 0.0,
                    }),
                    (0, 0, {
                        'account_id': bank_account.id,
                        'partner_id': payment.partner_id.id or False,
                        'debit': 0.0,
                        'credit': outstanding_line.credit,
                    }),
                ],
            })
            clearing_move.action_post()
            clearing_dr = clearing_move.line_ids.filtered(
                lambda l: l.account_id == outstanding_account
            )[:1]
            if clearing_dr:
                (outstanding_line | clearing_dr).reconcile()
        except Exception:
            pass  # Absolute safety — never fail the batch post

    def _matracon_auto_pay_wht_companion(self, payment, wht_companion):
        """Advance the FBR WHT companion payment to 'paid' automatically.

        Called immediately after ``wht_companion.action_post()`` in
        ``_create_and_post_payment()``.  WHT is deducted at source, so no
        bank-statement event will ever arrive to trigger reconciliation.
        We do it in two tiers:

        **Tier 1 — reconcile the WHT account lines**
        The tax-deduction JE credits the WHT account (e.g. 420402).
        The companion payment debits the same account.
        When the account has ``reconcile=True`` (standard for payable accounts)
        reconciling those two lines:
          • zeroes the WHT account balance cleanly
          • marks the companion's payable/receivable line as reconciled
          → Odoo computes ``payment_state = 'paid'`` → ``state = 'paid'`` ✓

        **Tier 2 — clear the outstanding-payment transit line**
        If Tier 1 did not push the companion to 'paid' (e.g. the WHT account
        is an expense-type account that isn't reconcilable), we look for the
        outstanding-payment transit credit on the companion's JE and create a
        balancing debit + bank credit JE — exactly what bank-statement
        reconciliation would do.  This leaves no unmatched transit lines and
        ensures the companion always reaches 'paid' even if the chart of accounts
        is not configured for reconciliation on the WHT account.
        """
        # ── Tier 1: reconcile WHT account Cr (deduction JE) ↔ Dr (companion) ──
        wht_account = wht_companion.x_expense_account_id
        deduction_move = payment.x_tax_deduction_move_id

        if wht_account and deduction_move and wht_account.reconcile:
            deduction_cr = deduction_move.line_ids.filtered(
                lambda l: l.account_id == wht_account
                and l.credit > 0 and not l.reconciled
            )[:1]
            companion_dr = wht_companion.move_id.line_ids.filtered(
                lambda l: l.account_id == wht_account
                and l.debit > 0 and not l.reconciled
            )[:1]
            if deduction_cr and companion_dr:
                try:
                    (deduction_cr | companion_dr).reconcile()
                except Exception:
                    pass  # Non-fatal — fall through to Tier 2

        # ── Tier 2: clear outstanding-payment transit line if still in_process ──
        wht_companion.invalidate_recordset()
        if wht_companion.state == 'paid':
            return  # Tier 1 succeeded

        outstanding_account = wht_companion.journal_id.payment_credit_account_id
        outstanding_line = wht_companion.move_id.line_ids.filtered(
            lambda l: l.account_id == outstanding_account and not l.reconciled
        )[:1]

        if not outstanding_line or not outstanding_account:
            return  # Nothing to clear — nothing we can do
        # Outstanding-payment accounts are reconcilable in standard Odoo setups;
        # if somehow not, skip gracefully.
        if not outstanding_account.reconcile:
            wht_companion.message_post(body=_(
                'WHT payment posted but could not be auto-marked as Paid. '
                'Please reconcile manually, or set "Allow Reconciliation = True" '
                'on account <b>%s</b> and/or account <b>%s</b>.'
            ) % (
                wht_account.display_name if wht_account else '—',
                outstanding_account.display_name,
            ))
            return

        bank_account = wht_companion.journal_id.default_account_id
        if not bank_account:
            return

        try:
            # Create a clearing JE: Dr Outstanding-Payments / Cr Bank
            # This mirrors the bank-statement reconciliation that would
            # normally happen when the FBR cheque clears the bank.
            clearing_move = self.env['account.move'].sudo().create({
                'move_type': 'entry',
                'journal_id': wht_companion.journal_id.id,
                'date': wht_companion.date,
                'ref': _('Auto-clear WHT outstanding — %s') % wht_companion.name,
                'company_id': wht_companion.company_id.id,
                'line_ids': [
                    (0, 0, {
                        'account_id': outstanding_account.id,
                        'partner_id': wht_companion.partner_id.id or False,
                        'debit': outstanding_line.credit,
                        'credit': 0.0,
                    }),
                    (0, 0, {
                        'account_id': bank_account.id,
                        'partner_id': wht_companion.partner_id.id or False,
                        'debit': 0.0,
                        'credit': outstanding_line.credit,
                    }),
                ],
            })
            clearing_move.action_post()
            clearing_dr = clearing_move.line_ids.filtered(
                lambda l: l.account_id == outstanding_account
            )[:1]
            if clearing_dr:
                (outstanding_line | clearing_dr).reconcile()
            wht_companion.message_post(body=_(
                'Outstanding WHT transit auto-cleared via JE <b>%s</b>.'
            ) % clearing_move.name)
        except Exception:
            # Absolute safety net — log and continue; never fail the batch post
            wht_companion.message_post(body=_(
                'WHT payment posted (in process). Could not auto-clear outstanding '
                'transit — please reconcile manually with a bank statement.'
            ))

    def action_view_payment(self):
        self.ensure_one()
        if not self.payment_id:
            raise UserError(_('This line has not been posted yet.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Payment — %s') % self.partner_id.name,
            'res_model': 'account.payment',
            'view_mode': 'form',
            'res_id': self.payment_id.id,
        }

    def action_print_bpv(self):
        """Open the Bank Payment Voucher for the posted payment in a new tab."""
        self.ensure_one()
        if not self.payment_id:
            raise UserError(_(
                'Post the batch first — the BPV is generated on posting.'
            ))
        return {
            'type': 'ir.actions.act_url',
            'url': '/site_operations/print/account.payment/%d' % self.payment_id.id,
            'target': 'new',
        }


class BatchPaymentLineBank(models.Model):
    """Per-bank fund allocation for one batch payment line.

    Mirrors x.payment.bank.allocation but belongs to the batch line (not the
    posted payment).  On batch posting these records are copied into
    x.payment.bank.allocation records linked to the created account.payment.
    """
    _name = 'x.batch.payment.line.bank'
    _description = 'Batch Payment Bank Allocation'
    _order = 'batch_line_id, id'

    batch_line_id = fields.Many2one(
        'x.batch.payment.line', string='Payment Line',
        required=True, ondelete='cascade', index=True,
    )

    journal_id = fields.Many2one(
        'account.journal', string='Bank / Journal',
        required=True,
        domain="[('type', 'in', ('bank', 'cash'))]",
    )

    available_balance = fields.Monetary(
        string='Available Balance',
        compute='_compute_available_balance',
        currency_field='currency_id', store=True,
    )

    allocation_amount = fields.Monetary(
        string='Amount', currency_field='currency_id',
    )

    x_cheque_leaf_id = fields.Many2one(
        'x.cheque.leaf', string='Cheque No.',
        domain="[('bank_journal_id', '=', journal_id), ('state', '=', 'available')]",
        ondelete='set null',
    )
    # Char kept for BPV / legacy; auto-filled from leaf
    x_cheque_number = fields.Char(string='Cheque No. (ref)')
    x_account_title = fields.Char(string='Account Title')

    currency_id = fields.Many2one(
        related='batch_line_id.currency_id', string='Currency',
    )

    @api.depends('journal_id')
    def _compute_available_balance(self):
        for rec in self:
            rec.available_balance = self._get_journal_balance(rec.journal_id)

    @api.onchange('journal_id')
    def _onchange_journal_id(self):
        self.available_balance = self._get_journal_balance(self.journal_id)
        if self.x_cheque_leaf_id and (
                self.x_cheque_leaf_id.bank_journal_id != self.journal_id):
            old_leaf = self.x_cheque_leaf_id
            self.x_cheque_leaf_id = False
            self.x_cheque_number = False
            if old_leaf:
                old_leaf.sudo().write({'state': 'available'})

    @api.onchange('x_cheque_leaf_id')
    def _onchange_cheque_leaf_id(self):
        if self.x_cheque_leaf_id:
            self.x_cheque_number = self.x_cheque_leaf_id.cheque_number
        else:
            self.x_cheque_number = False

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


class BatchPaymentLineTax(models.Model):
    """Tax / WHT deduction line for one batch payment line.

    Mirrors x.payment.tax.line but belongs to the batch line.
    On posting, these are copied as x.payment.tax.line records with
    x_fixed_amount set to the computed amount so they remain stable.
    """
    _name = 'x.batch.payment.line.tax'
    _description = 'Batch Payment Tax Line'
    _order = 'sequence, id'

    batch_line_id = fields.Many2one(
        'x.batch.payment.line', string='Payment Line',
        required=True, ondelete='cascade', index=True,
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(
        string='Description',
        help='Optional label (e.g. "Income Tax u/s 153").',
    )
    tax_type = fields.Selection([
        ('wht', 'Withholding Tax (WHT)'),
        ('retention', 'Retention Money'),
        ('other', 'Other Tax / Deduction'),
    ], string='Tax Type', default='wht', required=True)
    tax_id = fields.Many2one(
        'account.tax', string='Tax',
        domain="[('type_tax_use', '=', 'purchase'), ('active', '=', True)]",
    )
    effect = fields.Selection([
        ('deduct', 'Deducted from gross'),
        ('add', 'Added to gross'),
    ], string='Effect', default='deduct', required=True)
    x_fixed_amount = fields.Monetary(
        string='Fixed Amount', currency_field='currency_id',
        help='When set, overrides the tax-rate computation.',
    )
    x_exemption_id = fields.Many2one(
        'x.partner.wht.exemption', string='WHT Exemption',
        ondelete='set null',
        help='Exemption certificate that drove this WHT rate.',
    )
    x_exemption_rate = fields.Float(
        string='Exemption Rate %',
        digits=(5, 2),
        help='WHT rate from the exemption certificate. '
             'When set, overrides tax_id rate computation. Editable.',
    )
    amount = fields.Monetary(
        string='Amount',
        compute='_compute_amount', store=True,
        currency_field='currency_id',
    )
    currency_id = fields.Many2one(
        related='batch_line_id.currency_id', string='Currency',
    )

    @api.depends(
        'tax_id', 'x_fixed_amount', 'x_exemption_rate',
        'batch_line_id.gross_amount', 'effect',
    )
    def _compute_amount(self):
        for line in self:
            if line.x_fixed_amount:
                line.amount = line.x_fixed_amount
            elif line.x_exemption_rate:
                base = line.batch_line_id.gross_amount or 0.0
                line.amount = base * line.x_exemption_rate / 100.0
            elif line.tax_id and line.batch_line_id.gross_amount:
                line.amount = line.batch_line_id._matracon_tax_amount(
                    line.tax_id, line.batch_line_id.gross_amount,
                )
            else:
                line.amount = 0.0
