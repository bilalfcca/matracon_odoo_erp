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

    x_payment_category = fields.Selection([
        ('vendor', 'Vendor / Liability'),
        ('salary', 'Salary'),
        ('petty_cash', 'Petty Cash'),
    ], string='Payment Category', default='vendor', tracking=True)

    x_ceo_approval_state = fields.Selection([
        ('not_required', 'Not Required'),
        ('pending', 'Pending CEO'),
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
        string='Total Liability', readonly=True)

    x_total_approved = fields.Float(
        related='x_liability_sheet_id.total_approved',
        string='Total Approved', readonly=True)

    x_vendor_bank_account_id = fields.Many2one(
        'res.partner.bank', string='Vendor Bank Account',
        domain="[('partner_id', '=', partner_id)]")

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

    x_ipc_id = fields.Many2one(
        'x.subcontractor.ipc', string='IPC Reference', tracking=True,
        help='Interim Payment Certificate this payment is linked to.')

    x_allocation_ids = fields.One2many(
        'x.payment.project.allocation', 'payment_id',
        string='Fund Allocation')

    x_available_bank_balance = fields.Float(
        string='Available Bank Balance',
        compute='_compute_available_bank_balance', store=False)

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

    @api.depends('payment_method_line_id', 'payment_method_line_id.code', 'payment_method_line_id.name')
    def _compute_x_is_cheque_payment(self):
        for payment in self:
            code = (payment.payment_method_line_id.code or '').lower()
            name = (payment.payment_method_line_id.name or '').lower()
            payment.x_is_cheque_payment = (
                'check' in code or 'cheque' in code
                or 'check' in name or 'cheque' in name
            )

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
            if category in ('salary', 'petty_cash'):
                if self.env.user.has_group('purchase_demand_raise.group_ceo_approval'):
                    vals['x_ceo_approval_state'] = 'approved'
                elif self.env.user.has_group('site_operations.group_finance_ho'):
                    vals['x_ceo_approval_state'] = 'pending'
        payments = super().create(vals_list)
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
            if payment.x_payment_category not in ('salary', 'petty_cash'):
                raise UserError(_('CEO approval applies to salary and petty cash only.'))
            if payment.x_ceo_approval_state != 'pending':
                raise UserError(_('This payment is not pending CEO approval.'))
            payment.x_ceo_approval_state = 'approved'
            payment.message_post(body=_('CEO approved payment.'))
            fo_users = self.env['res.users'].search([
                ('group_ids', 'in', self.env.ref(
                    'site_operations.group_finance_ho').id),
            ])
            matracon_notify.notify_users(
                payment,
                fo_users,
                _('CEO approved <b>%s</b> payment — please process.') % payment.name,
                summary=_('Payment Approved by CEO'),
            )
            matracon_notify.schedule_activity(
                payment, fo_users, _('Process payment %s') % payment.name)

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
        for payment in self.filtered(
            lambda p: p.x_payment_category in ('salary', 'petty_cash')
        ):
            if payment.x_ceo_approval_state == 'pending':
                raise UserError(_(
                    'CEO approval is required before posting this %s payment.'
                ) % payment.x_payment_category)

    def _matracon_update_petty_cash_on_post(self):
        for payment in self.filtered(
            lambda p: p.state in _POSTED_STATES and p.x_petty_cash_request_id
        ):
            payment.x_petty_cash_request_id.action_mark_released(payment.amount)

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

    def action_post(self):
        self._validate_ceo_payment_approval()
        for payment in self.filtered(lambda p: p.x_liability_sheet_line_id):
            payment.amount = payment.x_net_payable or payment.amount
        self._validate_liability_payment()
        self._validate_fund_allocations()
        res = super().action_post()
        for payment in self.filtered(lambda p: p.state in _POSTED_STATES):
            payment._matracon_ensure_fund_allocations()
            payment._matracon_tag_payment_move_analytic()
            payment._matracon_create_interproject_entries()
            payment._matracon_update_liability_on_post()
            payment._matracon_update_petty_cash_on_post()
            payment._matracon_update_salary_on_post()
            payment._matracon_invalidate_project_funds()
        return res

    def _matracon_tag_payment_move_analytic(self):
        self.ensure_one()
        analytic = self.x_destination_project_id or self.x_fund_project_id
        if not analytic or not self.move_id:
            return
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

    def action_set_in_process(self):
        # Post to accounting (creates journal entry, tags analytic, updates liability)
        draft = self.filtered(lambda p: p.state == 'draft')
        if draft:
            draft.action_post()
        self.filtered(lambda p: p.x_payment_status == 'draft').write({'x_payment_status': 'in_process'})
        self.message_post(body=_('Payment set to In Process.'))

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
        return res
