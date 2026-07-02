from dateutil.relativedelta import relativedelta

from markupsafe import Markup

from odoo import models, fields, api, _
from odoo.exceptions import UserError

from . import matracon_notifications as matracon_notify


class LiabilitySheet(models.Model):
    _name = 'x.liability.sheet'
    _description = 'Liability Sheet'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date_from desc, id desc'

    # ── Schema guard: runs on every server startup ────────────────────────────
    # Odoo.sh sometimes restarts without running --update, so we defensively
    # ensure the PM columns exist before the ORM tries to SELECT them.
    @api.model
    def _register_hook(self):
        self.env.cr.execute("""
            ALTER TABLE x_liability_sheet
                ADD COLUMN IF NOT EXISTS pm_id               INTEGER,
                ADD COLUMN IF NOT EXISTS pm_is_signed        BOOLEAN NOT NULL DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS pm_signature_date   TIMESTAMP WITHOUT TIME ZONE,
                ADD COLUMN IF NOT EXISTS pm_signed_sheet     BYTEA,
                ADD COLUMN IF NOT EXISTS pm_signed_sheet_filename VARCHAR,
                ADD COLUMN IF NOT EXISTS account_move_id     INTEGER
        """)
        self.env.cr.execute("""
            ALTER TABLE x_liability_sheet_line
                ADD COLUMN IF NOT EXISTS payment_id INTEGER
        """)
        return super()._register_hook()

    name = fields.Char(
        string='Reference', compute='_compute_name', store=True, readonly=True)

    # Journal entry created on approval (appears in partner ledger)
    account_move_id = fields.Many2one(
        'account.move', string='Journal Entry', readonly=True,
        help='Posted on CEO approval — creates payable entries per vendor in partner ledger')

    project_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Project',
        required=True, tracking=True)

    date_from = fields.Date(string='Date From', required=True, tracking=True)
    date_to = fields.Date(string='Date To', required=True, tracking=True)

    state = fields.Selection([
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),
        ('approved', 'Approved'),
        ('paid', 'Paid'),
    ], default='draft', string='Status', tracking=True, required=True)

    # ── PM Signature ─────────────────────────────────────────────────────────
    pm_id = fields.Many2one(
        'res.users', string='Project Manager',
        tracking=True,
        help='PM responsible for this project liability sheet')
    pm_is_signed = fields.Boolean(
        string='Signed by PM', default=False, tracking=True, readonly=True)
    pm_signature_date = fields.Datetime(
        string='PM Signed On', readonly=True)
    pm_signed_sheet = fields.Binary(string='PM Signed Copy (Upload)')
    pm_signed_sheet_filename = fields.Char()

    line_ids = fields.One2many(
        'x.liability.sheet.line', 'sheet_id', string='Liability Lines')

    total_liability = fields.Float(
        string='Total Liability', compute='_compute_totals', store=True)
    total_recommended = fields.Float(
        string='Total Recommended', compute='_compute_totals', store=True)
    total_approved = fields.Float(
        string='Total Approved', compute='_compute_totals', store=True)
    total_paid = fields.Float(
        string='Total Paid', compute='_compute_totals', store=True)

    payment_ids = fields.One2many(
        'account.payment', 'x_liability_sheet_id', string='Payment Drafts',
        readonly=True)

    # ─────────────────────────────────────────────────────────────────────────
    # COMPUTE
    # ─────────────────────────────────────────────────────────────────────────

    @api.depends('project_analytic_account_id', 'date_from', 'date_to')
    def _compute_name(self):
        for sheet in self:
            proj = (sheet.project_analytic_account_id.code
                    or sheet.project_analytic_account_id.name
                    or '')
            df = sheet.date_from.strftime('%b-%Y') if sheet.date_from else ''
            dt = sheet.date_to.strftime('%b-%Y') if sheet.date_to else ''
            if proj:
                sheet.name = f'LS/{proj}/{df}-{dt}'
            else:
                sheet.name = f'LS/{df}-{dt}'

    @api.depends(
        'line_ids.liability_amount',
        'line_ids.recommended_amount',
        'line_ids.approved_amount',
        'line_ids.paid_amount',
    )
    def _compute_totals(self):
        for sheet in self:
            sheet.total_liability = sum(sheet.line_ids.mapped('liability_amount'))
            sheet.total_recommended = sum(sheet.line_ids.mapped('recommended_amount'))
            sheet.total_approved = sum(sheet.line_ids.mapped('approved_amount'))
            sheet.total_paid = sum(sheet.line_ids.mapped('paid_amount'))

    # ─────────────────────────────────────────────────────────────────────────
    # ACTIONS / WORKFLOW
    # ─────────────────────────────────────────────────────────────────────────

    def action_submit(self):
        """Site Accountant submits after recommending amounts + PM signed upload."""
        for sheet in self:
            if not sheet.line_ids:
                raise UserError(_('Cannot submit a liability sheet with no lines.'))
            if not sheet.pm_signed_sheet:
                raise UserError(_(
                    'Upload the physically signed PM document before submitting. '
                    'Download the PDF, get it signed offline, then attach the scan.'
                ))
            missing = sheet.line_ids.filtered(
                lambda l: l.liability_amount > 0 and l.recommended_amount <= 0)
            if missing:
                raise UserError(_(
                    'Enter a Recommended Amount for every vendor/subcontractor '
                    'with outstanding liability before submitting.'
                ))
            sheet.state = 'submitted'
            sheet.message_post(
                body=Markup(_(
                    'Liability Sheet submitted for CEO approval by <b>%s</b>.'
                )) % self.env.user.name)
            ceo_users = self.env['res.users'].search([
                ('group_ids', 'in', self.env.ref(
                    'purchase_demand_raise.group_ceo_approval').id),
            ])
            matracon_notify.notify_users(
                sheet,
                ceo_users,
                _('Liability Sheet <b>%s</b> submitted — CEO approval required.') % sheet.name,
                summary=_('Liability Sheet Approval'),
            )
            matracon_notify.schedule_activity(
                sheet,
                ceo_users,
                _('Approve Liability Sheet %s') % sheet.name,
            )

    def action_ceo_approve(self):
        """CEO locks approved amounts and creates vendor payment drafts for FO."""
        for sheet in self:
            if sheet.state != 'submitted':
                raise UserError(_('Only submitted liability sheets can be approved.'))
            lines = sheet.line_ids.filtered(lambda l: l.recommended_amount > 0)
            if not lines:
                raise UserError(_('No lines with a recommended amount to approve.'))
            unapproved = lines.filtered(lambda l: l.approved_amount <= 0)
            if unapproved:
                raise UserError(_(
                    'Set CEO approval (Full / % / Manual) for all recommended lines '
                    'before approving: %s'
                ) % ', '.join(unapproved.mapped('partner_id.display_name')))
            over = lines.filtered(
                lambda l: l.approved_amount > l.recommended_amount + 0.01)
            if over:
                raise UserError(_(
                    'Approved amount cannot exceed recommended amount.'
                ))
            sheet.line_ids.write({'is_locked': True})
            payments = sheet._create_ceo_payment_drafts()
            sheet.state = 'approved'
            msg = Markup(_(
                'Liability Sheet approved by CEO <b>%s</b>. '
                'Total Approved: <b>%s</b>.'
            )) % (self.env.user.name, f'{sheet.total_approved:,.2f}')
            if payments:
                msg += Markup(_(
                    '<br/>%d vendor payment draft(s) created for Finance HO.'
                )) % len(payments)
            sheet.message_post(body=msg)
            fo_users = self.env['res.users'].search([
                ('group_ids', 'in', self.env.ref(
                    'site_operations.group_finance_ho').id),
            ])
            matracon_notify.notify_users(
                sheet,
                fo_users,
                _('CEO approved liability sheet <b>%s</b> — %d payment draft(s) ready for Finance HO.')
                % (sheet.name, len(payments)),
                summary=_('Payments Ready for Finance HO'),
            )
            matracon_notify.schedule_activity(
                sheet,
                fo_users,
                _('Process vendor payments for %s') % sheet.name,
            )

    def _create_ceo_payment_drafts(self):
        """One locked outbound payment draft per approved vendor line."""
        self.ensure_one()
        Payment = self.env['account.payment'].sudo()
        created = Payment
        for line in self.line_ids.filtered(
            lambda l: l.approved_amount > 0 and l.partner_id and not l.payment_id
        ):
            payment = Payment.create({
                'payment_type': 'outbound',
                'partner_type': 'supplier',
                'partner_id': line.partner_id.id,
                'amount': line.approved_amount,
                'x_gross_approved_amount': line.approved_amount,
                'x_liability_sheet_id': self.id,
                'x_liability_sheet_line_id': line.id,
                'x_destination_project_id': self.project_analytic_account_id.id,
                'x_payment_status': 'draft',
            })
            line.payment_id = payment.id
            created |= payment
        return created

    def action_finalize_if_fully_paid(self):
        """Notify when every approved line is settled — FO closes the sheet manually."""
        for sheet in self:
            if sheet.state != 'approved':
                continue
            if not sheet.line_ids:
                continue
            unpaid = sheet.line_ids.filtered(
                lambda l: l.approved_amount > 0
                and l.paid_amount < l.approved_amount - 0.01
            )
            if unpaid:
                continue
            # All lines settled — notify FO to close manually via "Mark Paid" button.
            sheet.message_post(body=_(
                'All approved payments recorded — Finance HO can now close this sheet.'
            ))

    def _create_next_period_sheet(self):
        """Copy all partners; opening balance = remaining liability after payments."""
        self.ensure_one()
        if not self.date_to:
            return self.env['x.liability.sheet']
        date_from = self.date_to + relativedelta(days=1)
        period_days = (self.date_to - self.date_from).days + 1
        date_to = date_from + relativedelta(days=period_days - 1)

        existing = self.search([
            ('project_analytic_account_id', '=', self.project_analytic_account_id.id),
            ('date_from', '=', date_from),
            ('state', '=', 'draft'),
        ], limit=1)
        if existing:
            return existing

        line_vals = []
        for line in self.line_ids:
            remaining = max(line.liability_amount - line.paid_amount, 0.0)
            line_vals.append((0, 0, {
                'partner_id': line.partner_id.id,
                'description': line.description,
                'opening_balance': remaining,
                'new_liability': 0.0,
                'recommended_amount': 0.0,
            }))

        if not line_vals:
            return self.env['x.liability.sheet']

        return self.create({
            'project_analytic_account_id': self.project_analytic_account_id.id,
            'date_from': date_from,
            'date_to': date_to,
            'line_ids': line_vals,
        })

    def action_fo_mark_paid(self):
        """Finance HO closes the sheet after all vendor payments are posted."""
        for sheet in self:
            if sheet.state != 'approved':
                raise UserError(_('Only approved liability sheets can be marked paid.'))
            # Sync paid amounts from any posted payments first.
            sheet._sync_paid_amounts_from_payments()
            # Check whether all approved lines are fully settled.
            unpaid = sheet.line_ids.filtered(
                lambda l: l.approved_amount > 0
                and l.paid_amount < l.approved_amount - 0.01
            )
            if unpaid:
                # Lines still outstanding — require all non-zero payments to be posted.
                unposted = sheet.payment_ids.filtered(
                    lambda p: p.state != 'posted' and (p.amount or 0) > 0.01
                )
                if unposted:
                    raise UserError(_(
                        'Post all vendor payments before closing the sheet: %s'
                    ) % ', '.join(unposted.mapped('name')))
                raise UserError(_(
                    'Some approved lines are not fully paid yet: %s'
                ) % ', '.join(unpaid.mapped('partner_id.display_name')))
            sheet.state = 'paid'
            sheet.message_post(body=_('All approved payments completed — sheet closed by Finance HO.'))
            next_sheet = sheet._create_next_period_sheet()
            if next_sheet:
                sheet.message_post(body=Markup(_(
                    'Next period liability sheet <b>%s</b> created with '
                    'opening balances carried forward.'
                )) % next_sheet.name)

    def _sync_paid_amounts_from_payments(self):
        """Refresh line paid amounts from posted vendor payments."""
        for sheet in self:
            for line in sheet.line_ids:
                payments = sheet.payment_ids.filtered(
                    lambda p: p.state == 'posted'
                    and p.x_liability_sheet_line_id == line
                )
                if payments:
                    line.paid_amount = sum(
                        p.x_gross_approved_amount or p.amount for p in payments
                    )

    def action_reset_draft(self):
        for sheet in self:
            if sheet.state not in ('submitted', 'approved'):
                raise UserError(_('Only submitted or approved sheets can be reset.'))
            draft_payments = sheet.payment_ids.filtered(lambda p: p.state == 'draft')
            if sheet.payment_ids.filtered(lambda p: p.state == 'posted'):
                raise UserError(_(
                    'Cannot reset — one or more vendor payments are already posted.'
                ))
            draft_payments.unlink()
            sheet.line_ids.write({'is_locked': False, 'payment_id': False})
            sheet.state = 'draft'
            sheet.message_post(body=_('Liability Sheet reset to Draft.'))

    def action_download_pdf(self):
        """Download unsigned sheet for offline PM signature."""
        return self.env.ref(
            'site_operations.action_report_liability_sheet').report_action(self)

    def action_view_payments(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Vendor Payments — %s') % self.name,
            'res_model': 'account.payment',
            'view_mode': 'list,form',
            'domain': [('x_liability_sheet_id', '=', self.id)],
        }

    def action_refresh_from_ledger(self):
        """Pull opening_balance and new_liability from actual partner ledger entries,
        scoped to this liability sheet's project so totals are never mixed across sites."""
        AML = self.env['account.move.line'].sudo()
        for sheet in self:
            for line in sheet.line_ids:
                if not line.partner_id:
                    continue

                base_domain = [
                    ('partner_id', '=', line.partner_id.id),
                    ('move_id.state', '=', 'posted'),
                    ('account_id.account_type', '=', 'liability_payable'),
                ]

                # Scope to this sheet's project so figures from other sites are excluded.
                if sheet.project_analytic_account_id:
                    base_domain.append(
                        ('move_id.x_project_analytic_account_id', '=',
                         sheet.project_analytic_account_id.id)
                    )

                # Use move_id.date (always set on every journal entry and vendor bill)
                # instead of invoice_date which is NULL on plain journal entries
                # such as backcharge entries, causing those amounts to be missed.
                opening_lines = AML.search(base_domain + [
                    ('move_id.date', '<', sheet.date_from),
                    ('reconciled', '=', False),
                ])
                opening = max(0.0, sum(l.credit - l.debit for l in opening_lines))

                period_lines = AML.search(base_domain + [
                    ('move_id.date', '>=', sheet.date_from),
                    ('move_id.date', '<=', sheet.date_to),
                ])
                new_liab = sum(l.credit - l.debit for l in period_lines)

                line.write({
                    'opening_balance': round(opening, 2),
                    'new_liability': round(new_liab, 2),
                })

            sheet.message_post(
                body=Markup(_('Liability amounts refreshed from partner ledger by <b>%s</b>.'))
                % self.env.user.name
            )


class LiabilitySheetLine(models.Model):
    _name = 'x.liability.sheet.line'
    _description = 'Liability Sheet Line'
    _order = 'sequence, id'

    sheet_id = fields.Many2one(
        'x.liability.sheet', string='Sheet',
        ondelete='cascade', required=True)
    sequence = fields.Integer(default=10)
    is_locked = fields.Boolean(default=False)

    description = fields.Char(string='Description')
    partner_id = fields.Many2one(
        'res.partner', string='Vendor/Partner', required=True,
        domain="[('category_id.name', 'in', ['Vendor', 'Subcontractor'])]",
    )

    opening_balance = fields.Float(string='Opening Balance')
    new_liability = fields.Float(string='New Liability (Bills)')
    liability_amount = fields.Float(
        string='Total Liability',
        compute='_compute_liability_amount', store=True)

    recommended_amount = fields.Float(string='Recommended Amount')

    payment_id = fields.Many2one(
        'account.payment', string='Payment Draft', readonly=True, copy=False)

    x_is_ceo = fields.Boolean(compute='_compute_role_flags')

    decision = fields.Selection([
        ('full', 'Full'),
        ('manual', 'Manual'),
        ('25', '25%'),
        ('50', '50%'),
        ('75', '75%'),
    ], string='CEO Decision', default='full')
    approved_pct = fields.Float(
        string='Approved %', compute='_compute_approved_pct', store=False)
    approved_amount = fields.Float(
        string='Approved Amount',
        compute='_compute_approved_amount',
        store=True, readonly=False)

    remarks = fields.Text(string='Remarks')
    paid_amount = fields.Float(string='Paid Amount')
    balance = fields.Float(
        string='Balance', compute='_compute_balance', store=True)

    # ─────────────────────────────────────────────────────────────────────────
    # COMPUTE
    # ─────────────────────────────────────────────────────────────────────────

    def _compute_role_flags(self):
        is_ceo = self.env.user.has_group('purchase_demand_raise.group_ceo_approval')
        for line in self:
            line.x_is_ceo = is_ceo

    @api.depends('opening_balance', 'new_liability')
    def _compute_liability_amount(self):
        for line in self:
            line.liability_amount = line.opening_balance + line.new_liability

    @api.depends('decision', 'recommended_amount', 'approved_amount')
    def _compute_approved_pct(self):
        for line in self:
            if line.recommended_amount:
                line.approved_pct = (
                    (line.approved_amount / line.recommended_amount) * 100.0)
            else:
                line.approved_pct = 0.0

    @api.depends('decision', 'recommended_amount')
    def _compute_approved_amount(self):
        pct_map = {'25': 0.25, '50': 0.50, '75': 0.75}
        for line in self:
            if line.is_locked:
                continue
            if line.decision == 'full':
                line.approved_amount = line.recommended_amount
            elif line.decision in pct_map:
                line.approved_amount = line.recommended_amount * pct_map[line.decision]

    @api.depends('liability_amount', 'paid_amount')
    def _compute_balance(self):
        for line in self:
            line.balance = line.liability_amount - line.paid_amount

    # ─────────────────────────────────────────────────────────────────────────
    # ONCHANGE
    # ─────────────────────────────────────────────────────────────────────────

    @api.onchange('decision', 'recommended_amount')
    def _onchange_decision(self):
        pct_map = {'25': 0.25, '50': 0.50, '75': 0.75}
        if self.decision == 'full':
            self.approved_amount = self.recommended_amount
        elif self.decision in pct_map:
            self.approved_amount = self.recommended_amount * pct_map[self.decision]

    @api.onchange('approved_amount')
    def _onchange_approved_amount(self):
        if not self.recommended_amount:
            return
        pct_map = {'25': 0.25, '50': 0.50, '75': 0.75}
        for key, pct in pct_map.items():
            expected = self.recommended_amount * pct
            if abs(self.approved_amount - expected) < 0.01:
                self.decision = key
                return
        if abs(self.approved_amount - self.recommended_amount) < 0.01:
            self.decision = 'full'
            return
        self.decision = 'manual'

    def write(self, vals):
        user = self.env.user
        can_approve = (
            user.has_group('purchase_demand_raise.group_ceo_approval')
            or user.has_group('purchase_demand_raise.group_matracon_admin')
            or user.has_group('base.group_system')
        )
        if not can_approve:
            blocked = {'approved_amount', 'decision', 'is_locked'} & set(vals)
            if blocked:
                raise UserError(_(
                    'Only the CEO can set approval decisions and approved amounts.'
                ))
        return super().write(vals)
