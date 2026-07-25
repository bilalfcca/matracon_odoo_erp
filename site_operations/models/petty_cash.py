from markupsafe import Markup

from odoo import models, fields, api, _
from odoo.exceptions import UserError

from . import matracon_notifications as matracon_notify


class PettyCashFund(models.Model):
    _name = 'x.petty.cash.fund'
    _description = 'Site Petty Cash Fund'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'project_analytic_account_id'

    name = fields.Char(compute='_compute_name', store=True, readonly=True)
    is_ho_fund = fields.Boolean(
        string='Head Office Fund', default=False,
        help='HO-level petty cash not tied to any specific project.')
    project_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Site Project',
        tracking=True, index=True)
    balance = fields.Monetary(
        compute='_compute_balance',
        store=False,
        currency_field='currency_id',
        help='Current petty cash balance read directly from the GL (posted move lines '
             'on x_petty_cash_account_id filtered by this site\'s analytic account). '
             'Falls back to released − expensed when the GL account is not configured.',
    )
    total_released = fields.Monetary(
        compute='_compute_totals', store=True,
        currency_field='currency_id',
    )
    total_expensed = fields.Monetary(
        compute='_compute_totals', store=True,
        currency_field='currency_id',
    )
    currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id)

    # ── Petty Cash Account ───────────────────────────────────────────────────
    x_petty_cash_account_id = fields.Many2one(
        'account.account',
        string='Petty Cash Account',
        help='The "Cash in Hand" GL account for this fund (e.g. Cash in Hand — RWASA). '
             'Used as the credit account for expenses and debit account for HO releases. '
             'Auto-filled from the Site Project Configuration.',
        tracking=True,
    )

    request_ids = fields.One2many(
        'x.petty.cash.request', 'fund_id', string='Requests')
    expense_ids = fields.One2many(
        'x.petty.cash.expense', 'fund_id', string='Expenses')

    @api.depends('project_analytic_account_id', 'is_ho_fund')
    def _compute_name(self):
        for fund in self:
            if fund.is_ho_fund and not fund.project_analytic_account_id:
                fund.name = 'Petty Cash — Head Office'
            else:
                proj = fund.project_analytic_account_id.display_name or _('Petty Cash')
                fund.name = f'Petty Cash — {proj}'

    @api.depends(
        'request_ids.state', 'request_ids.released_amount',
        'expense_ids.amount', 'expense_ids.state',
    )
    def _compute_totals(self):
        """Compute total_released and total_expensed from the custom request/expense
        records. These fields track the workflow totals for reporting purposes."""
        for fund in self:
            released = sum(fund.request_ids.filtered(
                lambda r: r.state in ('released', 'confirmed')
            ).mapped('released_amount'))
            expensed = sum(fund.expense_ids.filtered(
                lambda e: e.state == 'posted'
            ).mapped('amount'))
            fund.total_released = released
            fund.total_expensed = expensed

    def _compute_balance(self):
        """Compute the fund balance directly from the GL (posted account.move.lines).

        When a petty cash GL account can be resolved (from the fund's own
        x_petty_cash_account_id OR from the linked site config), this reads the
        actual debit/credit balance of that account filtered to this site's analytic
        account — so ALL journal entries that touch the petty cash GL account
        (including HO opening balance entries, replenishment payments, manual journal
        entries, and expense journal entries) are reflected automatically.

        Two analytic-linking paths are checked (OR) to be consistent with the
        account.account Site Balance computation:
          1. move header  x_project_analytic_account_id
          2. move-line    analytic_distribution JSONB key  (HO MISC entries)

        Falls back to released − expensed only when no GL account can be determined.
        """
        for fund in self:
            acct = fund.x_petty_cash_account_id
            analytic = fund.project_analytic_account_id
            # If the fund has no direct account, look it up from the site config
            # (read-only, no write — safe inside a compute method)
            if not acct and analytic:
                site_config = self.env['x.project.site.config'].sudo().search(
                    [('analytic_account_id', '=', analytic.id)], limit=1)
                if site_config:
                    acct = site_config.x_petty_cash_account_id
            if acct and analytic:
                fund.env.cr.execute("""
                    SELECT  COALESCE(SUM(aml.balance), 0)
                    FROM    account_move_line  aml
                    JOIN    account_move       am   ON am.id = aml.move_id
                    WHERE   aml.account_id = %s
                      AND   am.state       = 'posted'
                      AND   (
                                am.x_project_analytic_account_id = %s
                             OR aml.analytic_distribution ? %s
                            )
                """, (acct.id, analytic.id, str(analytic.id)))
                fund.balance = fund.env.cr.fetchone()[0]
            else:
                # Fallback: released − expensed from custom workflow records
                released = sum(fund.request_ids.filtered(
                    lambda r: r.state in ('released', 'confirmed')
                ).mapped('released_amount'))
                expensed = sum(fund.expense_ids.filtered(
                    lambda e: e.state == 'posted'
                ).mapped('amount'))
                fund.balance = released - expensed

    @api.model
    def get_gl_balance_for_analytic(self, analytic_id):
        """Return the petty cash GL balance for a given analytic account ID.

        Designed for dashboards: works whether or not a fund record exists.
        Resolution order:
          1. Fund record with x_petty_cash_account_id → uses fund.balance (GL-based)
          2. Fund record WITHOUT account → looks up site config account, reads GL
          3. No fund but site config has account → reads GL directly
          4. Nothing configured → returns 0.0

        This means ANY journal entry touching the petty cash account with the
        correct analytic distribution will be counted, regardless of how it was
        created (manual entry, import, replenishment payment, etc.).
        """
        fund = self.search(
            [('project_analytic_account_id', '=', analytic_id)], limit=1)
        if fund:
            # fund.balance already handles site-config fallback (see _compute_balance)
            return fund.balance

        # No fund record — try to compute from site config's petty cash account
        site_config = self.env['x.project.site.config'].sudo().search(
            [('analytic_account_id', '=', analytic_id)], limit=1)
        if not site_config or not site_config.x_petty_cash_account_id:
            return 0.0

        acct_id = site_config.x_petty_cash_account_id.id
        self.env.cr.execute("""
            SELECT  COALESCE(SUM(aml.balance), 0)
            FROM    account_move_line  aml
            JOIN    account_move       am   ON am.id = aml.move_id
            WHERE   aml.account_id = %s
              AND   am.state       = 'posted'
              AND   (
                        am.x_project_analytic_account_id = %s
                     OR aml.analytic_distribution ? %s
                    )
        """, (acct_id, analytic_id, str(analytic_id)))
        return self.env.cr.fetchone()[0]

    @api.model
    def get_or_create_for_project(self, analytic):
        fund = self.search([
            ('project_analytic_account_id', '=', analytic.id),
        ], limit=1)
        if not fund:
            # Auto-fill petty cash account from site config if configured
            petty_cash_account = False
            site_config = self.env['x.project.site.config'].sudo().search([
                ('analytic_account_id', '=', analytic.id),
            ], limit=1)
            if site_config and site_config.x_petty_cash_account_id:
                petty_cash_account = site_config.x_petty_cash_account_id.id
            fund = self.create({
                'project_analytic_account_id': analytic.id,
                'x_petty_cash_account_id': petty_cash_account,
            })
        return fund

    @api.model
    def get_or_create_ho_fund(self):
        fund = self.search([('is_ho_fund', '=', True)], limit=1)
        if not fund:
            fund = self.create({'is_ho_fund': True})
        return fund

    def _get_petty_cash_account(self):
        """Return the GL account to use for this fund's cash entries.
        Looks up in order: fund's own account → site config account → None.
        """
        self.ensure_one()
        if self.x_petty_cash_account_id:
            return self.x_petty_cash_account_id
        # Fallback: try to read from site config
        if self.project_analytic_account_id:
            config = self.env['x.project.site.config'].sudo().search([
                ('analytic_account_id', '=', self.project_analytic_account_id.id),
            ], limit=1)
            if config and config.x_petty_cash_account_id:
                # Cache it on the fund for future calls
                self.sudo().write({
                    'x_petty_cash_account_id': config.x_petty_cash_account_id.id,
                })
                return config.x_petty_cash_account_id
        return self.env['account.account']


class PettyCashRequest(models.Model):
    _name = 'x.petty.cash.request'
    _description = 'Petty Cash Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'

    name = fields.Char(
        string='Reference', readonly=True, default=lambda self: _('New'))
    fund_id = fields.Many2one(
        'x.petty.cash.fund', string='Petty Cash Fund',
        required=True, ondelete='restrict', index=True)
    project_analytic_account_id = fields.Many2one(
        related='fund_id.project_analytic_account_id',
        store=True, readonly=True)
    current_balance = fields.Monetary(
        related='fund_id.balance', currency_field='currency_id', readonly=True)
    requested_amount = fields.Monetary(
        string='Required Amount', required=True, currency_field='currency_id')
    reason = fields.Text(required=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),
        ('ceo_approved', 'CEO Approved'),
        ('released', 'Released'),
        ('confirmed', 'Confirmed'),
        ('rejected', 'Rejected'),
    ], default='draft', tracking=True, required=True)
    released_amount = fields.Monetary(
        currency_field='currency_id', readonly=True, copy=False)
    ceo_amount_type = fields.Selection([
        ('full', 'Full Amount'),
        ('pct_75', '75%'),
        ('pct_50', '50%'),
        ('pct_25', '25%'),
        ('manual', 'Manual'),
    ], string='Approve Amount As', default='full')
    ceo_approved_amount = fields.Monetary(
        string='CEO Approved Amount', currency_field='currency_id', copy=False)
    payment_id = fields.Many2one(
        'account.payment', string='Release Payment', readonly=True, copy=False)
    currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id)

    # PM-signed document — mandatory before submitting to Finance HO.
    # Workflow: print the petty cash request, get it physically signed by the
    # Project Manager, scan/photograph it and upload here.  action_submit()
    # raises UserError if this is empty.
    x_pm_signed_document = fields.Binary(
        string='PM Signed Document',
        attachment=True,
        copy=False,
    )
    x_pm_signed_document_filename = fields.Char(
        string='PM Signed Document Filename',
        copy=False,
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        user = self.env.user
        if 'fund_id' in fields_list and not res.get('fund_id'):
            analytic = getattr(user, 'x_default_analytic_account_id', False)
            if analytic:
                fund = self.env['x.petty.cash.fund'].get_or_create_for_project(analytic)
                res['fund_id'] = fund.id
        return res

    _CEO_AMOUNT_FACTORS = {
        'full': 1.0, 'pct_75': 0.75, 'pct_50': 0.50, 'pct_25': 0.25,
    }

    @api.model_create_multi
    def create(self, vals_list):
        Analytic = self.env['account.analytic.account']
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                # Resolve analytic: related field not in vals, so browse fund
                analytic_id = vals.get('project_analytic_account_id')
                if not analytic_id and vals.get('fund_id'):
                    fund = self.env['x.petty.cash.fund'].browse(vals['fund_id'])
                    analytic_id = fund.project_analytic_account_id.id
                site_code = Analytic._matracon_site_code_for_id(analytic_id)
                vals['name'] = Analytic._matracon_ref_with_site(
                    'x.petty.cash.request', site_code)
            if not vals.get('fund_id') and vals.get('project_analytic_account_id'):
                fund = self.env['x.petty.cash.fund'].get_or_create_for_project(
                    self.env['account.analytic.account'].browse(
                        vals['project_analytic_account_id']))
                vals['fund_id'] = fund.id
            # Auto-fill CEO approved amount when type is not manual and amount not explicitly set
            amount_type = vals.get('ceo_amount_type', 'full')
            if amount_type != 'manual' and not vals.get('ceo_approved_amount'):
                factor = self._CEO_AMOUNT_FACTORS.get(amount_type, 1.0)
                vals['ceo_approved_amount'] = (vals.get('requested_amount') or 0.0) * factor
        return super().create(vals_list)

    @api.onchange('ceo_amount_type', 'requested_amount')
    def _onchange_ceo_amount_type(self):
        """Form-view live update: recalculate amount when type or requested amount changes."""
        if self.ceo_amount_type and self.ceo_amount_type != 'manual':
            self.ceo_approved_amount = (
                self.requested_amount * self._CEO_AMOUNT_FACTORS.get(self.ceo_amount_type, 1.0)
            )

    @api.onchange('ceo_approved_amount')
    def _onchange_ceo_approved_amount(self):
        """Form-view: auto-switch type to Manual when CEO edits the amount directly."""
        if not self.ceo_amount_type or self.ceo_amount_type == 'manual':
            return
        expected = self.requested_amount * self._CEO_AMOUNT_FACTORS.get(self.ceo_amount_type, 1.0)
        if self.ceo_approved_amount != expected:
            self.ceo_amount_type = 'manual'

    def write(self, vals):
        """
        Sync ceo_approved_amount ↔ ceo_amount_type on every write.

        @api.onchange only fires in the form view (client-side). For list-view
        multi-edit writes we mirror the same logic here so the two fields always
        stay consistent regardless of how they are changed.

        Rules:
        A) Type changes to non-manual AND amount NOT explicitly in same write
           → compute amount = requested_amount × factor  (per record, since
             requested_amount differs per record)
        B) Amount changes explicitly AND type NOT in same write AND current
           type is non-manual → auto-switch type to 'manual' (so the next
           form-view open doesn't silently re-calculate over the custom value)
        """
        new_type = vals.get('ceo_amount_type')
        has_amount = 'ceo_approved_amount' in vals

        # Rule A: type changes to a percentage/full → recompute amount per record
        if new_type and new_type != 'manual' and not has_amount:
            factor = self._CEO_AMOUNT_FACTORS.get(new_type, 1.0)
            for rec in self:
                req = vals.get('requested_amount', rec.requested_amount) or 0.0
                super(PettyCashRequest, rec).write(
                    dict(vals, ceo_approved_amount=round(req * factor, 2))
                )
            return True

        # Rule B: amount edited directly (no type in same write) → mark as manual
        if has_amount and not new_type:
            manual_recs = self.filtered(
                lambda r: r.ceo_amount_type and r.ceo_amount_type != 'manual'
            )
            if manual_recs:
                super(PettyCashRequest, manual_recs).write(
                    dict(vals, ceo_amount_type='manual')
                )
                rest = self - manual_recs
                if rest:
                    super(PettyCashRequest, rest).write(vals)
                return True

        return super().write(vals)

    def action_ceo_approve(self):
        for req in self:
            if req.state != 'submitted':
                raise UserError(_('Only submitted requests can be CEO-approved.'))
            if not req.ceo_approved_amount or req.ceo_approved_amount <= 0:
                raise UserError(_('Set the CEO approved amount before approving.'))
            req._do_ceo_approve()

    def _do_ceo_approve(self):
        """Core approval logic — called by both single and bulk approve."""
        self.ensure_one()
        self.state = 'ceo_approved'
        self.message_post(
            body=Markup(_(
                'CEO approved petty cash: <b>%s %.2f</b>'
            )) % (self.currency_id.symbol, self.ceo_approved_amount)
        )
        fo_users = self.env['res.users'].search([
            ('group_ids', 'in', self.env.ref('site_operations.group_finance_ho').id),
        ])
        matracon_notify.notify_users(
            self, fo_users,
            _('Petty cash <b>%s</b> CEO-approved — release required.') % self.name,
            summary=_('Petty Cash Approved'),
        )
        matracon_notify.schedule_activity(
            self, fo_users, _('Release petty cash %s') % self.name)

    def action_ceo_bulk_approve(self):
        """Bulk CEO approve — called from the list-view server action.

        Silently skips records that are not in 'submitted' state so the CEO
        can select all and click once without worrying about mixed states.
        Defaults ceo_approved_amount to requested_amount when not set.
        """
        if not self.env.user.has_group('purchase_demand_raise.group_ceo_approval'):
            raise UserError(_('Only the CEO can approve petty cash requests.'))
        approved = self.env['x.petty.cash.request']
        skipped = []
        for req in self:
            if req.state != 'submitted':
                skipped.append(req.name)
                continue
            # Default approved amount to requested amount if CEO hasn't entered one
            if not req.ceo_approved_amount or req.ceo_approved_amount <= 0:
                req.ceo_approved_amount = req.requested_amount
            req._do_ceo_approve()
            approved |= req

        if not approved and not skipped:
            return
        msg_parts = []
        if approved:
            msg_parts.append(
                _('%d request(s) approved.') % len(approved)
            )
        if skipped:
            msg_parts.append(
                _('%d skipped (not in Submitted state): %s') % (
                    len(skipped), ', '.join(skipped))
            )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('CEO Approval'),
                'message': '  '.join(msg_parts),
                'type': 'warning' if skipped else 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

    def action_submit(self):
        for req in self:
            if req.requested_amount <= 0:
                raise UserError(_('Enter a valid requested amount.'))
            if not req.x_pm_signed_document:
                raise UserError(_(
                    'A PM-signed document is required before submitting.\n'
                    'Please print this request, get it signed by the Project Manager, '
                    'scan it, and upload it in the "PM Signed Document" field.'
                ))
            req.state = 'submitted'
            req.message_post(body=_('Petty cash request submitted to Finance HO.'))
            fo_users = self.env['res.users'].search([
                ('group_ids', 'in', self.env.ref(
                    'site_operations.group_finance_ho').id),
            ])
            matracon_notify.notify_users(
                req, fo_users,
                _('Petty cash request <b>%s</b> — release required.') % req.name,
                summary=_('Petty Cash Request'),
            )
            matracon_notify.schedule_activity(
                req, fo_users, _('Release petty cash %s') % req.name)
            ceo_users = self.env['res.users'].search([
                ('group_ids', 'in', self.env.ref('purchase_demand_raise.group_ceo_approval').id),
            ])
            matracon_notify.notify_users(
                req, ceo_users,
                _('Petty cash request <b>%s</b> — CEO approval required.') % req.name,
                summary=_('Petty Cash Request'),
            )
            matracon_notify.schedule_activity(
                req, ceo_users, _('Approve petty cash %s') % req.name)

    def action_reset_to_draft(self):
        """Reset PCR back to Draft.

        Rules:
        • Site Accountant — can reset only when state == 'submitted'
          (CEO has not yet approved). Once CEO approves, Finance HO is in
          control and the site cannot reverse.
        • CEO / Matracon Admin — can reset from 'submitted' or 'ceo_approved'.
          Cannot reset 'released' or 'confirmed' (a payment has been made).
        """
        is_ceo   = self.env.user.has_group('purchase_demand_raise.group_ceo_approval')
        is_admin = self.env.user.has_group('purchase_demand_raise.group_matracon_admin')
        is_privileged = is_ceo or is_admin

        for req in self:
            if req.state in ('released', 'confirmed'):
                raise UserError(_(
                    '%(name)s has already been released. '
                    'A payment has been made — it cannot be reset to draft.',
                    name=req.name,
                ))
            if not is_privileged and req.state != 'submitted':
                raise UserError(_(
                    '%(name)s cannot be reset to draft. '
                    'CEO has already approved it — contact Finance HO.',
                    name=req.name,
                ))
            req.write({
                'state': 'draft',
                'ceo_approved_amount': 0.0,
            })
            req.message_post(
                body=Markup(
                    '↩️ Petty cash request reset to <b>Draft</b> by <b>%(user)s</b>.'
                ) % {'user': self.env.user.name},
                subtype_xmlid='mail.mt_log_note',
            )

    def action_release(self):
        """Finance HO releases petty cash via payment workflow.

        The payment's destination account is set to the site's dedicated
        petty cash account (Cash in Hand — Project) so the journal entry
        correctly debits that account instead of the generic AP account.

        NOTE: destination_account_id is a computed field that Odoo re-computes
        whenever the payment is edited (journal, method, etc.) — setting it here
        is a convenience hint for the Finance HO form view.  The authoritative
        override happens in _prepare_move_line_default_vals() at posting time.
        """
        self.ensure_one()
        if self.state != 'ceo_approved':
            raise UserError(_('Only CEO-approved requests can be released.'))
        Payment = self.env['account.payment']
        analytic = self.project_analytic_account_id

        # Resolve site petty cash account — REQUIRED before releasing
        petty_cash_account = self.fund_id._get_petty_cash_account()
        if not petty_cash_account:
            site_name = (analytic.name or self.fund_id.name or 'this site')
            raise UserError(_(
                'No Petty Cash Account (Cash in Hand) is configured for "%s".\n\n'
                'Please go to:\n'
                '  Configuration → Site Configurations → %s\n'
                'and set the "Petty Cash Account (Cash in Hand)" field, '
                'then try releasing again.'
            ) % (site_name, site_name))

        payment_vals = {
            'payment_type': 'outbound',
            'partner_type': 'supplier',
            'amount': self.ceo_approved_amount or self.requested_amount,
            'x_gross_approved_amount': self.ceo_approved_amount or self.requested_amount,
            'x_petty_cash_request_id': self.id,
            'x_payment_category': 'petty_cash',
            'x_ceo_approval_state': 'approved',
            'x_destination_project_id': analytic.id if analytic else False,
        }
        payment = Payment.create(payment_vals)

        # Set destination_account_id as a UI hint so Finance HO sees the correct
        # account pre-filled on the payment form.  This may be overwritten if the
        # user changes the journal/method — but _prepare_move_line_default_vals()
        # enforces the correct account at posting time regardless.
        payment.destination_account_id = petty_cash_account.id

        self.payment_id = payment.id
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.payment',
            'view_mode': 'form',
            'res_id': payment.id,
            'context': {'default_x_petty_cash_request_id': self.id},
        }

    def action_view_fund(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Petty Cash Fund'),
            'res_model': 'x.petty.cash.fund',
            'view_mode': 'form',
            'res_id': self.fund_id.id,
            'target': 'new',
        }

    def action_confirm_receipt(self):
        for req in self:
            if req.state != 'released':
                raise UserError(_('Finance HO must release funds first.'))
            req.state = 'confirmed'
            req.message_post(
                body=Markup(_('Receipt confirmed by Site Accountant <b>%s</b>.'))
                % self.env.user.name)

    def action_mark_released(self, amount=None):
        """Called when FO payment is posted."""
        for req in self:
            req.released_amount = amount or req.requested_amount
            req.state = 'released'
            req.message_post(
                body=Markup(_('Petty cash <b>%s</b> released by Finance HO.'))
                % f'{req.released_amount:,.2f}')

    def action_fix_petty_cash_entry(self):
        """Create a correcting journal entry when the release payment JE hit the
        wrong account (AP instead of Cash in Hand).

        This happens when the petty cash account was not configured on the site
        before the payment was posted, or when Odoo recomputed destination_account_id
        after action_release() set it.

        The correction:
          Dr  Cash in Hand (petty cash account)   ← correct account
          Cr  Wrong account (whatever was debited) ← reversal
        """
        self.ensure_one()
        if not self.payment_id:
            raise UserError(_('No payment is linked to this request.'))
        if self.payment_id.state not in ('in_process', 'paid', 'partial', 'posted'):
            raise UserError(_('The payment must be posted before a correction can be applied.'))
        if not self.payment_id.move_id:
            raise UserError(_(
                'The payment has no journal entry (move_id is empty). '
                'Nothing to correct.'
            ))

        pc_account = self.fund_id._get_petty_cash_account()
        if not pc_account:
            raise UserError(_(
                'No Petty Cash Account (Cash in Hand) is configured for this site.\n\n'
                'Please set it in Configuration → Site Configurations first, '
                'then run this correction.'
            ))

        move = self.payment_id.move_id

        # Identify the bank/outstanding line — it is the one using the journal's
        # default account or the outstanding_account_id.
        bank_account = (
            self.payment_id.outstanding_account_id
            or self.payment_id.journal_id.default_account_id
        )
        bank_account_id = bank_account.id if bank_account else None

        # The counterpart line is everything that is not the bank/cash line and
        # is not already the correct petty cash account.
        wrong_lines = move.line_ids.filtered(
            lambda l: l.account_id.id != bank_account_id
                      and l.account_id.id != pc_account.id
        )

        if not wrong_lines:
            # Either already correct or we can't identify the counterpart
            already_correct = move.line_ids.filtered(
                lambda l: l.account_id.id == pc_account.id
            )
            if already_correct:
                raise UserError(_(
                    'The payment journal entry already uses "%s". No correction needed.'
                ) % pc_account.display_name)
            raise UserError(_(
                'Cannot identify the counterpart line in the payment journal entry. '
                'Please correct it manually via Accounting → Journal Entries.'
            ))

        # Use the total balance of all wrong counterpart lines as the correction amount
        correction_amount = sum(abs(l.debit - l.credit) for l in wrong_lines)
        if correction_amount <= 0:
            raise UserError(_('Counterpart line balance is zero — nothing to correct.'))

        wrong_account = wrong_lines[0].account_id

        # Build correcting entry in a general journal
        journal = self.env['account.journal'].search([
            ('type', '=', 'general'),
            ('company_id', '=', self.env.company.id),
        ], limit=1)
        if not journal:
            journal = self.payment_id.journal_id

        analytic_distribution = {}
        if self.project_analytic_account_id:
            analytic_distribution = {str(self.project_analytic_account_id.id): 100}

        correcting_move = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': journal.id,
            'date': self.payment_id.date or fields.Date.today(),
            'ref': _('Correction: petty cash account for %s') % self.name,
            'x_project_analytic_account_id': (
                self.project_analytic_account_id.id
                if self.project_analytic_account_id else False
            ),
            'line_ids': [
                # Debit the correct Cash in Hand (petty cash) account
                (0, 0, {
                    'name': _('Correct: Cash in Hand — %s') % (
                        self.project_analytic_account_id.name or self.name),
                    'account_id': pc_account.id,
                    'debit': correction_amount,
                    'credit': 0.0,
                    'analytic_distribution': analytic_distribution or False,
                }),
                # Credit (reverse) the wrong account that was incorrectly debited
                (0, 0, {
                    'name': _('Reverse wrong account — %s') % self.name,
                    'account_id': wrong_account.id,
                    'debit': 0.0,
                    'credit': correction_amount,
                    'analytic_distribution': analytic_distribution or False,
                }),
            ],
        })
        correcting_move.action_post()

        self.message_post(body=Markup(_(
            'Correcting journal entry <b>%(move)s</b> posted.<br/>'
            'Amount: <b>%(amount)s</b><br/>'
            'Wrong account reversed: %(wrong)s<br/>'
            'Correct account debited: %(correct)s'
        )) % {
            'move': correcting_move.name,
            'amount': f'{correction_amount:,.2f}',
            'wrong': wrong_account.display_name,
            'correct': pc_account.display_name,
        })

        return {
            'type': 'ir.actions.act_window',
            'name': _('Correcting Entry'),
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': correcting_move.id,
        }

    def action_print_petty_cash_request(self):
        return self.env.ref(
            'site_operations.action_report_petty_cash_request'
        ).report_action(self)

    def action_direct_print_pcr(self):
        """Open the Petty Cash Request report in a new tab and auto-trigger print dialog."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'url': f'/site_operations/print/x.petty.cash.request/{self.id}',
            'target': 'new',
        }

    def unlink(self):
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_('Only draft petty cash requests can be deleted.'))
        return super().unlink()

    def action_delete_draft(self):
        self.unlink()
        return {'type': 'ir.actions.act_window_close'}


class PettyCashExpense(models.Model):
    _name = 'x.petty.cash.expense'
    _description = 'Petty Cash Expense'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'expense_date desc, id desc'

    # ── Identity ─────────────────────────────────────────────────────────────
    name = fields.Char(
        string='Description',
        required=True,
        help='What this expense is for (user-entered).',
    )
    x_ref = fields.Char(
        string='Reference',
        copy=False,
        help='System reference — auto-filled from source document (e.g. Site Procurement receipt). '
             'Enter manually for expenses created directly.',
    )

    # ── Fund / Project ────────────────────────────────────────────────────────
    fund_id = fields.Many2one(
        'x.petty.cash.fund', required=True, ondelete='restrict', index=True)
    project_analytic_account_id = fields.Many2one(
        related='fund_id.project_analytic_account_id', store=True, readonly=True)
    expense_date = fields.Date(
        default=fields.Date.context_today, required=True)
    amount = fields.Monetary(required=True, currency_field='currency_id')
    available_balance = fields.Monetary(
        related='fund_id.balance', string='Available Balance',
        currency_field='currency_id', readonly=True,
    )

    # ── Running balance (set at posting time) ─────────────────────────────────
    x_balance_before = fields.Monetary(
        string='Balance Before',
        currency_field='currency_id',
        readonly=True, copy=False,
        help='Fund balance immediately before this expense was posted.',
    )
    x_balance_after = fields.Monetary(
        string='Balance After',
        currency_field='currency_id',
        readonly=True, copy=False,
        help='Fund balance immediately after this expense was posted.',
    )

    category = fields.Selection([
        ('travel', 'Travel'),
        ('supplies', 'Supplies'),
        ('utilities', 'Utilities'),
        ('meals', 'Meals'),
        ('other', 'Other'),
    ], default='other', required=True)

    # ── Accounting ────────────────────────────────────────────────────────────
    x_petty_cash_account_id = fields.Many2one(
        'account.account',
        string='Petty Cash Account (Credit)',
        help='Cash in Hand account for this site. Credited when the expense is posted. '
             'Auto-filled from the project\'s petty cash fund configuration.',
    )
    journal_id = fields.Many2one(
        'account.journal',
        string='Cash Journal',
        domain=[('type', '=', 'cash')],
        help='Cash journal for this expense entry. Auto-filled from the project.',
    )
    expense_account_id = fields.Many2one(
        'account.account',
        string='Expense Account (Debit)',
        help='GL account to debit when posting this expense. '
             'Leave blank to use the category-based default.',
    )

    # ── Employee Advance ─────────────────────────────────────────────────────
    is_employee_advance = fields.Boolean(
        string='Employee Advance',
        default=False,
        help='Enable to record this petty cash payment as an advance to an employee.')
    employee_id = fields.Many2one(
        'hr.employee', string='Employee',
        domain="[('x_project_analytic_account_id', '=', project_analytic_account_id)]",
        help='Employee receiving this advance.')
    employee_advance_balance = fields.Monetary(
        string='Current Advance Balance',
        related='employee_id.x_advance_balance',
        currency_field='currency_id', readonly=True)

    # ── Subcontractor Advance ────────────────────────────────────────────────
    is_subcontractor_advance = fields.Boolean(
        string='Subcontractor Advance',
        default=False,
        help='Enable to record this petty cash payment as an advance to a subcontractor. '
             'Creates a debit entry on the selected payable account for the subcontractor — '
             'appears in the partner ledger and is tracked in IPC payment summaries.')
    advance_subcontractor_id = fields.Many2one(
        'res.partner', string='Subcontractor',
        domain="[('category_id.name', '=', 'Subcontractor')]",
        help='Subcontractor receiving this advance.')
    advance_payable_account_id = fields.Many2one(
        'account.account',
        string='Payable Account',
        domain="[('account_type', '=', 'liability_payable')]",
        help='Payable account to debit (e.g. Payable to Subcontractors). '
             'This entry appears in the partner ledger and IPC payment tracking.')

    # ── Link to the journal entry created on posting ──────────────────────────
    x_account_move_id = fields.Many2one(
        'account.move', string='Journal Entry',
        copy=False, readonly=True, ondelete='set null',
        help='Journal entry created when this expense is posted. '
             'Used by the IPC payment query to avoid double-counting.')

    receipt = fields.Binary(string='Receipt / Voucher')
    receipt_filename = fields.Char()

    # Signed voucher — mandatory upload before posting.
    # Workflow: accountant prints the expense voucher, gets it physically signed,
    # scans it, and uploads here.  action_post() enforces this.
    x_signed_voucher = fields.Binary(
        string='Signed Voucher',
        attachment=True,
    )
    x_signed_voucher_filename = fields.Char(string='Signed Voucher Filename')

    state = fields.Selection([
        ('draft', 'Draft'),
        ('posted', 'Posted'),
    ], default='draft', tracking=True)
    currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id)

    # ── Traceability ──────────────────────────────────────────────────────────
    x_purchase_order_id = fields.Many2one(
        'purchase.order',
        string='Purchase Requisition',
        readonly=True, copy=False, index=True,
    )
    x_picking_id = fields.Many2one(
        'stock.picking',
        string='Material Receipt',
        readonly=True, copy=False,
    )

    # ── Defaults ──────────────────────────────────────────────────────────────

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if 'fund_id' in fields_list and not res.get('fund_id'):
            analytic = self.env.user.sudo().x_default_analytic_account_id
            if analytic:
                fund = self.env['x.petty.cash.fund'].sudo().get_or_create_for_project(analytic)
                if fund:
                    res['fund_id'] = fund.id
                    if 'x_petty_cash_account_id' in fields_list:
                        pc_account = fund._get_petty_cash_account()
                        if pc_account:
                            res['x_petty_cash_account_id'] = pc_account.id
        return res

    # ── Onchanges ─────────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        """Auto-generate x_ref with site-based prefix if not already provided.

        Also auto-fills x_petty_cash_account_id from the fund/site config when
        the value was not sent by the client (Odoo does not submit readonly field
        values from the form, so default_get alone is insufficient).
        """
        Analytic = self.env['account.analytic.account']
        for vals in vals_list:
            if not vals.get('x_ref'):
                analytic_id = vals.get('project_analytic_account_id')
                if not analytic_id and vals.get('fund_id'):
                    fund = self.env['x.petty.cash.fund'].browse(vals['fund_id'])
                    analytic_id = fund.project_analytic_account_id.id
                site_code = Analytic._matracon_site_code_for_id(analytic_id)
                vals['x_ref'] = Analytic._matracon_ref_with_site(
                    'x.petty.cash.expense', site_code)
            # Ensure x_petty_cash_account_id is always populated from the fund.
            # readonly fields are NOT sent by the browser on save, so the value
            # set by default_get is lost unless we re-fill it here.
            if not vals.get('x_petty_cash_account_id') and vals.get('fund_id'):
                fund = self.env['x.petty.cash.fund'].browse(vals['fund_id'])
                pc_acct = fund._get_petty_cash_account()
                if pc_acct:
                    vals['x_petty_cash_account_id'] = pc_acct.id
        return super().create(vals_list)

    @api.onchange('is_subcontractor_advance', 'advance_subcontractor_id')
    def _onchange_sub_advance_fill_account(self):
        """Auto-fill expense_account_id from the subcontractor's default payable account
        when the subcontractor advance flag is toggled on or the subcontractor changes.
        The debit on that account with partner = subcontractor makes the entry appear
        in the partner ledger and be counted in IPC payment tracking.
        """
        if not self.is_subcontractor_advance:
            return
        # Use the subcontractor's configured payable account from their partner record
        if self.advance_subcontractor_id and self.advance_subcontractor_id.property_account_payable_id:
            self.expense_account_id = self.advance_subcontractor_id.property_account_payable_id

    @api.onchange('fund_id')
    def _onchange_fund_fill_accounting(self):
        """Auto-fill petty cash account and cash journal when fund changes."""
        if not self.fund_id:
            return
        # Petty cash account from fund/site config
        pc_account = self.fund_id._get_petty_cash_account()
        if pc_account:
            self.x_petty_cash_account_id = pc_account

        # Cash journal from site's linked journal
        analytic = self.project_analytic_account_id
        if analytic and not self.journal_id:
            journal = self.env['account.journal'].search([
                ('type', '=', 'cash'),
                ('x_site_ids', 'in', [analytic.id]),
                ('company_id', '=', self.env.company.id),
            ], limit=1)
            if journal:
                self.journal_id = journal

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_post(self):
        for expense in self:
            if expense.amount <= 0:
                raise UserError(_('Expense amount must be positive.'))
            if expense.is_employee_advance and not expense.employee_id:
                raise UserError(_(
                    'Please select an Employee before posting this advance.'
                ))
            if expense.is_subcontractor_advance and not expense.advance_subcontractor_id:
                raise UserError(_(
                    'Please select a Subcontractor before posting this advance.'
                ))
            if expense.is_subcontractor_advance and not expense.expense_account_id:
                raise UserError(_(
                    'Please select an Expense Account for this subcontractor advance.'
                ))
            if not expense.x_signed_voucher:
                raise UserError(_(
                    'A signed voucher is required before posting.\n\n'
                    'Please:\n'
                    '1. Print this expense voucher (Print → Petty Cash Expense Voucher)\n'
                    '2. Obtain physical signatures from all parties\n'
                    '3. Scan the signed document and upload it in the '
                    '"Signed Voucher" field above\n'
                    '4. Then click Post again.'
                ))
            balance_before = expense.fund_id.balance
            if balance_before < expense.amount - 0.01:
                raise UserError(_(
                    'Insufficient petty cash balance (available: %s %.2f).'
                ) % (expense.currency_id.symbol, balance_before))

            expense.state = 'posted'
            expense._create_journal_entry()

            balance_after = balance_before - expense.amount
            # Store the running balance snapshot
            expense.write({
                'x_balance_before': balance_before,
                'x_balance_after': balance_after,
            })

            # Update employee outstanding advance balance
            if expense.is_employee_advance and expense.employee_id:
                emp = expense.employee_id.sudo()
                prev_balance = emp.x_advance_balance or 0.0
                emp.x_advance_balance = prev_balance + expense.amount
                expense.message_post(body=Markup(_(
                    'Employee Advance posted for <b>%(emp)s</b>: '
                    '<b>+%(sym)s %(amount)s</b>. '
                    'Outstanding balance: %(sym)s %(prev)s → '
                    '<b>%(sym)s %(new)s</b>'
                )) % {
                    'emp': emp.name,
                    'sym': expense.currency_id.symbol,
                    'amount': f'{expense.amount:,.2f}',
                    'prev': f'{prev_balance:,.2f}',
                    'new': f'{emp.x_advance_balance:,.2f}',
                })

            sym = expense.currency_id.symbol
            advance_note = ''
            if expense.is_employee_advance and expense.employee_id:
                advance_note = f'<br/><b>Employee Advance:</b> {expense.employee_id.name}'
            elif expense.is_subcontractor_advance and expense.advance_subcontractor_id:
                advance_note = (
                    f'<br/><b>Subcontractor Advance:</b> {expense.advance_subcontractor_id.name}'
                    + (f' ({expense.expense_account_id.display_name})' if expense.expense_account_id else '')
                )
            body = Markup(
                '<b>Expense Posted</b><br/>'
                '<b>Reference:</b> {ref}<br/>'
                '<b>Description:</b> {name}'
                '{advance_note}<br/>'
                '<b>Amount:</b> {sym} {amount}<br/>'
                '<b>Balance Before:</b> {sym} {before}<br/>'
                '<b>Balance After:</b> {sym} {after}'
            ).format(
                ref=expense.x_ref or '—',
                name=expense.name,
                advance_note=advance_note,
                sym=sym,
                amount=f'{expense.amount:,.2f}',
                before=f'{balance_before:,.2f}',
                after=f'{balance_after:,.2f}',
            )

            # Attach receipt to the chatter message if present
            attachment_ids = []
            if expense.receipt:
                attachment = self.env['ir.attachment'].sudo().create({
                    'name': expense.receipt_filename or 'receipt',
                    'datas': expense.receipt,
                    'res_model': 'x.petty.cash.expense',
                    'res_id': expense.id,
                    'mimetype': 'application/octet-stream',
                })
                attachment_ids = [attachment.id]

            expense.message_post(body=body, attachment_ids=attachment_ids)

    def _create_journal_entry(self):
        """Double-entry: Debit Expense / Credit Cash-in-Hand (petty cash account).

        Credit side priority:
          1. x_petty_cash_account_id on the expense (from site config)
          2. journal_id.default_account_id (cash journal default)
          3. Any cash journal (fallback)

        Debit side:
          1. expense_account_id (manually selected)
          2. Category-based default account code
        """
        self.ensure_one()

        # ── Credit: petty cash account (Cash in Hand for this site) ──────────
        credit_account = self.x_petty_cash_account_id

        if not credit_account:
            # Fall back to cash journal default account
            Journal = self.env['account.journal']
            cash_journal = self.journal_id
            if not cash_journal:
                analytic = self.project_analytic_account_id
                if analytic:
                    cash_journal = Journal.search([
                        ('type', '=', 'cash'),
                        ('x_site_ids', 'in', [analytic.id]),
                        ('company_id', '=', self.env.company.id),
                    ], limit=1)
            if not cash_journal:
                cash_journal = Journal.search([
                    ('type', '=', 'cash'),
                    ('company_id', '=', self.env.company.id),
                ], limit=1)
            if cash_journal:
                credit_account = cash_journal.default_account_id

        if not credit_account:
            return  # Cannot determine Cash-in-Hand account — skip silently

        # ── Journal for the entry ─────────────────────────────────────────────
        Journal = self.env['account.journal']
        cash_journal = self.journal_id
        if not cash_journal:
            analytic = self.project_analytic_account_id
            if analytic:
                cash_journal = Journal.search([
                    ('type', '=', 'cash'),
                    ('x_site_ids', 'in', [analytic.id]),
                    ('company_id', '=', self.env.company.id),
                ], limit=1)
        if not cash_journal:
            cash_journal = Journal.search([
                ('type', '=', 'cash'),
                ('company_id', '=', self.env.company.id),
            ], limit=1)

        # ── Debit: subcontractor advance → expense_account_id (payable) + partner ─
        # Stamping the subcontractor as partner on the debit line makes the entry
        # appear in the partner ledger AND ensures the IPC "Payments Made" query
        # (which aggregates x_petty_cash_expense by advance_subcontractor_id) picks
        # it up — regardless of which payable account is selected.
        debit_partner_id = False
        if self.is_subcontractor_advance and self.advance_subcontractor_id and self.expense_account_id:
            debit_account = self.expense_account_id
            debit_partner_id = self.advance_subcontractor_id.id
        else:
            debit_account = self.expense_account_id
            if not debit_account:
                CATEGORY_ACCOUNT_MAP = {
                    'travel': '6270',
                    'supplies': '6280',
                    'utilities': '6300',
                    'meals': '6290',
                    'other': '6290',
                }
                code = CATEGORY_ACCOUNT_MAP.get(self.category, '6290')
                debit_account = self.env['account.account'].search([
                    ('code', 'like', code),
                    ('company_id', '=', self.env.company.id),
                ], limit=1)
            if not debit_account:
                debit_account = credit_account

        analytic_distribution = {}
        if self.project_analytic_account_id:
            analytic_distribution = {
                str(self.project_analytic_account_id.id): 100
            }

        ref_label = self.x_ref or self.name
        move_vals = {
            'move_type': 'entry',
            'journal_id': cash_journal.id if cash_journal else False,
            'date': self.expense_date,
            'ref': _('Petty Cash: %s') % ref_label,
            'line_ids': [
                (0, 0, {
                    'name': self.name,
                    'account_id': debit_account.id,
                    'partner_id': debit_partner_id or False,
                    'debit': self.amount,
                    'credit': 0.0,
                    'analytic_distribution': analytic_distribution or False,
                }),
                (0, 0, {
                    'name': _('Cash in Hand — %s') % (
                        self.project_analytic_account_id.name or _('Petty Cash')),
                    'account_id': credit_account.id,
                    'debit': 0.0,
                    'credit': self.amount,
                    'analytic_distribution': analytic_distribution or False,
                }),
            ],
        }
        if not cash_journal:
            # Need at least some journal — skip if none
            return
        move = self.env['account.move'].create(move_vals)
        move.action_post()
        self.x_account_move_id = move

    def action_print_expense_voucher(self):
        return self.env.ref(
            'site_operations.action_report_petty_cash_expense'
        ).report_action(self)

    def action_direct_print_pce(self):
        """Open the Petty Cash Expense Voucher in a new tab and auto-trigger print dialog."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'url': f'/site_operations/print/x.petty.cash.expense/{self.id}',
            'target': 'new',
        }

    def unlink(self):
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_('Only draft petty cash expenses can be deleted.'))
        return super().unlink()

    @api.model
    def action_admin_fix_petty_cash_accounts(self):
        """Admin action: back-fill x_petty_cash_account_id on all posted expenses where
        it is NULL, and correct posted JEs whose credit account differs from the site's
        configured petty cash (Cash-in-Hand) account.

        Calls the same three-step function used by the post_migrate_hook so the fix is
        consistent with what runs on every module upgrade.  Safe to run repeatedly.

        Accessible via:
          - Petty Cash Expenses list view → Action → Fix Petty Cash Accounts (admins)
          - Settings → Technical → Server Actions → Fix Petty Cash Accounts
        """
        if not (self.env.user.has_group('purchase_demand_raise.group_matracon_admin')
                or self.env.user.has_group('base.group_system')):
            raise UserError(_('Only Matracon Admin or System Administrator can run this action.'))
        from odoo.addons.site_operations.hooks import fix_petty_cash_expense_accounts
        fix_petty_cash_expense_accounts(self.env)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Petty Cash Accounts Fixed'),
                'message': _(
                    'Done. All expense credit accounts have been filled from site '
                    'configuration, and posted JEs with the wrong cash account have '
                    'been corrected. Check the server log for details.'
                ),
                'type': 'success',
                'sticky': True,
            },
        }

    def action_delete_draft(self):
        self.unlink()
        return {'type': 'ir.actions.act_window_close'}

    def action_view_pr_from_expense(self):
        self.ensure_one()
        if not self.x_purchase_order_id:
            return
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'purchase.order',
            'view_mode': 'form',
            'res_id': self.x_purchase_order_id.id,
        }

    def action_view_picking_from_expense(self):
        self.ensure_one()
        if not self.x_picking_id:
            return
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'view_mode': 'form',
            'res_id': self.x_picking_id.id,
        }

    @api.model
    def _matracon_create_from_po_receipt(self, picking):
        """Create a Draft Petty Cash Expense when a Site Procurement receipt is validated.

        The x_ref field gets the auto-generated source reference
        ("Site Procurement — MCH|N00003") so the site accountant can see
        where it came from.  The 'name' (Description) is intentionally left
        blank so the accountant must fill in what the expense was for before
        posting.
        """
        po = picking.purchase_id
        analytic = po.x_project_analytic_account_id
        if not analytic:
            picking.message_post(
                body=_('Site Procurement receipt validated but no project analytic account '
                       'found on PR %s — petty cash expense NOT created.') % po.name)
            return self.env['x.petty.cash.expense']

        fund = self.env['x.petty.cash.fund'].sudo().get_or_create_for_project(analytic)
        if not fund:
            return self.env['x.petty.cash.expense']

        total = sum(
            move.quantity * (move.product_id.standard_price or 0.0)
            for move in picking.move_ids.filtered(
                lambda m: m.product_id and m.quantity > 0
            )
        )

        # Auto-select petty cash account from fund/site config
        pc_account = fund._get_petty_cash_account()

        # Auto-select cash journal
        cash_journal = self.env['account.journal'].search([
            ('type', '=', 'cash'),
            ('x_site_ids', 'in', [analytic.id]),
            ('company_id', '=', self.env.company.id),
        ], limit=1)

        expense = self.sudo().create({
            # x_ref = source reference; name = description (filled by accountant)
            'x_ref': _('Site Procurement — %s') % picking.name,
            'name': _('Site Procurement — %s') % po.name,  # default, accountant can edit
            'fund_id': fund.id,
            'expense_date': fields.Date.context_today(self),
            'amount': total or 0.0,
            'category': 'other',
            'journal_id': cash_journal.id if cash_journal else False,
            'x_petty_cash_account_id': pc_account.id if pc_account else False,
            'x_purchase_order_id': po.id,
            'x_picking_id': picking.id,
            'state': 'draft',
        })

        accountants = matracon_notify.site_accountants_for_analytic(self.env, analytic)
        matracon_notify.notify_users(
            expense, accountants,
            _('Draft petty cash expense <b>%(exp)s</b> created from site procurement '
              'receipt <b>%(grn)s</b>. Please update the description, select the '
              'expense account, and post.')
            % {'exp': expense.x_ref, 'grn': picking.name},
            summary=_('Petty Cash Expense Ready'),
        )
        matracon_notify.schedule_activity(
            expense, accountants,
            _('Post petty cash expense for %s') % po.name,
            note=_('Receipt %s validated — petty cash expense draft created.') % picking.name,
        )
        picking.sudo().message_post(
            body=Markup(_(
                'Draft petty cash expense <b>%(exp)s</b> created for site accountant review.'
            )) % {'exp': expense.x_ref}
        )
        return expense


# ═══════════════════════════════════════════════════════════════════════════
# Admin Maintenance Wizard
# ═══════════════════════════════════════════════════════════════════════════

class XPettyCashAdminWizard(models.TransientModel):
    """Admin maintenance wizard — run the 3-step petty cash fix on demand.

    Accessible via:
      Accounting → Petty Cash → Configuration → Fix Petty Cash Accounts

    This runs the exact same function as the post_migrate_hook, so it is
    safe to execute multiple times.  Each step is idempotent:
      Step 1 — fills x_petty_cash_account_id on expenses where it is NULL
      Step 2 — creates missing journal entries for posted expenses
      Step 3 — corrects posted JEs whose credit account ≠ the site petty
               cash (Cash-in-Hand) account (in-place SQL, no reversal entries)
    """
    _name = 'x.petty.cash.admin.wizard'
    _description = 'Petty Cash Admin Maintenance'

    def action_run_fix(self):
        """Run the full 3-step petty cash account fix."""
        if not (self.env.user.has_group('purchase_demand_raise.group_matracon_admin')
                or self.env.user.has_group('base.group_system')):
            raise UserError(_('Only Matracon Admin or System Administrator can run this action.'))
        from odoo.addons.site_operations.hooks import fix_petty_cash_expense_accounts
        fix_petty_cash_expense_accounts(self.env)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Petty Cash Fix Complete'),
                'message': _(
                    'Done. All expense credit accounts have been filled from site '
                    'configuration, missing journal entries have been created, and '
                    'posted JEs with the wrong cash account have been corrected '
                    'in-place. Check the server log for a detailed report.'
                ),
                'type': 'success',
                'sticky': True,
            },
        }
