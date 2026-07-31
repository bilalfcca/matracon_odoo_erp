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
                    'Cancel the individual payments directly if needed.'
                ))
            batch.state = 'cancelled'
            matracon_notify.close_activities(batch)
            batch.message_post(body=_('Batch cancelled.'))

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
    x_active_exemption_id = fields.Many2one(
        'x.partner.wht.exemption', string='WHT Exemption',
        compute='_compute_has_wht', store=False,
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

    @api.depends('tax_line_ids.tax_type', 'tax_line_ids.effect', 'tax_line_ids.x_exemption_id')
    def _compute_has_wht(self):
        for line in self:
            wht_lines = [
                t for t in line.tax_line_ids
                if t.tax_type == 'wht' and t.effect == 'deduct'
            ]
            line.x_has_wht = bool(wht_lines)
            # Pick the first WHT line that carries an exemption certificate
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
        # Auto-fill tax source project from the vendor payment project
        if not self.x_fbr_destination_project_id and self.x_destination_project_id:
            self.x_fbr_destination_project_id = self.x_destination_project_id

    @api.onchange('x_destination_project_id')
    def _onchange_vendor_project_sync_fbr(self):
        """Keep tax source project in sync with vendor project unless already overridden."""
        if self.x_has_wht and not self.x_fbr_destination_project_id:
            self.x_fbr_destination_project_id = self.x_destination_project_id

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
            # Use the FBR-specific project if set; otherwise fall back to vendor project
            fbr_project = self.x_fbr_destination_project_id or self.x_destination_project_id
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
        if not journal or journal.type not in ('bank', 'cash'):
            return 0.0
        lines = self.env['account.move.line'].sudo().search([
            ('journal_id', '=', journal.id),
            ('parent_state', '=', 'posted'),
            ('account_id.account_type', 'not in', [
                'asset_receivable', 'liability_payable', 'off_balance',
            ]),
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
