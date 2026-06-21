from odoo import models, fields, api, _
from odoo.exceptions import UserError


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
                ADD COLUMN IF NOT EXISTS pm_signed_sheet_filename VARCHAR
        """)
        return super()._register_hook()

    name = fields.Char(
        string='Reference', compute='_compute_name', store=True, readonly=True)

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
        for sheet in self:
            if not sheet.line_ids:
                raise UserError(
                    _('Cannot submit a liability sheet with no lines.'))
            sheet.state = 'submitted'
            sheet.message_post(
                body=_('Liability Sheet submitted for CEO approval.'))

    def action_ceo_approve(self):
        for sheet in self:
            sheet.state = 'approved'
            sheet.line_ids.write({'is_locked': True})
            sheet.message_post(
                body=_(
                    'Liability Sheet approved and locked by CEO. '
                    'Total Approved: %s'
                ) % f'{sheet.total_approved:,.2f}'
            )

    def action_mark_paid(self):
        for sheet in self:
            sheet.state = 'paid'
            sheet.message_post(body=_('Liability Sheet marked as Paid.'))

    def action_reset_draft(self):
        for sheet in self:
            if sheet.state == 'submitted':
                sheet.state = 'draft'
                sheet.message_post(body=_('Liability Sheet reset to Draft.'))

    def action_pm_sign(self):
        """Record PM digital signature on the liability sheet."""
        for sheet in self:
            if sheet.pm_is_signed:
                raise UserError(_('This sheet has already been signed by the PM.'))
            sheet.write({
                'pm_is_signed': True,
                'pm_signature_date': fields.Datetime.now(),
                'pm_id': self.env.user.id,
            })
            sheet.message_post(
                body=_('Liability Sheet signed by Project Manager: <b>%s</b> on %s.') % (
                    self.env.user.name,
                    fields.Datetime.now().strftime('%d-%b-%Y %H:%M'),
                )
            )

    def action_refresh_from_ledger(self):
        """Pull opening_balance and new_liability from actual partner ledger entries."""
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

                # Opening balance = unreconciled payable entries BEFORE period
                opening_lines = AML.search(base_domain + [
                    ('move_id.invoice_date', '<', sheet.date_from),
                    ('reconciled', '=', False),
                ])
                opening = sum(l.credit - l.debit for l in opening_lines)

                # New liability = net payable movement IN the period
                # credit − debit: bills add (+), backcharges/credit-notes reduce (−)
                period_lines = AML.search(base_domain + [
                    ('move_id.invoice_date', '>=', sheet.date_from),
                    ('move_id.invoice_date', '<=', sheet.date_to),
                ])
                new_liab = sum(l.credit - l.debit for l in period_lines)

                line.write({
                    'opening_balance': round(opening, 2),
                    'new_liability': round(new_liab, 2),
                })

            sheet.message_post(
                body=_('Liability amounts refreshed from partner ledger by <b>%s</b>.')
                % self.env.user.name
            )

    def action_download_pdf(self):
        return self.env.ref(
            'site_operations.action_report_liability_sheet').report_action(self)


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
        'res.partner', string='Vendor/Partner', required=True)

    opening_balance = fields.Float(string='Opening Balance')
    new_liability = fields.Float(string='New Liability (Bills)')
    liability_amount = fields.Float(
        string='Total Liability',
        compute='_compute_liability_amount', store=True)

    recommended_amount = fields.Float(string='Recommended Amount')

    decision = fields.Selection([
        ('full', 'Full'),
        ('manual', 'Manual'),
        ('percentage', 'Percentage'),
    ], string='Approval Method', default='manual')
    approved_pct = fields.Float(string='Approved %', default=0.0)
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

    @api.depends('opening_balance', 'new_liability')
    def _compute_liability_amount(self):
        for line in self:
            line.liability_amount = line.opening_balance + line.new_liability

    @api.depends('decision', 'recommended_amount', 'approved_pct')
    def _compute_approved_amount(self):
        for line in self:
            if line.is_locked:
                continue
            if line.decision == 'full':
                line.approved_amount = line.recommended_amount
            elif line.decision == 'percentage':
                line.approved_amount = (
                    line.recommended_amount * (line.approved_pct / 100.0))
            # 'manual': user types directly — no recompute override

    @api.depends('liability_amount', 'paid_amount')
    def _compute_balance(self):
        for line in self:
            line.balance = line.liability_amount - line.paid_amount

    # ─────────────────────────────────────────────────────────────────────────
    # ONCHANGE
    # ─────────────────────────────────────────────────────────────────────────

    @api.onchange('decision', 'recommended_amount', 'approved_pct')
    def _onchange_decision(self):
        if self.decision == 'full':
            self.approved_amount = self.recommended_amount
        elif self.decision == 'percentage' and self.approved_pct:
            self.approved_amount = (
                self.recommended_amount * (self.approved_pct / 100.0))

    @api.onchange('approved_amount')
    def _onchange_approved_amount(self):
        if self.recommended_amount:
            self.approved_pct = (
                (self.approved_amount / self.recommended_amount) * 100.0)
        self.decision = 'manual'
