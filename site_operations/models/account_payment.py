from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import UserError

from . import matracon_notifications as matracon_notify

# Odoo 19 removed the 'posted' state from account.payment.
# Active (non-draft, non-cancelled) payments are now 'in_process', 'paid', or 'partial'.
# Keep 'posted' here as a fallback for any future Odoo version that restores it.
_POSTED_STATES = frozenset(('in_process', 'paid', 'partial', 'posted'))


class AccountPaymentSiteOps(models.Model):
    _inherit = 'account.payment'

    x_payment_status = fields.Selection([
        ('draft', 'Draft'),
        ('in_process', 'In Process'),
        ('paid', 'Paid'),
    ], string='Payment Status', default='draft', tracking=True)

    x_fund_project_id = fields.Many2one(
        'account.analytic.account',
        string='Fund Project',
        tracking=True,
        help='Inbound: project receiving client/HO funds. '
             'Outbound without allocations: project funding the payment.',
    )
    x_fund_project_project_id = fields.Many2one(
        'project.project',
        string='Fund Project (App)',
        compute='_compute_fund_project_project_id',
        store=False,
    )

    x_destination_project_id = fields.Many2one(
        'account.analytic.account',
        string='Destination Project',
        tracking=True,
        help='Project for which this vendor payment is being made.',
    )

    x_source_project_ids = fields.Many2many(
        'account.analytic.account',
        'payment_source_project_rel',
        'payment_id', 'project_id',
        string='Source Projects',
        help='Projects whose fund pools will be debited (see Fund Allocation).',
    )

    x_liability_sheet_id = fields.Many2one(
        'x.liability.sheet', string='Liability Sheet', tracking=True,
        domain=[('state', 'in', ('approved', 'paid'))])

    x_liability_sheet_line_id = fields.Many2one(
        'x.liability.sheet.line', string='Liability Line',
        readonly=True, copy=False)

    x_bpv_ref = fields.Char(
        string='BPV Reference',
        copy=False,
        readonly=True,
        index=True,
        help='Auto-assigned on posting: BPV-YY-MM-XXXXX (sequential per month).',
    )

    x_account_title = fields.Char(
        string='Account Title',
        help='Bank account holder name for this payment. '
             'Used on the Bank Payment Voucher when no per-bank allocation lines are set. '
             'Falls back to the vendor name on the printed voucher if left blank.',
    )

    x_payment_category = fields.Selection([
        ('vendor', 'Vendor / Liability'),
        ('salary', 'Salary'),
        ('petty_cash', 'Petty Cash'),
    ], string='Payment Category', default='vendor', tracking=True)

    x_is_ho_advance = fields.Boolean(
        string='HO Advance to Subcontractor',
        default=False,
        tracking=True,
        help='Tick when this outbound payment is an HO advance disbursed directly '
             'to a subcontractor.  On posting, an advance record is auto-created '
             'and linked to the IPC advance tracker.',
    )

    x_ceo_approval_state = fields.Selection([
        ('not_required', 'Not Required'),
        ('pending', 'Pending CEO'),
        ('submitted', 'Awaiting CEO'),   # FO notified CEO, waiting for approval
        ('approved', 'CEO Approved'),
    ], string='CEO Approval', default='not_required', tracking=True)

    # ── CEO Direct Payment ────────────────────────────────────────────────────
    x_ceo_direct_payment = fields.Boolean(
        string='CEO Direct Payment', default=False,
        help='Direct vendor payment created by CEO — FO completes journal/tax/allocation.')
    x_ceo_submitted = fields.Boolean(
        string='Submitted to FO', default=False,
        help='True once CEO has submitted this payment to Finance HO.')

    # Role flags for view visibility (non-stored, depends on session user)
    x_viewer_is_fo = fields.Boolean(compute='_compute_viewer_role', store=False)
    x_viewer_is_ceo_only = fields.Boolean(compute='_compute_viewer_role', store=False)

    x_salary_sheet_id = fields.Many2one(
        'x.salary.sheet', string='Salary Sheet', readonly=True, copy=False)
    x_petty_cash_request_id = fields.Many2one(
        'x.petty.cash.request', string='Petty Cash Request',
        readonly=True, copy=False)

    x_gross_approved_amount = fields.Monetary(
        string='CEO Approved (Gross)',
        currency_field='currency_id',
        readonly=True,
        help='Locked gross amount approved by CEO on the liability sheet.',
    )

    x_total_liability = fields.Float(
        related='x_liability_sheet_id.total_liability',
        string='Total Liability', readonly=True, digits=(16, 0))

    x_total_approved = fields.Float(
        related='x_liability_sheet_id.total_approved',
        string='Total Approved', readonly=True, digits=(16, 0))

    x_vendor_bank_account_id = fields.Many2one(
        'res.partner.bank', string='Vendor Bank Account',
        domain="[('partner_id', '=', partner_id)]")

    x_expense_account_id = fields.Many2one(
        'account.account',
        string='Expense Account',
        tracking=True,
        help='Optional: override which payable account the payment JE debits. '
             'Leave blank to use the vendor\'s default payable account. '
             'IPC payment tracking is based on the vendor contact — '
             'this field does not affect whether the payment appears in an IPC.',
    )

    x_cheque_number = fields.Char(string='Cheque / Reference No.', tracking=True)

    x_is_cheque_payment = fields.Boolean(
        string='Cheque Payment',
        compute='_compute_x_is_cheque_payment',
        store=False,
    )

    x_wht_tax_id = fields.Many2one(
        'account.tax', string='Withholding Tax (WHT)',
        domain="[('type_tax_use', '=', 'purchase'), ('active', '=', True)]")
    x_retention_tax_id = fields.Many2one(
        'account.tax', string='Retention Money',
        domain="[('type_tax_use', '=', 'purchase'), ('active', '=', True)]")
    x_other_tax_id = fields.Many2one(
        'account.tax', string='Other Tax',
        domain="[('type_tax_use', '=', 'purchase'), ('active', '=', True)]")

    x_tax_line_ids = fields.One2many(
        'x.payment.tax.line', 'payment_id',
        string='Tax Compliance Lines',
        copy=True,
    )

    x_wht_amount = fields.Monetary(
        string='WHT Amount', compute='_compute_tax_amounts', store=True,
        currency_field='currency_id')
    x_retention_amount = fields.Monetary(
        string='Retention Money Amount', compute='_compute_tax_amounts', store=True,
        currency_field='currency_id')
    x_other_tax_amount = fields.Monetary(
        string='Other Tax Amount', compute='_compute_tax_amounts', store=True,
        currency_field='currency_id')
    x_total_tax_amount = fields.Monetary(
        string='Total Taxes', compute='_compute_tax_amounts', store=True,
        currency_field='currency_id')
    x_net_payable = fields.Monetary(
        string='Net Payable', compute='_compute_tax_amounts', store=True,
        currency_field='currency_id')

    x_wht_certificate_ids = fields.One2many(
        'x.wht.certificate', 'payment_id', string='WHT Certificates')
    x_wht_certificate_count = fields.Integer(
        compute='_compute_wht_certificate_count', store=False)

    # WHT companion payment — created automatically when FO sets a
    # liability payment in-process and it carries a WHT deduction line.
    x_wht_payment_id = fields.Many2one(
        'account.payment', string='WHT Payment (FBR)',
        readonly=True, copy=False,
        help='Companion draft payment to FBR for the WHT amount on this payment.')
    x_origin_payment_id = fields.Many2one(
        'account.payment', string='Original Vendor Payment',
        readonly=True, copy=False,
        help='The vendor payment from which this WHT payment was generated.')

    # Separate payee field for WHT companion payments.
    # Kept distinct from partner_id so it is always editable regardless of
    # payment state — FO may need to select the correct FBR account even
    # after the payment is confirmed.  An onchange keeps partner_id in sync.
    x_payee_id = fields.Many2one(
        'res.partner',
        string='Payee',
        domain="['|', ('parent_id', '=', False), ('is_company', '=', True)]",
        tracking=True,
        help='Payee for WHT companion payments (FBR or alternate entity).',
    )

    # Journal entry created to debit vendor AP for WHT + Retention deductions.
    # This reduces the vendor's balance in the partner ledger by the deducted amounts
    # and creates a Retention Payable back to the same subcontractor.
    x_tax_deduction_move_id = fields.Many2one(
        'account.move', string='Tax Deduction Entry',
        readonly=True, copy=False,
        help='Journal entry that clears vendor AP for WHT/Retention deductions.')

    x_ipc_id = fields.Many2one(
        'x.subcontractor.ipc', string='IPC Reference', tracking=True,
        help='Interim Payment Certificate this payment is linked to.')

    x_allocation_ids = fields.One2many(
        'x.payment.project.allocation', 'payment_id',
        string='Fund Allocation')

    # ── Multi-bank source tracking ────────────────────────────────────────────
    x_source_journal_ids = fields.Many2many(
        'account.journal',
        'payment_source_journal_rel',
        'payment_id', 'journal_id',
        string='Source Banks',
        domain="[('type', 'in', ('bank', 'cash'))]",
        help='Bank / cash journals from which this payment is funded. '
             'The first journal is also set as the primary posting journal.',
    )
    x_bank_allocation_ids = fields.One2many(
        'x.payment.bank.allocation', 'payment_id',
        string='Bank Allocations',
    )

    x_available_bank_balance = fields.Float(
        string='Available Bank Balance',
        compute='_compute_available_bank_balance', store=False, digits=(16, 0))

    # ─────────────────────────────────────────────────────────────────────────
    # COMPUTE
    # ─────────────────────────────────────────────────────────────────────────

    def _compute_wht_certificate_count(self):
        for p in self:
            p.x_wht_certificate_count = len(p.x_wht_certificate_ids)

    def action_generate_wht_certificate(self):
        self.ensure_one()
        cert = self.env['x.wht.certificate']._generate_from_payment(self)
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'x.wht.certificate',
            'view_mode': 'form',
            'res_id': cert.id,
        }

    def action_view_wht_certificates(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('WHT Certificates'),
            'res_model': 'x.wht.certificate',
            'view_mode': 'list,form',
            'domain': [('payment_id', '=', self.id)],
        }

    def action_assign_cheque_number(self):
        """Auto-assign next cheque number from the active series for this bank."""
        self.ensure_one()
        if not self.journal_id:
            raise UserError(_('Select a payment journal first.'))
        series = self.env['x.cheque.series'].search([
            ('bank_journal_id', '=', self.journal_id.id),
            ('state', '=', 'active'),
        ], limit=1)
        if not series:
            raise UserError(_(
                'No active cheque series found for bank "%s". '
                'Please set one up.'
            ) % self.journal_id.name)
        self.x_cheque_number = series.get_next_cheque_number()

    @api.depends('x_fund_project_id')
    def _compute_fund_project_project_id(self):
        Project = self.env['project.project']
        for payment in self:
            if payment.x_fund_project_id:
                payment.x_fund_project_project_id = Project.search(
                    [('x_analytic_account_id', '=', payment.x_fund_project_id.id)],
                    limit=1,
                )
            else:
                payment.x_fund_project_project_id = False

    @api.depends('journal_id')
    def _compute_available_bank_balance(self):
        AML = self.env['account.move.line'].sudo()
        for payment in self:
            journal = payment.journal_id
            if not journal or journal.type not in ('bank', 'cash'):
                payment.x_available_bank_balance = 0.0
                continue
            # Sum all posted move lines in this journal that are NOT on
            # AR/AP/off-balance accounts — this gives the true cash/bank balance
            # regardless of which specific GL account the journal is configured with.
            # (In Odoo 19, payment_debit_account_id / payment_credit_account_id
            # were removed; default_account_id may or may not be set.)
            lines = AML.search([
                ('journal_id', '=', journal.id),
                ('parent_state', '=', 'posted'),
                ('account_id.account_type', 'not in', [
                    'asset_receivable',
                    'liability_payable',
                    'off_balance',
                ]),
            ])
            payment.x_available_bank_balance = sum(lines.mapped('balance'))

    @api.depends_context('uid')
    def _compute_viewer_role(self):
        user = self.env.user
        is_fo = (
            user.has_group('site_operations.group_finance_ho')
            or user._matracon_is_admin()
        )
        is_ceo_only = (
            user.has_group('purchase_demand_raise.group_ceo_approval')
            and not is_fo
        )
        for payment in self:
            payment.x_viewer_is_fo = is_fo
            payment.x_viewer_is_ceo_only = is_ceo_only

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        user = self.env.user
        is_ceo_only = (
            user.has_group('purchase_demand_raise.group_ceo_approval')
            and not user.has_group('site_operations.group_finance_ho')
            and not user._matracon_is_admin()
        )
        if is_ceo_only:
            vals['x_ceo_direct_payment'] = True
            vals['payment_type'] = 'outbound'

        # If Odoo resolved a default journal and payment method at open time,
        # switch the method to Checks when available (instead of Manual).
        if (vals.get('payment_type') == 'outbound'
                and vals.get('journal_id')
                and 'payment_method_line_id' in fields_list):
            journal = self.env['account.journal'].browse(vals['journal_id'])
            checks_line = self._get_checks_method_line(journal)
            if checks_line:
                vals['payment_method_line_id'] = checks_line.id

        return vals

    def action_ceo_submit_to_fo(self):
        """CEO submits a direct payment request to Finance HO for processing."""
        for payment in self:
            if payment.x_ceo_submitted:
                continue

            fo_group = self.env.ref('site_operations.group_finance_ho')
            fo_users = self.env['res.users'].sudo().search([('all_group_ids', 'in', fo_group.id)])

            # Post human-readable HTML message and notify FO via chatter
            body = Markup(
                '<b>CEO Direct Payment Request</b><br/>'
                'Vendor: <b>{vendor}</b><br/>'
                'Amount: <b>{currency} {amount}</b><br/>'
                'Project: <b>{project}</b><br/><br/>'
                'Please complete journal, tax compliance, and fund allocation, then post.'
            ).format(
                vendor=payment.partner_id.name or '—',
                amount='{:,.2f}'.format(payment.amount),
                currency=payment.currency_id.name or '',
                project=payment.x_destination_project_id.name or '—',
            )
            payment.message_post(
                body=body,
                partner_ids=fo_users.mapped('partner_id').ids,
                subtype_xmlid='mail.mt_comment',
            )

            # Create an Odoo activity for each FO user
            activity_type = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
            if activity_type and fo_users:
                for user in fo_users:
                    payment.activity_schedule(
                        activity_type_id=activity_type.id,
                        summary=_('Process CEO Direct Payment — %s') % (payment.partner_id.name or ''),
                        note=Markup('<b>{currency} {amount}</b> to <b>{vendor}</b>').format(
                            currency=payment.currency_id.name or '',
                            amount='{:,.2f}'.format(payment.amount),
                            vendor=payment.partner_id.name or '',
                        ),
                        user_id=user.id,
                    )

            payment.x_ceo_submitted = True

    # ── Payment method helpers ────────────────────────────────────────────────

    def _get_checks_method_line(self, journal):
        """Return the Checks (check_printing) outbound method line for *journal*.

        Falls back to the first outbound method line whose name/code contains
        'check' or 'cheque' (case-insensitive).  Returns an empty recordset if
        no such line exists on the journal.
        """
        if not journal:
            return self.env['account.payment.method.line']
        return journal.outbound_payment_method_line_ids.filtered(
            lambda l: (
                'check' in (l.code or '').lower()
                or 'cheque' in (l.code or '').lower()
                or 'check' in (l.name or '').lower()
                or 'cheque' in (l.name or '').lower()
            )
        )[:1]

    @api.depends('payment_method_line_id', 'payment_method_line_id.code', 'payment_method_line_id.name')
    def _compute_x_is_cheque_payment(self):
        for payment in self:
            code = (payment.payment_method_line_id.code or '').lower()
            name = (payment.payment_method_line_id.name or '').lower()
            payment.x_is_cheque_payment = (
                'check' in code or 'cheque' in code
                or 'check' in name or 'cheque' in name
            )

    def _compute_show_require_partner_bank(self):
        """Override: always hide the vendor/customer bank account field.

        Matracon uses x_vendor_bank_account_id (hidden) and routes payments
        via the Bank Fund tab instead. The standard partner_bank_id widget
        (Vendor Bank Account) must never appear on the payment form.
        """
        for payment in self:
            payment.show_partner_bank_account = False
            payment.require_partner_bank_account = False

    def _matracon_tax_amount(self, tax, base_amount):
        if not tax or base_amount <= 0:
            return 0.0
        res = tax.compute_all(
            base_amount,
            currency=self.currency_id,
            quantity=1.0,
            partner=self.partner_id,
        )
        return abs(sum(t.get('amount', 0.0) for t in res.get('taxes', [])))

    @api.depends(
        'x_gross_approved_amount', 'amount',
        'x_tax_line_ids.amount', 'x_tax_line_ids.effect',
        'x_wht_tax_id', 'x_retention_tax_id', 'x_other_tax_id',
    )
    def _compute_tax_amounts(self):
        for payment in self:
            base = payment.x_gross_approved_amount or payment.amount or 0.0
            if payment.x_tax_line_ids:
                deduct = sum(
                    l.amount for l in payment.x_tax_line_ids if l.effect == 'deduct'
                )
                add = sum(
                    l.amount for l in payment.x_tax_line_ids if l.effect == 'add'
                )
                payment.x_wht_amount = sum(
                    l.amount for l in payment.x_tax_line_ids if l.tax_type == 'wht'
                )
                payment.x_retention_amount = sum(
                    l.amount for l in payment.x_tax_line_ids if l.tax_type == 'retention'
                )
                payment.x_other_tax_amount = sum(
                    l.amount for l in payment.x_tax_line_ids if l.tax_type == 'other'
                )
                payment.x_total_tax_amount = deduct
                payment.x_net_payable = max(base - deduct + add, 0.0)
            else:
                wht = payment._matracon_tax_amount(payment.x_wht_tax_id, base)
                retention = payment._matracon_tax_amount(payment.x_retention_tax_id, base)
                other = payment._matracon_tax_amount(payment.x_other_tax_id, base)
                payment.x_wht_amount = wht
                payment.x_retention_amount = retention
                payment.x_other_tax_amount = other
                payment.x_total_tax_amount = wht + retention + other
                payment.x_net_payable = max(base - payment.x_total_tax_amount, 0.0)

    # ─────────────────────────────────────────────────────────────────────────
    # ONCHANGE
    # ─────────────────────────────────────────────────────────────────────────

    @api.onchange('x_source_project_ids')
    def _onchange_source_projects_sync_allocations(self):
        if self.payment_type != 'outbound':
            return
        existing = {
            a.project_analytic_account_id.id: a
            for a in self.x_allocation_ids
            if a.project_analytic_account_id
        }
        lines = []
        for analytic in self.x_source_project_ids:
            if analytic.id in existing:
                lines.append((4, existing[analytic.id].id))
            else:
                lines.append((0, 0, {
                    'project_analytic_account_id': analytic.id,
                    'allocation_amount': 0.0,
                }))
        self.x_allocation_ids = lines

    @api.onchange('x_payee_id')
    def _onchange_payee_sync_partner(self):
        """Keep partner_id in sync with x_payee_id on WHT companion payments."""
        if self.x_origin_payment_id and self.x_payee_id:
            self.partner_id = self.x_payee_id

    @api.onchange('x_bank_allocation_ids')
    def _onchange_bank_allocations_set_journal(self):
        """Auto-set journal_id from the first bank allocation line.

        Since journal_id is hidden from the form (banks are selected via the
        Bank Fund tab rows), we derive the primary posting journal from the
        first allocation. When the journal changes, payment_method_line_id is
        cleared so Odoo's own onchange picks a valid method for the new journal
        — this avoids the "payment method not available" validation error.
        """
        if self.x_bank_allocation_ids:
            first_journal = self.x_bank_allocation_ids[0].journal_id
            if first_journal and first_journal != self.journal_id:
                self.journal_id = first_journal
                # Prefer Checks over Manual; fall back to False so Odoo re-picks.
                self.payment_method_line_id = (
                    self._get_checks_method_line(first_journal) or False
                )
        else:
            if self.journal_id:
                self.journal_id = False
                self.payment_method_line_id = False

    @api.onchange('journal_id')
    def _onchange_journal_prefer_checks_method(self):
        """After Odoo selects the default payment method for the new journal
        (usually Manual), switch to Checks if the journal has that method line.
        The user can still manually switch back to Manual in the form.
        """
        if self.payment_type != 'outbound' or not self.journal_id:
            return
        checks_line = self._get_checks_method_line(self.journal_id)
        if checks_line:
            self.payment_method_line_id = checks_line

    @api.onchange('x_source_journal_ids')
    def _onchange_source_journals_sync_allocations(self):
        """Sync bank allocation lines with the selected source journals.

        NOTE: x_source_journal_ids is hidden from the payment form.
        Banks are added directly as rows in the Bank Fund tab.
        This onchange is kept for backward compatibility / programmatic use only.
        It does NOT auto-set journal_id to avoid invalidating the payment
        method line (which caused: "The selected payment method is not
        available for this payment").
        """
        existing = {
            a.journal_id.id: a
            for a in self.x_bank_allocation_ids
            if a.journal_id
        }
        lines = []
        BankAlloc = self.env['x.payment.bank.allocation']
        for journal in self.x_source_journal_ids:
            if journal.id in existing:
                lines.append((4, existing[journal.id].id))
            else:
                bal = BankAlloc._get_journal_balance(journal)
                lines.append((0, 0, {
                    'journal_id': journal.id,
                    'available_balance': bal,
                    'allocation_amount': 0.0,
                }))
        self.x_bank_allocation_ids = lines
        # journal_id is NOT auto-set here — FO selects it manually on the form.

    @api.onchange('x_liability_sheet_id')
    def _onchange_liability_sheet_project(self):
        if self.x_liability_sheet_id and self.x_liability_sheet_id.project_analytic_account_id:
            self.x_destination_project_id = (
                self.x_liability_sheet_id.project_analytic_account_id.id
            )

    @api.onchange(
        'x_tax_line_ids', 'x_tax_line_ids.tax_id', 'x_tax_line_ids.effect',
        'x_wht_tax_id', 'x_retention_tax_id', 'x_other_tax_id',
        'x_gross_approved_amount',
    )
    def _onchange_taxes_set_net_amount(self):
        if self.x_liability_sheet_line_id and self.x_net_payable:
            self.amount = self.x_net_payable

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('x_liability_sheet_line_id') and not vals.get('x_tax_line_ids'):
                vals['x_tax_line_ids'] = [(0, 0, {
                    'tax_type': 'wht',
                    'effect': 'deduct',
                    'sequence': 10,
                })]
            category = vals.get('x_payment_category', 'vendor')
            # Only auto-set approval state if NOT explicitly provided by caller
            # (e.g. salary sheet already sets 'approved' — don't overwrite it)
            if 'x_ceo_approval_state' not in vals:
                user = self.env.user
                is_ceo = user.has_group('purchase_demand_raise.group_ceo_approval')
                is_fo = user.has_group('site_operations.group_finance_ho')
                is_admin = user._matracon_is_admin()
                if category in ('salary', 'petty_cash'):
                    if is_ceo or is_admin:
                        vals['x_ceo_approval_state'] = 'approved'
                    elif is_fo:
                        vals['x_ceo_approval_state'] = 'pending'
                elif category == 'vendor':
                    # Vendor payments require EXPLICIT CEO approval in all cases
                    # except:
                    #   • CEO direct payment flag → self-authorised by CEO
                    #   • WHT companion (x_origin_payment_id) → auto-created during posting
                    #   • Liability sheet already carries CEO approval at sheet level
                    # NOTE: CEO and Admin creating normal vendor payments must also
                    # click "Approve (CEO)" explicitly — prevents "CEO Approved" badge
                    # from appearing before actual approval action is taken.
                    is_ceo_direct = bool(vals.get('x_ceo_direct_payment'))
                    has_liability_sheet = bool(vals.get('x_liability_sheet_id'))
                    is_wht_companion = bool(vals.get('x_origin_payment_id'))
                    if is_ceo_direct or is_wht_companion:
                        vals['x_ceo_approval_state'] = 'approved'
                    elif not has_liability_sheet:
                        vals['x_ceo_approval_state'] = 'pending'
        payments = super().create(vals_list)
        payments._matracon_fix_salary_ceo_state()
        payments._matracon_notify_ceo_on_payment_create()
        return payments

    def _matracon_notify_ceo_on_payment_create(self):
        ceo_users = self.env['res.users'].search([
            ('group_ids', 'in', self.env.ref(
                'purchase_demand_raise.group_ceo_approval').id),
        ])
        fo_users = self.env['res.users'].search([
            ('group_ids', 'in', self.env.ref(
                'site_operations.group_finance_ho').id),
        ])
        for payment in self.filtered(
            lambda p: p.x_ceo_approval_state == 'pending'
        ):
            matracon_notify.notify_users(
                payment,
                ceo_users,
                _('%(category)s payment <b>%(name)s</b> requires CEO approval.') % {
                    'category': dict(
                        payment._fields['x_payment_category'].selection
                    ).get(payment.x_payment_category, ''),
                    'name': payment.name or _('Draft'),
                },
                summary=_('Payment CEO Approval'),
            )
            matracon_notify.schedule_activity(
                payment,
                ceo_users,
                _('Approve %s payment') % payment.x_payment_category,
            )
        for payment in self.filtered(
            lambda p: p.x_ceo_approval_state == 'approved'
            and p.x_payment_category in ('salary', 'petty_cash')
            and self.env.user.has_group('purchase_demand_raise.group_ceo_approval')
        ):
            matracon_notify.notify_users(
                payment,
                fo_users,
                _('CEO created %s payment <b>%s</b> — ready for Finance HO.')
                % (payment.x_payment_category, payment.name or _('Draft')),
                summary=_('Payment Ready for FO'),
            )

    def action_ceo_approve_payment(self):
        for payment in self:
            if payment.x_ceo_approval_state not in ('pending', 'submitted'):
                raise UserError(_('This payment is not pending CEO approval.'))
            payment.x_ceo_approval_state = 'approved'
            vendor = payment.partner_id.name or '—'
            amount_str = '{:,.2f}'.format(payment.amount)
            currency_sym = payment.currency_id.symbol or ''
            payment.message_post(
                body=_('CEO approved payment to <b>%s</b> (%s %s).') % (
                    vendor, currency_sym, amount_str)
            )
            fo_users = self.env['res.users'].search([
                ('group_ids', 'in', self.env.ref(
                    'site_operations.group_finance_ho').id),
            ])
            matracon_notify.notify_users(
                payment,
                fo_users,
                _('CEO approved payment to <b>%(vendor)s</b> '
                  '(%(currency)s %(amount)s) — please process.') % {
                    'vendor': vendor,
                    'currency': currency_sym,
                    'amount': amount_str,
                },
                summary=_('Payment Approved by CEO'),
            )
            matracon_notify.schedule_activity(
                payment, fo_users,
                _('Process payment — %s (%s %s)') % (vendor, currency_sym, amount_str)
            )

    def action_notify_ceo_for_approval(self):
        """Finance HO: submit/re-notify CEO to approve a pending vendor payment.

        Called from the "Submit to CEO ▶" button (pending state) or the
        "↻ Re-notify CEO" button (submitted state) on the payment form.
        Sends a chatter notification to all CEO-group users and schedules
        an activity, so the CEO sees it in their inbox.
        After the first click the state moves from 'pending' → 'submitted'
        so the primary button disappears and "Re-notify" appears instead.
        """
        self.ensure_one()
        if self.x_ceo_approval_state == 'approved':
            raise UserError(_('This payment is already CEO-approved.'))
        ceo_users = self.env['res.users'].search([
            ('group_ids', 'in', self.env.ref(
                'purchase_demand_raise.group_ceo_approval').id),
        ])
        vendor = self.partner_id.name or '—'
        amount_str = '{:,.2f}'.format(self.amount)
        currency_sym = self.currency_id.symbol or ''
        matracon_notify.notify_users(
            self,
            ceo_users,
            _(
                'Finance HO has submitted a vendor payment to <b>%(vendor)s</b> '
                '(%(currency)s %(amount)s) for your approval. '
                'Please open the payment and click <b>"Approve (CEO)"</b>.'
            ) % {
                'vendor': vendor,
                'currency': currency_sym,
                'amount': amount_str,
            },
            summary=_('Payment Awaiting CEO Approval'),
        )
        matracon_notify.schedule_activity(
            self,
            ceo_users,
            _('Approve vendor payment — %s (%s %s)') % (vendor, currency_sym, amount_str),
        )
        self.message_post(
            body=_('Payment submitted to CEO for approval by <b>%s</b>.')
            % self.env.user.name,
        )
        # Move to 'submitted' so the primary "Submit to CEO" button disappears
        # and a softer "Re-notify CEO" button takes its place.
        if self.x_ceo_approval_state == 'pending':
            self.x_ceo_approval_state = 'submitted'

    def _matracon_ensure_fund_allocations(self):
        """Auto-fill fund allocation from source projects when FO posts payment.

        Handles two cases:
        1. No allocation records at all → create them.
        2. All existing allocations have amount=0 → update them proportionally.
           (This happens when FO adds a source project but leaves amount blank.)
        """
        Allocation = self.env['x.payment.project.allocation']
        for payment in self.filtered(
            lambda p: p.payment_type == 'outbound'
            and p.state in _POSTED_STATES
            and p.x_source_project_ids
        ):
            amount = payment.amount
            projects = payment.x_source_project_ids
            if not projects or amount <= 0:
                continue

            existing = payment.x_allocation_ids
            all_zero = existing and all(
                a.allocation_amount == 0.0 for a in existing
            )

            if existing and not all_zero:
                # Allocations already filled in by FO — respect them.
                continue

            share = round(amount / len(projects), 2)
            allocated = 0.0

            if existing and all_zero:
                # Update the existing zero-amount records in place.
                for idx, alloc in enumerate(existing):
                    alloc_amount = share
                    if idx == len(existing) - 1:
                        alloc_amount = round(amount - allocated, 2)
                    allocated += alloc_amount
                    alloc.allocation_amount = alloc_amount
            else:
                # No records yet — create them.
                lines = []
                for idx, analytic in enumerate(projects):
                    alloc_amount = share
                    if idx == len(projects) - 1:
                        alloc_amount = round(amount - allocated, 2)
                    allocated += alloc_amount
                    lines.append({
                        'payment_id': payment.id,
                        'project_analytic_account_id': analytic.id,
                        'allocation_amount': alloc_amount,
                    })
                Allocation.create(lines)

    def _matracon_invalidate_project_funds(self):
        Project = self.env['project.project']
        analytic_ids = set()
        for payment in self:
            if payment.x_fund_project_id:
                analytic_ids.add(payment.x_fund_project_id.id)
            analytic_ids.update(payment.x_source_project_ids.ids)
            analytic_ids.update(
                payment.x_allocation_ids.mapped('project_analytic_account_id').ids
            )
        if analytic_ids:
            projects = Project.search([
                ('x_analytic_account_id', 'in', list(analytic_ids)),
            ])
            if projects:
                projects.invalidate_recordset([
                    'x_funds_received', 'x_total_spent', 'x_available_balance',
                    'x_total_vendor_liability', 'x_total_sub_liability',
                ])

    # ─────────────────────────────────────────────────────────────────────────
    # VALIDATION & POSTING
    # ─────────────────────────────────────────────────────────────────────────

    def _validate_liability_payment(self):
        for payment in self.filtered(
            lambda p: p.payment_type == 'outbound' and p.x_liability_sheet_line_id
        ):
            if payment.x_gross_approved_amount and payment.amount > (
                payment.x_gross_approved_amount + 0.01
            ):
                raise UserError(_(
                    'Payment amount cannot exceed CEO approved gross amount (%(gross).2f).'
                ) % {'gross': payment.x_gross_approved_amount})
            if not payment.x_destination_project_id:
                raise UserError(_('Destination Project is required.'))
            if not payment.x_source_project_ids:
                raise UserError(_('Select at least one Source Project.'))
            if not payment.journal_id:
                raise UserError(_('Source Bank / Payment Journal is required.'))
            if payment.x_is_cheque_payment and not payment.x_cheque_number:
                raise UserError(_('Cheque / Reference Number is required for cheque payments.'))
            if payment.x_allocation_ids:
                total_alloc = sum(payment.x_allocation_ids.mapped('allocation_amount'))
                # Skip check when all allocations are zero — they will be auto-filled
                # by _matracon_ensure_fund_allocations() after super().action_post().
                if total_alloc > 0.0 and abs(total_alloc - payment.amount) > 0.02:
                    raise UserError(_(
                        'Fund allocation total (%(alloc).2f) must equal net payment '
                        'amount (%(pay).2f).'
                    ) % {'alloc': total_alloc, 'pay': payment.amount})

    def _validate_fund_allocations(self):
        """Allow payments even when project fund balance is zero or negative."""
        return

    def _matracon_create_interproject_entries(self):
        for payment in self.filtered(lambda p: p.state in _POSTED_STATES):
            dest = payment.x_destination_project_id
            if not dest or not payment.x_allocation_ids:
                continue
            ref = _('Payment %s — %s') % (payment.name, payment.partner_id.name)
            moves = self.env['account.move']
            for alloc in payment.x_allocation_ids.filtered(
                lambda a: a.allocation_amount > 0
            ):
                src = alloc.project_analytic_account_id
                if src and src != dest:
                    move = payment._create_interproject_entry(
                        src, dest, alloc.allocation_amount, ref)
                    moves |= move
            if moves:
                payment.x_interproject_move_ids = [(6, 0, moves.ids)]

    def _get_deduction_account(self, xmlid):
        """Safely resolve an account by xmlid, fall back to code search."""
        account = self.env.ref(f'site_operations.{xmlid}', raise_if_not_found=False)
        if account:
            return account
        # Fallback: search by code (handles installs where data wasn't reloaded)
        code_map = {
            'account_wht_payable': '252100',
            'account_retention_payable': '211200',
        }
        code = code_map.get(xmlid)
        if code:
            return self.env['account.account'].search(
                [('code', '=', code), ('company_id', '=', self.company_id.id)], limit=1
            )
        return self.env['account.account']

    def _create_tax_deduction_entries(self):
        """For each WHT / Retention deduction line, post a journal entry that:
        Salary payments are skipped — their WHT is tracked at salary-sheet level
        and certificates are issued per-employee via the sheet's WHT flow.
          - Debits the vendor's AP account (reduces their current payable balance)
          - Credits WHT Payable (for WHT) or Retention Payable (for Retention),
            tagging the vendor as partner on the Retention credit so it appears
            in the partner ledger as a payable back to them.

        This ensures the partner ledger balance reflects the gross settled amount,
        and retention shows as a separate payable to the subcontractor.
        """
        self.ensure_one()
        if self.x_payment_category == 'salary':
            return  # Salary WHT tracked at sheet level; no vendor AP to reduce.
        if self.x_tax_deduction_move_id:
            return  # Already created — idempotent.

        deduction_lines = self.x_tax_line_ids.filtered(
            lambda l: l.effect == 'deduct' and l.amount > 0
            and l.tax_type in ('wht', 'retention')
        )
        if not deduction_lines:
            return

        # Resolve accounts
        wht_account = self._get_deduction_account('account_wht_payable')
        retention_account = self._get_deduction_account('account_retention_payable')
        if not wht_account and not retention_account:
            return  # Nothing to post — accounts not set up

        # Vendor AP account — read from the existing payment JE
        ap_account = self.env['account.account']
        if self.move_id:
            ap_line = self.move_id.line_ids.filtered(
                lambda l: l.account_id.account_type == 'liability_payable'
            )[:1]
            ap_account = ap_line.account_id

        if not ap_account:
            # Fallback to partner's default payable account
            ap_account = self.partner_id.with_company(
                self.company_id).property_account_payable_id

        if not ap_account:
            return

        # Use a general journal for the deduction entry
        journal = self.env['account.journal'].search([
            ('type', '=', 'general'),
            ('company_id', '=', self.company_id.id),
        ], limit=1) or self.journal_id

        line_vals = []
        descriptions = []
        for tl in deduction_lines:
            if tl.tax_type == 'wht':
                credit_account = wht_account
                if not credit_account:
                    continue
                label = _('WHT — %s') % (tl.tax_id.name if tl.tax_id else 'WHT')
                # Dr AP (vendor) — no credit-side partner; WHT is owed to FBR
                line_vals += [
                    {
                        'name': label,
                        'account_id': ap_account.id,
                        'partner_id': self.partner_id.id,
                        'debit': tl.amount,
                        'credit': 0.0,
                        'analytic_distribution': (
                            self._analytic_distribution_for_account(
                                self.x_destination_project_id)
                            if self.x_destination_project_id else {}
                        ),
                    },
                    {
                        'name': label,
                        'account_id': credit_account.id,
                        'partner_id': False,
                        'debit': 0.0,
                        'credit': tl.amount,
                    },
                ]
                descriptions.append(f'WHT {tl.amount:,.2f}')

            elif tl.tax_type == 'retention':
                credit_account = retention_account
                if not credit_account:
                    continue
                label = _('Retention — %s') % self.partner_id.name
                # Dr AP (vendor) — removes from current AP
                # Cr Retention Payable (vendor) — creates new payable back to them
                line_vals += [
                    {
                        'name': label,
                        'account_id': ap_account.id,
                        'partner_id': self.partner_id.id,
                        'debit': tl.amount,
                        'credit': 0.0,
                        'analytic_distribution': (
                            self._analytic_distribution_for_account(
                                self.x_destination_project_id)
                            if self.x_destination_project_id else {}
                        ),
                    },
                    {
                        'name': label,
                        'account_id': credit_account.id,
                        'partner_id': self.partner_id.id,  # tagged to vendor
                        'debit': 0.0,
                        'credit': tl.amount,
                    },
                ]
                descriptions.append(f'Retention {tl.amount:,.2f}')

        if not line_vals:
            return

        ref = _('Tax deductions for %s (%s)') % (
            self.name or '', ', '.join(descriptions)
        )
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'ref': ref,
            'date': self.date or fields.Date.today(),
            'journal_id': journal.id,
            'company_id': self.company_id.id,
            'line_ids': [(0, 0, v) for v in line_vals],
            'origin_payment_id': self.id,
            # Stamp the project so site accountant ir.rule includes this WHT/retention
            # entry in Partner Ledger (reducing the correct AP balance for the vendor).
            'x_project_analytic_account_id': (
                self.x_destination_project_id.id
                or self.x_fund_project_id.id
                or False
            ),
        })
        move.action_post()
        self.x_tax_deduction_move_id = move.id
        self.message_post(body=_(
            'Tax deduction journal entry <b>%s</b> posted: %s.'
        ) % (move.name, ', '.join(descriptions)))

    def _matracon_update_liability_on_post(self):
        for payment in self.filtered(
            lambda p: p.state in _POSTED_STATES and p.x_liability_sheet_line_id
        ):
            line = payment.x_liability_sheet_line_id
            gross_paid = payment.x_gross_approved_amount or payment.amount
            payments = payment.x_liability_sheet_id.payment_ids.filtered(
                lambda p: p.state in _POSTED_STATES
                and p.x_liability_sheet_line_id == line
            )
            line.paid_amount = sum(
                p.x_gross_approved_amount or p.amount for p in payments
            )
            payment.x_payment_status = 'paid'
            if payment.x_liability_sheet_id:
                payment.x_liability_sheet_id.action_finalize_if_fully_paid()

    def _validate_ceo_payment_approval(self):
        for payment in self:
            # Petty cash: CEO approved at request level — skip payment-level check.
            if payment.x_petty_cash_request_id:
                continue
            # Salary: CEO approved at salary sheet level — skip payment-level check.
            if payment.x_salary_sheet_id:
                continue
            # WHT companion payments are auto-created during posting — no approval needed.
            if payment.x_origin_payment_id:
                continue

            if (payment.payment_type == 'outbound'
                    and payment.x_payment_category == 'vendor'):
                # Liability sheet carries its own CEO approval — skip payment-level check.
                if payment.x_liability_sheet_id:
                    continue
                # CEO direct payment — CEO is the initiator, already authorised.
                if payment.x_ceo_direct_payment:
                    continue
                # All other outbound vendor payments MUST be CEO-approved.
                if payment.x_ceo_approval_state != 'approved':
                    raise UserError(_(
                        'CEO approval is required before this payment can be processed.\n\n'
                        'Payment "%s" has not been approved by the CEO. '
                        'The CEO must click "Approve (CEO)" before Finance HO can proceed.'
                    ) % (payment.name or _('Draft')))
            elif payment.x_ceo_approval_state in ('pending', 'submitted'):
                # Catch remaining pending/submitted payments (salary/petty-cash without a sheet link).
                raise UserError(_(
                    'CEO approval is required before posting this payment.\n\n'
                    'Payment "%s" is awaiting CEO approval. '
                    'Ask the CEO to approve it before posting.'
                ) % (payment.name or payment.x_payment_category))

    def _matracon_update_petty_cash_on_post(self):
        for payment in self.filtered(
            lambda p: p.state in _POSTED_STATES and p.x_petty_cash_request_id
        ):
            payment.x_petty_cash_request_id.action_mark_released(payment.amount)

    def _matracon_fix_salary_ceo_state(self):
        """Ensure salary payments linked to approved/paid sheets are always CEO-approved.
        Catches any edge-case where create() set state to pending despite explicit value."""
        for payment in self.filtered(
            lambda p: p.x_salary_sheet_id
            and p.x_salary_sheet_id.state in ('approved', 'paid')
            and p.x_ceo_approval_state == 'pending'
        ):
            payment.x_ceo_approval_state = 'approved'

    def _matracon_update_salary_on_post(self):
        for payment in self.filtered(
            lambda p: p.state in _POSTED_STATES and p.x_salary_sheet_id
        ):
            sheet = payment.x_salary_sheet_id
            if sheet.state != 'paid':
                sheet.state = 'paid'
                sheet.message_post(body=_('Salary payment posted by Finance HO.'))
                # Reduce each employee's advance balance by the amount recovered
                for line in sheet.line_ids:
                    if line.detail_advance > 0 and line.employee_id:
                        emp = line.employee_id.sudo()
                        new_balance = max(
                            (emp.x_advance_balance or 0.0) - line.detail_advance, 0.0)
                        emp.write({'x_advance_balance': new_balance})

    def _matracon_confirm_ho_advance_on_post(self):
        """Auto-create and confirm an x.subcontractor.ho.advance record when
        this payment is posted with x_is_ho_advance = True.

        Also confirms any draft advance that was pre-linked to this payment
        (legacy manual-link path).

        Called from action_post after the normal payment posting hooks.
        No changes to the payment flow — CEO approval on the payment covers
        the advance authorisation.
        """
        self.ensure_one()
        if self.state not in _POSTED_STATES:
            return

        # ── Auto-create advance record from the payment checkbox ────────────
        if (self.x_is_ho_advance
                and self.payment_type == 'outbound'
                and self.partner_id
                and self.x_destination_project_id):
            # Only create if not already linked
            existing = self.env['x.subcontractor.ho.advance'].sudo().search([
                ('payment_id', '=', self.id),
            ], limit=1)
            if not existing:
                self.env['x.subcontractor.ho.advance'].sudo().create({
                    'subcontractor_id': self.partner_id.id,
                    'project_analytic_account_id': self.x_destination_project_id.id,
                    'amount': self.amount,
                    'advance_date': self.date or fields.Date.today(),
                    'payment_id': self.id,
                    'state': 'confirmed',
                    'notes': _('Auto-created from payment %s.') % self.name,
                })

        # ── Confirm any pre-linked draft advance (manual-link path) ─────────
        linked = self.env['x.subcontractor.ho.advance'].sudo().search([
            ('payment_id', '=', self.id),
            ('state', '=', 'draft'),
        ])
        for adv in linked:
            adv.state = 'confirmed'
            adv.message_post(
                body=_('Advance auto-confirmed: linked payment <b>%s</b> posted.')
                % self.name
            )

    def _get_next_bpv_ref(self):
        """Return the next BPV-YY-MM-XXXXX reference for the current month."""
        today = fields.Date.context_today(self)
        yy = today.year % 100
        mm = today.month
        prefix = 'BPV-%02d-%02d-' % (yy, mm)
        self.env.cr.execute(
            """
            SELECT x_bpv_ref FROM account_payment
            WHERE x_bpv_ref LIKE %s
            ORDER BY x_bpv_ref DESC
            LIMIT 1
            """,
            (prefix + '%',),
        )
        row = self.env.cr.fetchone()
        if row:
            try:
                last_seq = int(row[0].split('-')[-1])
            except (ValueError, IndexError):
                last_seq = 0
            next_seq = last_seq + 1
        else:
            next_seq = 1
        return prefix + '%05d' % next_seq

    def action_post(self):
        """Auto-assign cheque number from series before posting if not already set."""
        for payment in self:
            if (payment.x_is_cheque_payment
                    and payment.journal_id
                    and not payment.x_cheque_number):
                series = self.env['x.cheque.series'].sudo().search([
                    ('bank_journal_id', '=', payment.journal_id.id),
                    ('state', '=', 'active'),
                ], limit=1)
                if series:
                    payment.x_cheque_number = series.get_next_cheque_number()
                else:
                    raise UserError(_(
                        'No active cheque series found for bank "%s". '
                        'Please set one up in Configuration → Cheque Series.'
                    ) % payment.journal_id.name)
        # Assign BPV-YY-MM-XXXXX reference for outbound payments being posted now.
        for payment in self.filtered(
            lambda p: p.payment_type == 'outbound' and not p.x_bpv_ref
        ):
            payment.x_bpv_ref = payment._get_next_bpv_ref()

        self._validate_ceo_payment_approval()
        for payment in self.filtered(lambda p: p.x_liability_sheet_line_id):
            payment.amount = payment.x_net_payable or payment.amount
        # Sync x_source_project_ids from Fund Allocation tab lines when the user
        # added allocation rows directly (skipping the now-invisible source project
        # widget).  Without this, the validation below fires even though allocations
        # are present.
        for payment in self.filtered(
            lambda p: p.payment_type == 'outbound'
            and not p.x_source_project_ids
            and p.x_allocation_ids
        ):
            payment.x_source_project_ids = payment.x_allocation_ids.mapped(
                'project_analytic_account_id'
            )
        self._validate_liability_payment()
        self._validate_fund_allocations()
        # In Odoo 19 Enterprise (with 'accountant' module installed),
        # outstanding_account_id is NOT automatically set from the payment method
        # unless the payment method line has a payment_account configured.
        # Without it, _generate_journal_entry() silently skips creation and
        # the payment never appears in the partner ledger or general ledger.
        # We force-set it here so the state transition triggers journal entry creation.
        for payment in self.filtered(lambda p: not p.outstanding_account_id):
            try:
                outstanding = payment._get_outstanding_account(payment.payment_type)
                if outstanding:
                    payment.outstanding_account_id = outstanding.id
            except Exception:
                pass  # If chart-template lookup fails, fall through to journal fallback.
            # Final fallback: use the journal's own bank/cash account.
            # This ensures a JE is always created when posting against a bank/cash
            # journal whose payment-method line has no payment_account_id configured
            # (e.g. "Checks" method without a Cheques-in-Transit account).
            # Produces a direct Dr/Cr entry (no outstanding-transit step), which is
            # correct for Matracon's payment workflow.
            if not payment.outstanding_account_id and payment.journal_id.default_account_id:
                payment.outstanding_account_id = payment.journal_id.default_account_id
        res = super().action_post()
        # Belt-and-suspenders: if any payment is now active but still has no journal
        # entry, generate it now (covers edge cases where write() hook was bypassed).
        no_move = self.filtered(lambda p: p.state in _POSTED_STATES and not p.move_id)
        if no_move:
            # Ensure outstanding_account_id is set before generating the JE —
            # _generate_journal_entry() is a no-op when the field is empty.
            for payment in no_move:
                if not payment.outstanding_account_id and payment.journal_id.default_account_id:
                    payment.outstanding_account_id = payment.journal_id.default_account_id
            no_move.filtered(lambda p: p.outstanding_account_id)._generate_journal_entry()
            no_move.move_id.filtered(lambda m: m.state == 'draft').action_post()
        for payment in self.filtered(lambda p: p.state in _POSTED_STATES):
            payment._matracon_ensure_fund_allocations()
            payment._matracon_tag_payment_move_analytic()
            payment._matracon_create_interproject_entries()
            payment._matracon_update_liability_on_post()
            payment._matracon_update_petty_cash_on_post()
            payment._matracon_update_salary_on_post()
            payment._matracon_invalidate_project_funds()
            payment._create_tax_deduction_entries()
            payment._matracon_confirm_ho_advance_on_post()
            # Create companion WHT payment to FBR for ANY outbound payment that
            # carries a WHT deduction line — vendor, salary, or any other category.
            if payment.payment_type == 'outbound':
                payment._create_wht_payment_if_needed()
        return res

    def _matracon_tag_payment_move_analytic(self):
        self.ensure_one()
        analytic = self.x_destination_project_id or self.x_fund_project_id
        if not analytic or not self.move_id:
            return
        # Stamp x_project_analytic_account_id on the payment journal entry so
        # that the ir.rule for site accountants (which scopes by this field)
        # continues to show the payment in Partner Ledger / Aged reports.
        if not self.move_id.x_project_analytic_account_id:
            self.move_id.sudo().write(
                {'x_project_analytic_account_id': analytic.id}
            )
        dist = self._analytic_distribution_for_account(analytic)
        lines = self.move_id.line_ids.filtered(
            lambda l: l.account_id.account_type in (
                'liability_payable', 'expense', 'expense_direct_cost',
                'asset_receivable',
            )
        )
        if lines:
            lines.write({'analytic_distribution': dist})

    def _prepare_move_line_default_vals(self, write_off_line_vals=None, force_balance=None):
        line_vals_list = super()._prepare_move_line_default_vals(
            write_off_line_vals=write_off_line_vals,
            force_balance=force_balance,
        )

        # ── Petty cash replenishment: always force the petty cash (Cash in Hand) account ──
        # destination_account_id is a computed stored field — it gets re-computed to
        # the default AP account whenever Finance HO edits the payment after creation
        # (e.g. changes journal, payment method, or bank allocations).
        # Setting it in action_release() is therefore NOT sufficient.  We override the
        # JE counterpart line here, right before Odoo finalises the journal entry, so
        # the correct Cash-in-Hand account is ALWAYS used regardless of how many times
        # the payment was edited before being posted.
        if self.x_petty_cash_request_id and self.payment_type == 'outbound':
            pc_account = self.x_petty_cash_request_id.fund_id._get_petty_cash_account()
            if pc_account:
                # Identify the bank/outstanding side by account ID so we can leave it
                # untouched and fix only the counterpart (destination) line.
                bank_account_id = (
                    self.outstanding_account_id.id
                    if self.outstanding_account_id
                    else (self.journal_id.default_account_id.id if self.journal_id else None)
                )
                for vals in line_vals_list:
                    if (vals.get('account_id') != bank_account_id
                            and vals.get('account_id') != pc_account.id):
                        vals['account_id'] = pc_account.id
                        break  # Only the first counterpart line

        # ── Substitute manually-chosen expense account on the payable/receivable line ──
        # When x_expense_account_id is set, override the auto-computed counterpart so
        # the payment JE hits the selected account (e.g. "Payable to Subcontractors"
        # instead of the generic payable). This makes the entry appear in the IPC
        # "Payments Made" GL query automatically for any subcontractor payment.
        if self.x_expense_account_id and self.payment_type == 'outbound':
            for vals in line_vals_list:
                acct = self.env['account.account'].browse(vals.get('account_id'))
                if acct.account_type in ('liability_payable', 'asset_receivable'):
                    vals['account_id'] = self.x_expense_account_id.id
                    break  # Only the first payable/receivable line (the counterpart)

        # ── Multi-bank split: one Cr line per bank allocation ──────────────────
        # Standard Odoo produces a single bank credit line keyed to journal_id
        # (which is auto-set to the first bank in the Bank Fund tab).  When the
        # user has split the payment across multiple banks, we must replace that
        # single line with N credit lines — one per bank allocation — so the JE
        # accurately reflects each bank's contribution.
        #
        # E.g.:  Dr AP/Petty Cash  22,334,455
        #        Cr JS Bank  4397   7,891,203   ← was: Cr JS Bank 22,334,455
        #        Cr Soneri   2458   6,402,118   ← new
        #        Cr Alfalah  0040   8,041,134   ← new
        if self.payment_type == 'outbound' and len(self.x_bank_allocation_ids) > 1:
            allocs = self.x_bank_allocation_ids.filtered(
                lambda a: a.allocation_amount > 0 and a.journal_id.default_account_id
            )
            if len(allocs) > 1:
                # The bank-side line account equals outstanding_account_id when set
                # (we force-set it in action_post to journal.default_account_id).
                bank_acct_id = (
                    self.outstanding_account_id.id
                    if self.outstanding_account_id
                    else (
                        self.journal_id.default_account_id.id
                        if self.journal_id
                        else None
                    )
                )
                # Odoo 19 line vals use 'balance' (negative = credit for outbound).
                # There is no 'credit'/'debit' key — checking credit > 0 always fails.
                # The bank/liquidity line has account_id == outstanding_account_id and
                # balance < 0 (money going out).
                bank_line_idx = next(
                    (
                        i for i, v in enumerate(line_vals_list)
                        if v.get('account_id') == bank_acct_id
                        and (v.get('balance') or 0) < 0
                    ),
                    None,
                )
                if bank_line_idx is not None:
                    base = line_vals_list.pop(bank_line_idx)
                    # Convert alloc amounts to company currency (PKR = same here).
                    # balance is negative for outbound (credit side).
                    for alloc in allocs:
                        split = dict(base)
                        split['account_id'] = alloc.journal_id.default_account_id.id
                        split['balance'] = -alloc.allocation_amount
                        split['amount_currency'] = -alloc.allocation_amount
                        # Remove stale debit/credit keys if any ancestor set them.
                        split.pop('debit', None)
                        split.pop('credit', None)
                        # Keep the original label (e.g. "Manual Payment") but
                        # append the bank journal name so each line is identifiable.
                        base_name = base.get('name') or ''
                        split['name'] = (
                            '%s - %s' % (base_name, alloc.journal_id.name)
                            if base_name
                            else alloc.journal_id.name
                        )
                        line_vals_list.append(split)

        analytic = None
        if self.payment_type == 'inbound':
            analytic = self.x_fund_project_id
        elif self.payment_type == 'outbound':
            analytic = self.x_destination_project_id or self.x_fund_project_id
        if not analytic:
            return line_vals_list
        dist = self._analytic_distribution_for_account(analytic)
        for vals in line_vals_list:
            account = self.env['account.account'].browse(vals.get('account_id'))
            if account.account_type in (
                'liability_payable', 'expense', 'expense_direct_cost',
                'asset_receivable',
            ):
                vals['analytic_distribution'] = dist
        return line_vals_list

    def action_print_cheque(self):
        return self.env.ref(
            'site_operations.action_report_cheque').report_action(self)

    def action_direct_print_bpv(self):
        """Open the Bank Payment Voucher in a new tab and auto-trigger print dialog."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'url': f'/site_operations/print/account.payment/{self.id}',
            'target': 'new',
        }

    def action_set_in_process(self):
        # Post to accounting (creates journal entry, tags analytic, updates liability).
        # WHT companion payment is created inside action_post() for all outbound payments.
        draft = self.filtered(lambda p: p.state == 'draft')
        if draft:
            draft.action_post()
        self.filtered(lambda p: p.x_payment_status == 'draft').write({'x_payment_status': 'in_process'})
        self.message_post(body=_('Payment set to In Process.'))

    def _create_wht_payment_if_needed(self):
        """Create a draft WHT payment to FBR if this payment has a WHT deduction.

        FO issues two cheques when WHT applies:
          1. Net amount  → Original vendor
          2. WHT amount  → FBR (Federal Board of Revenue)
        This method creates #2 automatically so FO doesn't have to do it manually.
        Idempotent: does nothing if a WHT payment already exists.
        """
        self.ensure_one()
        if self.x_wht_payment_id:
            return  # Already created — do not duplicate.

        wht_amount = sum(
            l.amount for l in self.x_tax_line_ids
            if l.tax_type == 'wht' and l.effect == 'deduct' and l.amount > 0
        )
        if not wht_amount:
            return  # No WHT on this payment.

        # For salary payments x_source_project_ids is hidden; derive from Fund Allocation.
        # For vendor payments use the explicit source project list.
        if self.x_payment_category == 'salary':
            src_ids = self.x_allocation_ids.mapped('project_analytic_account_id').ids
            origin_label = self.x_salary_sheet_id.name or 'Salary'
        else:
            src_ids = self.x_source_project_ids.ids
            origin_label = self.partner_id.name or ''

        # Optionally pre-fill FBR as vendor if they exist — FO can select/change later.
        fbr_partner = self.env['res.partner'].search(
            ['|', ('name', 'ilike', 'Federal Board of Revenue'),
                  ('name', 'ilike', 'FBR')],
            limit=1,
        )

        memo = _('WHT — %s | %s') % (origin_label, self.name or '')

        wht_payment = self.create({
            'payment_type': 'outbound',
            'partner_type': 'supplier',
            'partner_id': fbr_partner.id if fbr_partner else False,
            'x_payee_id': fbr_partner.id if fbr_partner else False,
            'amount': wht_amount,
            'currency_id': self.currency_id.id,
            'journal_id': self.journal_id.id,
            'memo': memo,
            'x_payment_category': 'vendor',
            'x_destination_project_id': self.x_destination_project_id.id or False,
            'x_source_project_ids': [(6, 0, src_ids)],
            'x_origin_payment_id': self.id,
        })
        self.x_wht_payment_id = wht_payment.id
        self.message_post(body=_(
            'WHT payment draft <b>%s</b> created for FBR — amount: %s %s.'
        ) % (wht_payment.name or '', self.currency_id.name, f'{wht_amount:,.2f}'))
        wht_payment.message_post(body=_(
            'Auto-generated WHT payment for <b>%s</b> (origin: %s).'
        ) % (origin_label, self.name or ''))

    def action_open_wht_payment(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('WHT Payment — FBR'),
            'res_model': 'account.payment',
            'view_mode': 'form',
            'res_id': self.x_wht_payment_id.id,
        }

    def action_open_origin_payment(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Original Vendor Payment'),
            'res_model': 'account.payment',
            'view_mode': 'form',
            'res_id': self.x_origin_payment_id.id,
        }

    def action_open_tax_deduction_move(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Tax Deduction Entry'),
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': self.x_tax_deduction_move_id.id,
        }

    def action_open_journal_entry(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Journal Entry'),
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': self.move_id.id,
        }

    def action_mark_paid(self):
        self.write({'x_payment_status': 'paid'})
        for payment in self.filtered(lambda p: p.state in _POSTED_STATES):
            payment._matracon_update_liability_on_post()
            payment._matracon_invalidate_project_funds()
        self.message_post(body=_('Payment marked as Paid.'))

    def write(self, vals):
        res = super().write(vals)
        if 'amount' in vals:
            for payment in self.filtered(
                lambda p: p.x_liability_sheet_line_id and p.x_gross_approved_amount
            ):
                if payment.amount > payment.x_gross_approved_amount + 0.01:
                    raise UserError(_(
                        'Cannot exceed CEO approved amount of %.2f.'
                    ) % payment.x_gross_approved_amount)
        # Self-heal: if a salary sheet payment somehow ended up pending, fix it.
        if 'x_salary_sheet_id' in vals or 'x_ceo_approval_state' in vals:
            self._matracon_fix_salary_ceo_state()
        return res
