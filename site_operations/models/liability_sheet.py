from dateutil.relativedelta import relativedelta

from markupsafe import Markup

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

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

    x_sequence_no = fields.Char(
        string='Sequence No', copy=False, readonly=True,
        help='Auto-assigned sequential number used in the reference (e.g. 001).')

    # Journal entry created on approval (appears in partner ledger)
    account_move_id = fields.Many2one(
        'account.move', string='Journal Entry', readonly=True,
        help='Posted on CEO approval — creates payable entries per vendor in partner ledger')

    project_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Project',
        required=True, tracking=True,
        default=lambda self: self.env.user.x_default_analytic_account_id)

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

    # ── Approval tracking for digital signatures on PDF ───────────────────
    x_ceo_approved_by_id = fields.Many2one(
        'res.users', string='CEO Approved By', readonly=True, copy=False, index=True,
        help='User who CEO-approved this liability sheet. Signature shown on printed PDF.')

    line_ids = fields.One2many(
        'x.liability.sheet.line', 'sheet_id', string='Liability Lines')

    total_liability = fields.Float(
        string='Total Liability', compute='_compute_totals', store=True, digits=(16, 0))
    total_recommended = fields.Float(
        string='Total Recommended', compute='_compute_totals', store=True, digits=(16, 0))
    total_approved = fields.Float(
        string='Total Approved', compute='_compute_totals', store=True, digits=(16, 0))
    total_paid = fields.Float(
        string='Total Paid', compute='_compute_totals', store=True, digits=(16, 0))

    payment_ids = fields.One2many(
        'account.payment', 'x_liability_sheet_id', string='Payment Drafts',
        readonly=True)

    # ── Batch payment created on CEO approval ─────────────────────────────────
    batch_payment_id = fields.Many2one(
        'x.batch.payment', string='Batch Payment',
        readonly=True, copy=False,
        help='Batch payment created by CEO approval — Finance HO posts this.')

    # ── View helpers: selected lines only (CEO/FO view after submission) ─────
    selected_line_ids = fields.One2many(
        'x.liability.sheet.line', 'sheet_id',
        domain=[('is_selected', '=', True)],
        string='Selected Lines', readonly=True,
    )

    # ─────────────────────────────────────────────────────────────────────────
    # COMPUTE
    # ─────────────────────────────────────────────────────────────────────────

    # ── Partner filter (used in form to live-filter one2many lines by partner) ──
    # store=False / no compute → pure transient widget that never touches the DB.
    # Requires @api.onchange so that a change fires a server round-trip: the
    # onchange response carries the new value back to the client, which triggers
    # a full form re-render and re-evaluation of the line_ids/selected_line_ids
    # domain.  Without the onchange, OWL sees x_filter_partner_id = False (the
    # server-side default) and never applies the filter.
    x_filter_partner_id = fields.Many2one(
        'res.partner', string='Filter by Partner', store=False)

    # ── Role flag (used in form to toggle group label visibility) ─────────────
    x_is_site_accountant = fields.Boolean(compute='_compute_role_flag_sheet', store=False)

    def _compute_role_flag_sheet(self):
        is_sa = self.env.user.has_group('site_operations.group_site_accountant')
        for sheet in self:
            sheet.x_is_site_accountant = is_sa

    @api.onchange('x_filter_partner_id')
    def _onchange_x_filter_partner_id(self):
        """Return an updated domain for both One2many line fields.

        Returning {'domain': {'field': [...]}} from an onchange is the standard
        Odoo mechanism that instructs the client to apply a new domain to a field —
        it works reliably in Odoo 19 OWL, whereas a static XML domain on a
        store=False field is not always re-evaluated reactively.
        """
        if self.x_filter_partner_id:
            domain = [('partner_id', '=', self.x_filter_partner_id.id)]
        else:
            domain = []
        return {
            'domain': {
                'line_ids': domain,
                'selected_line_ids': domain,
            }
        }

    @api.depends('project_analytic_account_id', 'x_sequence_no')
    def _compute_name(self):
        for sheet in self:
            if sheet.project_analytic_account_id:
                # Prefer analytic account code (e.g. MCH-BHW); fall back to warehouse code
                site = (
                    sheet.project_analytic_account_id.code
                    or sheet.project_analytic_account_id._matracon_site_code()
                    or 'HO'
                )
                site = site.strip().upper()
            else:
                site = 'HO'
            seq = sheet.x_sequence_no or 'NEW'
            sheet.name = f'LS/{site}/{seq}'

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
    # CONSTRAINTS
    # ─────────────────────────────────────────────────────────────────────────

    @api.constrains('date_from', 'date_to', 'project_analytic_account_id')
    def _check_no_date_overlap(self):
        """Two sheets for the same project must never have overlapping date ranges.

        If Jun 1–Aug 31 exists, the next sheet must start Sep 1 at the earliest.
        The constraint applies to all states — paid sheets still own their historical
        GL data and a new sheet must not re-cover the same period.
        """
        for sheet in self:
            if not sheet.date_from or not sheet.date_to or not sheet.project_analytic_account_id:
                continue
            if sheet.date_from > sheet.date_to:
                raise ValidationError(_(
                    '"Date From" (%s) cannot be after "Date To" (%s).'
                ) % (sheet.date_from, sheet.date_to))
            overlapping = self.search([
                ('project_analytic_account_id', '=',
                 sheet.project_analytic_account_id.id),
                ('id', '!=', sheet.id),
                # Two ranges [A.from, A.to] and [B.from, B.to] overlap when
                # A.from <= B.to  AND  B.from <= A.to
                ('date_from', '<=', sheet.date_to),
                ('date_to',   '>=', sheet.date_from),
            ], limit=1)
            if overlapping:
                next_allowed = overlapping.date_to + relativedelta(days=1)
                raise ValidationError(_(
                    'Period overlap for project "%(project)s":\n'
                    '%(existing)s already covers %(efrom)s → %(eto)s.\n'
                    'The new sheet\'s period conflicts — it must start from '
                    '%(next)s at the earliest.'
                ) % {
                    'project': sheet.project_analytic_account_id.display_name,
                    'existing': overlapping.name,
                    'efrom': overlapping.date_from,
                    'eto': overlapping.date_to,
                    'next': next_allowed,
                })

    # ─────────────────────────────────────────────────────────────────────────
    # CRUD
    # ─────────────────────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records:
            if not record.x_sequence_no:
                seq = self.env['ir.sequence'].next_by_code('x.liability.sheet') or '001'
                record.x_sequence_no = seq
        return records

    # ─────────────────────────────────────────────────────────────────────────
    # ACTIONS / WORKFLOW
    # ─────────────────────────────────────────────────────────────────────────

    def action_submit(self):
        """Site Accountant submits after recommending amounts + PM signed upload.

        Only lines with `is_selected = True` are submitted to CEO.
        At least one line must be selected and have a recommended amount.
        """
        for sheet in self:
            if not sheet.line_ids:
                raise UserError(_('Cannot submit a liability sheet with no lines.'))
            selected = sheet.line_ids.filtered(lambda l: l.is_selected)
            if not selected:
                raise UserError(_(
                    'Select at least one vendor line to submit. '
                    'Tick the "Submit" checkbox on the lines you want the CEO to approve.'
                ))
            if not sheet.pm_signed_sheet:
                raise UserError(_(
                    'Upload the physically signed PM document before submitting. '
                    'Download the PDF, get it signed offline, then attach the scan.'
                ))
            missing = selected.filtered(
                lambda l: l.liability_amount > 0 and l.recommended_amount <= 0)
            if missing:
                raise UserError(_(
                    'Enter a Recommended Amount for every selected vendor before submitting: %s'
                ) % ', '.join(missing.mapped('partner_id.display_name')))
            # Pre-fill approved_amount from recommended_amount so CEO sees it ready to edit.
            # Use sudo() because LiabilitySheetLine.write() guards approved_amount against
            # non-CEO users — this is a system-driven pre-fill, not a manual CEO action.
            for line in selected:
                if line.recommended_amount > 0 and not line.approved_amount:
                    line.sudo().write({'approved_amount': line.recommended_amount})
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
        """CEO locks approved amounts and creates ONE batch payment for Finance HO.

        Only 'selected' lines (is_selected=True) are included in the batch.
        Finance HO will open the batch, add bank details + cheque numbers, then post.
        """
        for sheet in self:
            if sheet.state != 'submitted':
                raise UserError(_('Only submitted liability sheets can be approved.'))
            # CEO can only see/approve selected lines
            lines = sheet.line_ids.filtered(
                lambda l: l.is_selected and l.recommended_amount > 0)
            if not lines:
                raise UserError(_(
                    'No selected lines have a recommended amount to approve.'
                ))
            unapproved = lines.filtered(lambda l: l.approved_amount <= 0)
            if unapproved:
                raise UserError(_(
                    'Enter an Approved Amount for all selected lines before approving: %s'
                ) % ', '.join(unapproved.mapped('partner_id.display_name')))
            # Lock approved lines
            lines.write({'is_locked': True})
            # Create ONE batch payment (Finance HO posts it after adding bank + cheques)
            batch = sheet._create_ceo_batch_payment(lines)
            sheet.state = 'approved'
            sheet.x_ceo_approved_by_id = self.env.uid
            # Close the CEO activity that was created on submission
            matracon_notify.close_activities(sheet, summary_contains='Approve Liability Sheet')

            # ── Auto-create next period sheet ─────────────────────────────────
            # Runs on approval (not on paid) so SA can start filling the next
            # period immediately while Finance HO is still processing payments.
            next_sheet = None
            try:
                next_sheet = sheet._create_next_period_sheet()
            except Exception as e:
                # Never let auto-creation failure block the CEO approval
                sheet.message_post(body=Markup(_(
                    '⚠️ Could not auto-create the next period sheet: %s'
                )) % str(e))

            msg = Markup(_(
                'Liability Sheet approved by CEO <b>%(ceo)s</b>. '
                'Total Approved: <b>%(total)s</b>.<br/>'
                'Batch payment <a href="#" data-oe-model="x.batch.payment" '
                'data-oe-id="%(batch_id)s">%(batch_name)s</a> created — '
                'Finance HO must add bank details and post.'
            )) % {
                'ceo': self.env.user.name,
                'total': f'{sheet.total_approved:,.2f}',
                'batch_id': batch.id,
                'batch_name': batch.name,
            }
            sheet.message_post(body=msg)

            # Post a chatter note on both sheets about the auto-created next period
            if next_sheet:
                sheet.message_post(body=Markup(_(
                    'Next period sheet '
                    '<a href="#" data-oe-model="x.liability.sheet" '
                    'data-oe-id="%(id)s"><b>%(name)s</b></a> '
                    'auto-created for <b>%(from)s → %(to)s</b>. '
                    'Site Accountant: run "↻ Refresh from Ledger" to populate balances.'
                )) % {
                    'id': next_sheet.id,
                    'name': next_sheet.name,
                    'from': next_sheet.date_from,
                    'to': next_sheet.date_to,
                })
                next_sheet.message_post(body=Markup(_(
                    'Sheet auto-created on CEO approval of '
                    '<a href="#" data-oe-model="x.liability.sheet" '
                    'data-oe-id="%(id)s"><b>%(name)s</b></a> '
                    '(period: %(from)s → %(to)s).<br/>'
                    'Run <b>↻ Refresh from Ledger</b> to pull current vendor balances from GL.'
                )) % {
                    'id': sheet.id,
                    'name': sheet.name,
                    'from': sheet.date_from,
                    'to': sheet.date_to,
                })

            fo_users = self.env['res.users'].search([
                ('group_ids', 'in', self.env.ref(
                    'site_operations.group_finance_ho').id),
            ])
            matracon_notify.notify_users(
                sheet,
                fo_users,
                _('CEO approved liability sheet <b>%(sheet)s</b> — '
                  'batch payment <b>%(batch)s</b> is ready. '
                  'Add bank allocations and post.') % {
                    'sheet': sheet.name,
                    'batch': batch.name,
                },
                summary=_('Batch Payment Ready for Finance HO'),
            )
            matracon_notify.schedule_activity(
                sheet,
                fo_users,
                _('Post batch payment %s for %s') % (batch.name, sheet.name),
            )

    def _create_ceo_batch_payment(self, approved_lines=None):
        """Create ONE x.batch.payment covering all CEO-approved lines.

        Each line becomes one x.batch.payment.line pre-filled with the
        vendor + gross amount.  Finance HO adds bank allocations + cheque
        numbers and posts the batch (which creates individual account.payments
        and triggers all existing accounting hooks).
        """
        self.ensure_one()
        if approved_lines is None:
            approved_lines = self.line_ids.filtered(
                lambda l: l.is_selected and l.approved_amount > 0 and l.partner_id
            )

        Batch = self.env['x.batch.payment'].sudo()
        batch = Batch.create({
            'date': fields.Date.today(),
            'memo': _('Liability Sheet %s') % self.name,
            'x_destination_project_id': self.project_analytic_account_id.id or False,
            # CEO already approved at liability sheet level — no separate batch approval needed.
            'x_ceo_approval_state': 'not_required',
            'line_ids': [
                (0, 0, {
                    'partner_id': line.partner_id.id,
                    'gross_amount': line.approved_amount,
                    'memo': line.description or '',
                    'x_account_title': line.x_account_title or '',
                    'x_destination_project_id': (
                        self.project_analytic_account_id.id or False
                    ),
                    'x_liability_sheet_id': self.id,
                    'x_liability_sheet_line_id': line.id,
                })
                for line in approved_lines
                if line.partner_id and not line.payment_id
            ],
        })
        self.batch_payment_id = batch.id
        return batch

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
        """Auto-create the next liability sheet immediately following this one.

        Rules:
        - date_from  = self.date_to + 1 day  (e.g. Jun 1–Aug 31 → Sep 1)
        - date_to    = last day of that same month (Sep 1 → Sep 30); SA can edit
        - Lines      = all partners from this sheet copied with 0 amounts so the
                       site accountant can immediately run "↻ Refresh from Ledger"
                       to pull current GL balances into the new sheet
        - Idempotent = if a sheet already covering that next period exists, returns
                       it without creating a duplicate (safe to call twice)
        """
        self.ensure_one()

        next_date_from = self.date_to + relativedelta(days=1)
        next_date_to = (next_date_from + relativedelta(months=1)) - relativedelta(days=1)

        # If a sheet already covers this next period, return it — don't duplicate.
        existing = self.search([
            ('project_analytic_account_id', '=', self.project_analytic_account_id.id),
            ('id', '!=', self.id),
            ('date_from', '<=', next_date_to),
            ('date_to',   '>=', next_date_from),
        ], limit=1)
        if existing:
            return existing

        # Copy partner lines with 0 amounts — SA runs Refresh from Ledger for balances.
        # Opening balances are intentionally 0: the GL is the authoritative source.
        # Refresh from Ledger will compute correct opening/new-liability from posted moves.
        line_vals = [
            (0, 0, {
                'partner_id': line.partner_id.id,
                'description': line.description,
                'x_bank_name': line.x_bank_name or '',
                'x_account_title': line.x_account_title or '',
                'x_iban': line.x_iban or '',
                'opening_balance': 0.0,
                'new_liability': 0.0,
                'recommended_amount': 0.0,
            })
            for line in self.line_ids
            if line.partner_id
        ]

        new_sheet = self.sudo().create({
            'project_analytic_account_id': self.project_analytic_account_id.id,
            'date_from': next_date_from,
            'date_to': next_date_to,
            'line_ids': line_vals,
        })
        return new_sheet

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
            # Close the Finance HO activity that was created on CEO approval
            matracon_notify.close_activities(sheet)
            sheet.message_post(body=_(
                'All approved payments completed — sheet closed by Finance HO.'))
            # Next period sheet is normally created on CEO approval.
            # _create_next_period_sheet() is idempotent — returns the existing sheet
            # if one was already auto-created at approval time; only creates a new one
            # if somehow it was missed (e.g. sheets approved before this feature).
            next_sheet = sheet._create_next_period_sheet()
            if next_sheet and next_sheet.state == 'draft':
                # Only post a message if this is a brand-new sheet (wasn't created at approval)
                already_noted = any(
                    'auto-created on CEO approval' in (m.body or '')
                    for m in next_sheet.message_ids
                )
                if not already_noted:
                    sheet.message_post(body=Markup(_(
                        'Next period sheet '
                        '<a href="#" data-oe-model="x.liability.sheet" '
                        'data-oe-id="%(id)s"><b>%(name)s</b></a> '
                        'created for <b>%(from)s → %(to)s</b>.'
                    )) % {
                        'id': next_sheet.id,
                        'name': next_sheet.name,
                        'from': next_sheet.date_from,
                        'to': next_sheet.date_to,
                    })

    def _sync_paid_amounts_from_payments(self):
        """Refresh line paid amounts from posted vendor payments.

        Excludes WHT companion payments (x_origin_payment_id set) — those are
        payments to FBR for WHT, not payments against the vendor liability.
        Using x_gross_approved_amount (CEO-approved gross) ensures WHT deduction
        is NOT double-counted: the gross already includes the WHT portion that was
        deducted from the net sent to the vendor.
        """
        for sheet in self:
            for line in sheet.line_ids:
                payments = sheet.payment_ids.filtered(
                    lambda p: (p.state in ('in_process', 'paid') or p.state == 'posted')
                    and p.x_liability_sheet_line_id == line
                    and not p.x_origin_payment_id  # exclude WHT companion payments
                )
                if payments:
                    line.paid_amount = sum(
                        p.x_gross_approved_amount or p.amount for p in payments
                    )

    def _is_ho_role(self):
        """Return True if the current user holds any Head Office role."""
        u = self.env.user
        return (
            u.has_group('purchase_demand_raise.group_matracon_admin')
            or u.has_group('purchase_demand_raise.group_ceo_approval')
            or u.has_group('site_operations.group_finance_ho')
            or u.has_group('purchase_demand_raise.group_procurement_ho')
        )

    def action_reset_draft(self):
        """Standard reset — available to Site Accountant and HO roles.

        Blocked if posted payments exist (use action_force_reset_draft for that).
        """
        for sheet in self:
            if sheet.state not in ('submitted', 'approved'):
                raise UserError(_('Only submitted or approved sheets can be reset.'))
            # Check batch payment: if it has posted individual payments, block reset
            if sheet.batch_payment_id and sheet.batch_payment_id.state == 'posted':
                if sheet.payment_ids.filtered(lambda p: p.state in ('in_process', 'paid')):
                    raise UserError(_(
                        'Cannot reset — the batch payment has already been posted. '
                        'Use "Force Reset to Draft" if you are sure.'
                    ))
            elif sheet.payment_ids.filtered(lambda p: p.state in ('in_process', 'paid')):
                raise UserError(_(
                    'Cannot reset — one or more vendor payments are already posted. '
                    'Use "Force Reset to Draft" if you are sure.'
                ))
            draft_payments = sheet.payment_ids.filtered(lambda p: p.state == 'draft')
            draft_payments.unlink()
            # Cancel the batch payment if still draft
            if sheet.batch_payment_id and sheet.batch_payment_id.state == 'draft':
                sheet.batch_payment_id.action_cancel()
            sheet.batch_payment_id = False
            sheet.line_ids.write({'is_locked': False, 'payment_id': False})
            prev_state = dict(sheet._fields['state'].selection).get(sheet.state, sheet.state)
            sheet.state = 'draft'
            matracon_notify.close_activities(sheet)
            sheet.message_post(body=Markup(_(
                'Liability Sheet reset to <b>Draft</b> by <b>%(user)s</b> '
                '(was: <i>%(prev)s</i>).'
            )) % {'user': self.env.user.name, 'prev': prev_state})

    def action_force_reset_draft(self):
        """HO-only force reset — works from any state, even with posted payments.

        Posted payments are NOT cancelled (they remain as legitimate accounting
        entries). Lines are unlocked so the sheet can be corrected and re-submitted.
        Full audit trail is written to the chatter.
        """
        for sheet in self:
            if not self._is_ho_role():
                raise UserError(_(
                    'Only Head Office roles (Admin, CEO, Finance HO, Procurement HO) '
                    'can force-reset a liability sheet.'
                ))
            if sheet.state == 'draft':
                continue  # nothing to do

            prev_state = dict(sheet._fields['state'].selection).get(sheet.state, sheet.state)

            # Cancel draft payments; leave posted ones intact as accounting history.
            draft_payments = sheet.payment_ids.filtered(
                lambda p: p.state == 'draft'
            )
            posted_payments = sheet.payment_ids.filtered(
                lambda p: p.state in ('in_process', 'paid')
            )
            draft_payments.unlink()

            # Cancel and clear batch payment if still draft
            if sheet.batch_payment_id and sheet.batch_payment_id.state == 'draft':
                sheet.batch_payment_id.action_cancel()
            sheet.batch_payment_id = False

            # Unlock all lines and clear draft payment links.
            sheet.line_ids.write({'is_locked': False, 'payment_id': False})
            sheet.state = 'draft'
            matracon_notify.close_activities(sheet)

            # Build detailed chatter message — always preserved.
            msg_parts = [Markup(_(
                'Liability Sheet <b>force-reset to Draft</b> by <b>%(user)s</b> '
                '(was: <i>%(prev)s</i>).'
            )) % {'user': self.env.user.name, 'prev': prev_state}]

            if posted_payments:
                msg_parts.append(Markup(_(
                    '<br/>⚠️ <b>%(n)d posted payment(s) were NOT cancelled</b> and '
                    'remain in accounting: %(names)s. '
                    'Adjust or reconcile them manually if needed.'
                )) % {
                    'n': len(posted_payments),
                    'names': ', '.join(posted_payments.mapped('name')),
                })

            sheet.message_post(body=Markup('').join(msg_parts))

    def action_download_pdf(self):
        """Download liability sheet PDF for the current record(s).

        When called from the form view, ``self`` is a single record.
        When called from the list view header (multiple checkboxes ticked),
        ``self`` is the recordset of ONLY the selected records — the header
        button mechanism guarantees this so the download is scoped correctly.
        """
        if not self:
            raise UserError(_('No liability sheet selected.'))
        return self.env.ref(
            'site_operations.action_report_liability_sheet').report_action(self)

    def action_view_batch_payment(self):
        """Open the batch payment created by CEO approval."""
        self.ensure_one()
        if not self.batch_payment_id:
            raise UserError(_('No batch payment has been created for this sheet yet.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Batch Payment — %s') % self.name,
            'res_model': 'x.batch.payment',
            'view_mode': 'form',
            'res_id': self.batch_payment_id.id,
        }

    def action_view_payments(self):
        self.ensure_one()
        # If a batch payment was created (new flow), open it
        if self.batch_payment_id:
            return self.action_view_batch_payment()
        # Legacy: individual payments (old flow before batch)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Vendor Payments — %s') % self.name,
            'res_model': 'account.payment',
            'view_mode': 'list,form',
            'domain': [('x_liability_sheet_id', '=', self.id)],
        }

    def action_refresh_from_ledger(self):
        """Pull opening_balance and new_liability from actual partner ledger entries,
        scoped to this liability sheet's project so totals are never mixed across sites.

        Works for both vendor bills (x_project_analytic_account_id on the move header)
        and direct journal entries (analytic_distribution on the individual payable line).
        Also auto-adds lines for partners found in the ledger but not yet on the sheet.
        """
        AML = self.env['account.move.line'].sudo()
        for sheet in self:
            analytic_id = (
                sheet.project_analytic_account_id.id
                if sheet.project_analytic_account_id else None
            )
            str_analytic_id = str(analytic_id) if analytic_id else None

            def _in_project(aml_line, _aid=analytic_id, _said=str_analytic_id):
                """True when this line belongs to the sheet's analytic project.

                Checks the move-level field (vendor bills) AND the line-level
                analytic_distribution (direct MISC journal entries).
                """
                if not _aid:
                    return True  # no analytic filter — include everything
                if aml_line.move_id.x_project_analytic_account_id.id == _aid:
                    return True
                return _said in (aml_line.analytic_distribution or {})

            # ── 1. Refresh amounts on already-existing lines ──────────────────
            for line in sheet.line_ids:
                if not line.partner_id:
                    continue

                # Fetch all posted payable lines for this partner, then filter
                # by project in Python so both move-level and line-level analytics
                # are honoured (ORM domain cannot query inside JSON fields).
                all_lines = AML.search([
                    ('partner_id', '=', line.partner_id.id),
                    ('move_id.state', '=', 'posted'),
                    ('account_id.account_type', '=', 'liability_payable'),
                ]).filtered(_in_project)

                # Use move_id.date: always set on journal entries; invoice_date is
                # NULL on plain journal entries and would cause amounts to be missed.
                opening_lines = all_lines.filtered(
                    lambda l: l.move_id.date < sheet.date_from and not l.reconciled
                )
                opening = max(0.0, sum(l.credit - l.debit for l in opening_lines))

                period_lines = all_lines.filtered(
                    lambda l: sheet.date_from <= l.move_id.date <= sheet.date_to
                )
                new_liab = sum(l.credit - l.debit for l in period_lines)

                partner_desc = (
                    line.partner_id.x_description
                    or line.partner_id.name
                    or ''
                ).strip()
                p = line.partner_id
                line.write({
                    'opening_balance': round(opening, 2),
                    'new_liability': round(new_liab, 2),
                    # Preserve any description the SA has manually typed.
                    # Only fill from the partner if the field is still empty.
                    'description': line.description or partner_desc,
                    # Auto-fill bank details from partner if not yet set on this line.
                    'x_bank_name': line.x_bank_name or (p.x_bank_name or ''),
                    'x_account_title': line.x_account_title or (p.x_account_title or ''),
                    'x_iban': line.x_iban or (p.x_iban or ''),
                })

            # ── 2. Auto-discover partners in the ledger not yet on the sheet ──
            # Uses raw SQL so we can leverage PostgreSQL's native JSONB '?' key-
            # existence operator to match analytic_distribution without loading
            # every move line into the ORM.
            if analytic_id:
                existing_partner_ids = sheet.line_ids.mapped('partner_id').ids or [0]
                self.env.cr.execute("""
                    SELECT DISTINCT aml.partner_id
                    FROM account_move_line aml
                    JOIN account_move     am  ON am.id  = aml.move_id
                    JOIN account_account  aa  ON aa.id  = aml.account_id
                    WHERE am.state = 'posted'
                      AND aa.account_type = 'liability_payable'
                      AND aml.partner_id IS NOT NULL
                      AND aml.partner_id != ALL(%s)
                      AND am.date <= %s
                      AND (
                          am.x_project_analytic_account_id = %s
                          OR (aml.analytic_distribution IS NOT NULL
                              AND aml.analytic_distribution ? %s)
                      )
                """, [existing_partner_ids, sheet.date_to, analytic_id, str_analytic_id])
                new_partner_ids = [row[0] for row in self.env.cr.fetchall()]

                for partner_id in new_partner_ids:
                    all_lines = AML.search([
                        ('partner_id', '=', partner_id),
                        ('move_id.state', '=', 'posted'),
                        ('account_id.account_type', '=', 'liability_payable'),
                    ]).filtered(_in_project)

                    opening_lines = all_lines.filtered(
                        lambda l: l.move_id.date < sheet.date_from and not l.reconciled
                    )
                    opening = max(0.0, sum(l.credit - l.debit for l in opening_lines))

                    period_lines = all_lines.filtered(
                        lambda l: sheet.date_from <= l.move_id.date <= sheet.date_to
                    )
                    new_liab = sum(l.credit - l.debit for l in period_lines)

                    if opening > 0 or new_liab != 0:
                        partner = self.env['res.partner'].browse(partner_id)
                        sheet.sudo().write({'line_ids': [(0, 0, {
                            'partner_id': partner_id,
                            'description': (
                                partner.x_description or partner.name or ''
                            ).strip(),
                            'x_bank_name': partner.x_bank_name or '',
                            'x_account_title': partner.x_account_title or '',
                            'x_iban': partner.x_iban or '',
                            'opening_balance': round(opening, 2),
                            'new_liability': round(new_liab, 2),
                        })]})

            sheet.message_post(
                body=Markup(_('Liability amounts refreshed from partner ledger by <b>%s</b>.'))
                % self.env.user.name
            )

    def unlink(self):
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_('Only draft liability sheets can be deleted.'))
        return super().unlink()

    def action_delete_draft(self):
        self.unlink()
        return {'type': 'ir.actions.act_window_close'}

    def action_select_all_lines(self):
        """Tick the Submit checkbox on every line (SA use in draft state)."""
        self.ensure_one()
        self.line_ids.write({'is_selected': True})

    def action_deselect_all_lines(self):
        """Untick the Submit checkbox on every line (SA use in draft state)."""
        self.ensure_one()
        self.line_ids.write({'is_selected': False})


class LiabilitySheetLine(models.Model):
    _name = 'x.liability.sheet.line'
    _description = 'Liability Sheet Line'
    _order = 'sequence, id'

    sheet_id = fields.Many2one(
        'x.liability.sheet', string='Sheet',
        ondelete='cascade', required=True)
    sequence = fields.Integer(default=10)
    is_locked = fields.Boolean(default=False)

    # Selection for partial submission — SA ticks which lines to send for CEO approval.
    is_selected = fields.Boolean(
        string='Submit',
        default=True,
        help='Tick to include this line in the submission to CEO. '
             'Unticked lines stay on the sheet for next cycle.'
    )

    description = fields.Char(string='Description')

    # Banking details — editable by site accountant, auto-filled from partner,
    # flow through to batch payment line for BPV / cheque printing.
    x_bank_name = fields.Char(string='Bank Name')
    x_account_title = fields.Char(string='Account Title')
    x_iban = fields.Char(string='IBAN / Account No')

    partner_id = fields.Many2one(
        'res.partner', string='Vendor/Partner', required=True,
        domain="[('category_id.name', 'in', ['Vendor', 'Subcontractor'])]",
    )

    opening_balance = fields.Float(string='Opening Balance', digits=(16, 0))
    new_liability = fields.Float(string='New Liability (Bills)', digits=(16, 0))
    liability_amount = fields.Float(
        string='Total Liability',
        compute='_compute_liability_amount', store=True, digits=(16, 0))

    # Recommended: entered manually by Site Accountant
    recommended_amount = fields.Float(string='Recommended Amount', digits=(16, 0))

    payment_id = fields.Many2one(
        'account.payment', string='Payment Draft', readonly=True, copy=False)

    x_is_ceo = fields.Boolean(compute='_compute_role_flags')

    # Approved: entered manually by CEO — no auto-compute, no percentage/decision helpers
    approved_amount = fields.Float(string='Approved Amount', digits=(16, 0))

    remarks = fields.Text(string='Remarks')
    paid_amount = fields.Float(string='Paid Amount', digits=(16, 0))
    balance = fields.Float(
        string='Balance', compute='_compute_balance', store=True, digits=(16, 0))

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

    @api.depends('liability_amount', 'paid_amount')
    def _compute_balance(self):
        for line in self:
            line.balance = line.liability_amount - line.paid_amount

    # ─────────────────────────────────────────────────────────────────────────
    # ONCHANGE
    # ─────────────────────────────────────────────────────────────────────────

    @api.onchange('partner_id')
    def _onchange_partner_description(self):
        """Auto-fill description and bank details when partner is selected."""
        if not self.partner_id:
            return
        tags = self.partner_id.category_id
        tag_name = tags[0].name if tags else ''
        partner_desc = (self.partner_id.x_description or '').strip()
        if tag_name and partner_desc:
            self.description = f'{tag_name} ({partner_desc})'
        elif tag_name:
            self.description = tag_name
        elif partner_desc:
            self.description = partner_desc
        # Auto-fill bank details from partner
        self.x_bank_name = self.partner_id.x_bank_name or ''
        self.x_account_title = self.partner_id.x_account_title or ''
        self.x_iban = self.partner_id.x_iban or ''

    def write(self, vals):
        # self.env.su is True when called via sudo() — allow system-driven pre-fills
        # (e.g. action_submit pre-filling approved_amount from recommended_amount).
        user = self.env.user
        can_approve = (
            self.env.su
            or user.has_group('purchase_demand_raise.group_ceo_approval')
            or user.has_group('purchase_demand_raise.group_matracon_admin')
            or user.has_group('base.group_system')
        )
        if not can_approve:
            blocked = {'approved_amount', 'is_locked'} & set(vals)
            if blocked:
                raise UserError(_(
                    'Only the CEO can set approved amounts.'
                ))
        return super().write(vals)
