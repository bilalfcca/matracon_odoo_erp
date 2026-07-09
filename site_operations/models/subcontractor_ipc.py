"""Subcontractor Interim Payment Certificate — cumulative billing workflow."""

from dateutil.relativedelta import relativedelta
from markupsafe import Markup

from odoo import models, fields, api, _
from odoo.exceptions import UserError

class SubcontractorIPCBackchargeLine(models.Model):
    _name = 'x.subcontractor.ipc.backcharge.line'
    _description = 'IPC Back Charge Line'
    _order = 'date asc, id asc'

    ipc_id = fields.Many2one(
        'x.subcontractor.ipc', string='IPC',
        ondelete='cascade', required=True, index=True)
    backcharge_id = fields.Many2one(
        'x.subcontractor.backcharge', string='Back Charge',
        required=True)
    description = fields.Char(
        string='Description', related='backcharge_id.description')
    date = fields.Date(
        string='Date', related='backcharge_id.date')
    amount = fields.Monetary(
        string='Amount',
        compute='_compute_backcharge_amount', store=True,
        currency_field='currency_id')
    currency_id = fields.Many2one(
        'res.currency', related='ipc_id.currency_id', store=True)

    @api.depends('backcharge_id', 'backcharge_id.amount')
    def _compute_backcharge_amount(self):
        for line in self:
            line.amount = line.backcharge_id.amount if line.backcharge_id else 0.0


class SubcontractorIPC(models.Model):
    _name = 'x.subcontractor.ipc'
    _description = 'Subcontractor Interim Payment Certificate'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'ipc_date desc, id desc'

    # ── Identifiers ───────────────────────────────────────────────────────────
    name = fields.Char(
        string='IPC Reference', readonly=True,
        default=lambda self: _('New'), copy=False)
    ipc_number = fields.Integer(
        string='IPC No.', required=True, tracking=True,
        help='Sequential IPC number for this subcontractor on this project '
             '(e.g. 1, 2, 3). Enter manually — specific to each sub+project combination.')
    subcontractor_id = fields.Many2one(
        'res.partner', string='Subcontractor',
        required=True,
        domain=[('category_id.name', '=', 'Subcontractor')],
        tracking=True)
    project_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Project',
        required=True, tracking=True,
        default=lambda self: self.env.user.sudo().x_default_analytic_account_id)
    ipc_date = fields.Date(
        string='IPC Date', default=fields.Date.context_today, required=True)
    currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),
        ('paid', 'Paid'),
    ], default='draft', tracking=True, string='Status')

    # ── Retention percentage (default 5%, editable per IPC) ──────────────────
    retention_pct = fields.Float(
        string='Retention %', default=5.0, digits=(5, 2), tracking=True,
        help='Retention percentage applied to gross work done. Default 5%.')

    # ── IPC Document (mandatory before submission) ────────────────────────────
    ipc_document = fields.Binary(
        string='IPC Document', attachment=True, copy=False,
        help='Signed/stamped IPC document — mandatory before submission.')
    ipc_document_filename = fields.Char(string='IPC Document Filename')

    # ── Previous IPC data (auto, same subcontractor + same project only) ──────
    previous_ipc_id = fields.Many2one(
        'x.subcontractor.ipc', string='Previous IPC',
        compute='_compute_previous_ipc', store=True, readonly=True,
        help='Most recently submitted/paid IPC for this subcontractor on this project.')
    previous_gross_work_done = fields.Monetary(
        string='Work Done up to Previous IPC',
        compute='_compute_previous_ipc', store=True,
        currency_field='currency_id', readonly=True,
        help='Cumulative gross work value from the previous submitted IPC.')
    ipc_date_from = fields.Date(
        string='Period From',
        compute='_compute_previous_ipc', store=True,
        help='Auto-computed: one day after the previous IPC date.')

    # ── Work amounts ──────────────────────────────────────────────────────────
    gross_work_done = fields.Monetary(
        string='Gross Work Done (Cumulative)',
        required=True, currency_field='currency_id', tracking=True,
        help='Total cumulative value of work done from project start up to and '
             'including this IPC. Enter the running total, not just this period.')
    this_ipc_gross = fields.Monetary(
        string='Work Done for this IPC',
        compute='_compute_deductions', store=True,
        currency_field='currency_id',
        help='Auto: Gross Work Done − Work Done up to Previous IPC.')

    # ── Retention (fixed at 5%) ───────────────────────────────────────────────
    total_retention_cumulative = fields.Monetary(
        string='Total Retention @5% (cumulative)',
        compute='_compute_deductions', store=True,
        currency_field='currency_id',
        help='Auto: 5% × Gross Work Done (cumulative).')
    previous_retention_amount = fields.Monetary(
        string='Retention Deducted in Previous IPCs',
        compute='_compute_deductions', store=True,
        currency_field='currency_id',
        help='Auto: 5% × Work Done up to Previous IPC.')
    retention_amount = fields.Monetary(
        string='Retention for this IPC',
        compute='_compute_deductions', store=True,
        currency_field='currency_id',
        help='Auto: Total Retention − Previous Retention = 5% × this IPC work.')

    # ── Mob Advance Recovery ──────────────────────────────────────────────────
    mob_advance_recovery = fields.Monetary(
        string='Mob Advance Recovery',
        currency_field='currency_id', tracking=True,
        help='Mobilisation advance amount to be recovered in this IPC.')

    # ── Payments Made to Subcontractor (GL-based, replaces HO Advance tracker) ─
    # These read actual posted GL debits on any liability_payable account
    # for this subcontractor + project, regardless of payment source
    # (HO bank payment, petty cash advance, or manual journal entry).
    payments_till_prev_ipc = fields.Monetary(
        string='Payments till Previous IPC',
        compute='_compute_payments_made', store=False,
        currency_field='currency_id', readonly=True,
        help='Total payments made to this subcontractor for this project up to '
             'the previous IPC date (sum of GL debits on all payable accounts).')
    payments_till_this_ipc = fields.Monetary(
        string='Payments till This IPC',
        compute='_compute_payments_made', store=False,
        currency_field='currency_id', readonly=True,
        help='Total payments made to this subcontractor for this project up to '
             'this IPC date.')
    payments_this_period = fields.Monetary(
        string='Payments in This Period',
        compute='_compute_payments_made', store=False,
        currency_field='currency_id', readonly=True,
        help='Payments made between the previous IPC and this IPC '
             '(= till this IPC − till previous IPC). Pre-fills the recovery field.')
    ho_advance_recovery = fields.Monetary(
        string='Payment Recovery',
        currency_field='currency_id', tracking=True,
        help='Amount to be recovered (deducted) in this IPC for payments already '
             'made to the subcontractor. Auto-filled from payments in this period; '
             'accountant may adjust.')

    # ── Back charges (auto-populated from pending records for this sub+project) ─
    backcharge_line_ids = fields.One2many(
        'x.subcontractor.ipc.backcharge.line', 'ipc_id',
        string='Back Charges')
    backcharge_total = fields.Monetary(
        string='Total Back Charges',
        compute='_compute_deductions', store=True,
        currency_field='currency_id')

    # ── Security Withheld ─────────────────────────────────────────────────────
    security_withheld = fields.Monetary(
        string='Security Withheld', currency_field='currency_id', tracking=True)

    # ── Other Deductions (user, with mandatory reason) ────────────────────────
    other_deductions_amount = fields.Monetary(
        string='Other Deductions',
        currency_field='currency_id', tracking=True)
    other_deductions_reason = fields.Char(
        string='Reason (Other Deductions)',
        help='Mandatory when Other Deductions > 0.')

    # ── Other Additions (user, with mandatory reason) ─────────────────────────
    other_additions_amount = fields.Monetary(
        string='Other Additions',
        currency_field='currency_id', tracking=True,
        help='Positive adjustments that increase the net payable for this IPC.')
    other_additions_reason = fields.Char(
        string='Reason (Other Additions)',
        help='Mandatory when Other Additions > 0.')

    # ── Computed totals ───────────────────────────────────────────────────────
    total_deductions = fields.Monetary(
        string='Total Deductions',
        compute='_compute_deductions', store=True,
        currency_field='currency_id')
    net_payable = fields.Monetary(
        string='Net Payable',
        compute='_compute_deductions', store=True,
        currency_field='currency_id')

    # ── Payments ──────────────────────────────────────────────────────────────
    payment_ids = fields.One2many(
        'account.payment', 'x_ipc_id', string='Payments')
    payment_count = fields.Integer(compute='_compute_payment_count')

    # ── HO Advances smart button ──────────────────────────────────────────────
    ho_advance_count = fields.Integer(
        string='HO Advances', compute='_compute_ho_advance_count')

    def _compute_ho_advance_count(self):
        HOAdv = self.env['x.subcontractor.ho.advance'].sudo()
        for ipc in self:
            ipc.ho_advance_count = HOAdv.search_count([
                ('subcontractor_id', '=', ipc.subcontractor_id.id),
                ('project_analytic_account_id', '=',
                 ipc.project_analytic_account_id.id),
                ('state', '=', 'confirmed'),
            ]) if ipc.subcontractor_id and ipc.project_analytic_account_id else 0

    def action_view_ho_advances(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('HO Advances'),
            'res_model': 'x.subcontractor.ho.advance',
            'view_mode': 'list,form',
            'domain': [
                ('subcontractor_id', '=', self.subcontractor_id.id),
                ('project_analytic_account_id', '=',
                 self.project_analytic_account_id.id),
            ],
            'context': {
                'default_subcontractor_id': self.subcontractor_id.id,
                'default_project_analytic_account_id':
                    self.project_analytic_account_id.id,
            },
        }

    # ── Linked liability sheet (auto on submit) ────────────────────────────────
    liability_sheet_id = fields.Many2one(
        'x.liability.sheet', string='Liability Sheet',
        readonly=True, copy=False)

    notes = fields.Text(string='Notes / Scope of Work')

    # ─────────────────────────────────────────────────────────────────────────
    # COMPUTE
    # ─────────────────────────────────────────────────────────────────────────

    @api.depends('subcontractor_id', 'project_analytic_account_id', 'ipc_date')
    def _compute_previous_ipc(self):
        """Find the most recently submitted/paid IPC for the same sub+project."""
        for ipc in self:
            if not ipc.subcontractor_id or not ipc.project_analytic_account_id:
                ipc.previous_ipc_id = False
                ipc.previous_gross_work_done = 0.0
                ipc.ipc_date_from = False
                continue
            domain = [
                ('subcontractor_id', '=', ipc.subcontractor_id.id),
                ('project_analytic_account_id', '=', ipc.project_analytic_account_id.id),
                ('state', 'in', ('submitted', 'paid')),
            ]
            if ipc.id:
                domain.append(('id', '!=', ipc.id))
            prev = self.search(domain, order='ipc_date desc, id desc', limit=1)
            if prev:
                ipc.previous_ipc_id = prev.id
                ipc.previous_gross_work_done = prev.gross_work_done
                ipc.ipc_date_from = (
                    prev.ipc_date + relativedelta(days=1)
                    if prev.ipc_date else False
                )
            else:
                ipc.previous_ipc_id = False
                ipc.previous_gross_work_done = 0.0
                ipc.ipc_date_from = False

    @api.depends(
        'subcontractor_id', 'project_analytic_account_id',
        'ipc_date', 'previous_ipc_id',
    )
    def _compute_payments_made(self):
        """Sum all payments made to this subcontractor for this project.

        Queries account.payment (outbound, posted) and petty cash subcontractor
        advances directly by partner + project — independent of which expense/payable
        account was selected on the payment, so any chart of accounts works.
        """
        cr = self.env.cr
        for ipc in self:
            if not ipc.subcontractor_id or not ipc.project_analytic_account_id:
                ipc.payments_till_prev_ipc = 0.0
                ipc.payments_till_this_ipc = 0.0
                ipc.payments_this_period = 0.0
                continue

            partner_id = ipc.subcontractor_id.id
            analytic_id = ipc.project_analytic_account_id.id

            def _total_payments(cutoff_date):
                if not cutoff_date:
                    return 0.0
                # Bank / vendor payments via account.payment
                cr.execute("""
                    SELECT COALESCE(SUM(ap.amount), 0)
                    FROM   account_payment ap
                    WHERE  ap.partner_id               = %s
                      AND  ap.payment_type             = 'outbound'
                      AND  ap.state                   IN ('in_process', 'paid')
                      AND  ap.date                    <= %s
                      AND  ap.x_destination_project_id = %s
                """, (partner_id, cutoff_date, analytic_id))
                vendor_pay = cr.fetchone()[0]

                # Petty cash subcontractor advances
                cr.execute("""
                    SELECT COALESCE(SUM(pce.amount), 0)
                    FROM   x_petty_cash_expense pce
                    WHERE  pce.advance_subcontractor_id    = %s
                      AND  pce.is_subcontractor_advance    = true
                      AND  pce.state                       = 'posted'
                      AND  pce.expense_date               <= %s
                      AND  pce.project_analytic_account_id = %s
                """, (partner_id, cutoff_date, analytic_id))
                petty_cash = cr.fetchone()[0]

                return vendor_pay + petty_cash

            prev_date = (
                ipc.previous_ipc_id.ipc_date
                if ipc.previous_ipc_id and ipc.previous_ipc_id.ipc_date
                else None
            )
            till_prev = _total_payments(prev_date)
            till_this = _total_payments(ipc.ipc_date)

            ipc.payments_till_prev_ipc = till_prev
            ipc.payments_till_this_ipc = till_this
            ipc.payments_this_period = max(till_this - till_prev, 0.0)

    @api.depends(
        'gross_work_done', 'previous_gross_work_done',
        'retention_pct',
        'mob_advance_recovery', 'ho_advance_recovery',
        'security_withheld',
        'other_deductions_amount', 'other_additions_amount',
        'backcharge_line_ids.amount',
    )
    def _compute_deductions(self):
        for ipc in self:
            this_gross = max(
                ipc.gross_work_done - ipc.previous_gross_work_done, 0.0)
            pct = (ipc.retention_pct or 5.0) / 100.0
            total_ret = ipc.gross_work_done * pct
            prev_ret = ipc.previous_gross_work_done * pct
            this_ret = total_ret - prev_ret

            bc_total = sum(
                line.amount for line in ipc.backcharge_line_ids
                if line.backcharge_id
            )

            total_ded = (
                this_ret
                + ipc.mob_advance_recovery
                + ipc.ho_advance_recovery
                + ipc.security_withheld
                + bc_total
                + ipc.other_deductions_amount
            )
            net = max(this_gross - total_ded + ipc.other_additions_amount, 0.0)

            ipc.this_ipc_gross = this_gross
            ipc.total_retention_cumulative = total_ret
            ipc.previous_retention_amount = prev_ret
            ipc.retention_amount = this_ret
            ipc.backcharge_total = bc_total
            ipc.total_deductions = total_ded
            ipc.net_payable = net

    def _compute_payment_count(self):
        for ipc in self:
            ipc.payment_count = len(ipc.payment_ids)

    # ─────────────────────────────────────────────────────────────────────────
    # ORM OVERRIDES
    # ─────────────────────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        Analytic = self.env['account.analytic.account']
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                site_code = Analytic._matracon_site_code_for_id(
                    vals.get('project_analytic_account_id'))
                seq_ref = Analytic._matracon_ref_with_site(
                    'x.subcontractor.ipc', site_code)
                # Embed ipc_number in the reference: IPC/MCH/1/2026/0001
                ipc_num = vals.get('ipc_number')
                if ipc_num:
                    parts = seq_ref.split('/')
                    parts.insert(2, str(ipc_num))
                    vals['name'] = '/'.join(parts)
                else:
                    vals['name'] = seq_ref

        ipcs = super().create(vals_list)

        # Auto-link pending backcharges not already in the One2many
        for ipc in ipcs:
            if not ipc.subcontractor_id or not ipc.project_analytic_account_id:
                continue
            already_linked = ipc.backcharge_line_ids.mapped('backcharge_id').ids
            pending = self.env['x.subcontractor.backcharge'].search([
                ('subcontractor_id', '=', ipc.subcontractor_id.id),
                ('project_analytic_account_id', '=',
                 ipc.project_analytic_account_id.id),
                ('state', '=', 'pending'),
                ('id', 'not in', already_linked),
            ])
            if pending:
                ipc.write({'backcharge_line_ids': [
                    (0, 0, {'backcharge_id': bc.id}) for bc in pending
                ]})
        return ipcs

    # ─────────────────────────────────────────────────────────────────────────
    # ONCHANGE
    # ─────────────────────────────────────────────────────────────────────────

    @api.onchange('subcontractor_id', 'project_analytic_account_id', 'ipc_date')
    def _onchange_prefill_ho_advance_recovery(self):
        """Auto-fill Payment Recovery with payments made in this IPC period.

        Queries account.payment (outbound) + petty cash advances by partner+project
        so any expense account selection is captured.
        """
        if not (self.subcontractor_id and self.project_analytic_account_id
                and self.ipc_date):
            return

        partner_id = self.subcontractor_id.id
        analytic_id = self.project_analytic_account_id.id
        cr = self.env.cr

        def _total_payments(cutoff_date):
            if not cutoff_date:
                return 0.0
            cr.execute("""
                SELECT COALESCE(SUM(ap.amount), 0)
                FROM   account_payment ap
                WHERE  ap.partner_id               = %s
                  AND  ap.payment_type             = 'outbound'
                  AND  ap.state                   IN ('in_process', 'paid')
                  AND  ap.date                    <= %s
                  AND  ap.x_destination_project_id = %s
            """, (partner_id, cutoff_date, analytic_id))
            vendor_pay = cr.fetchone()[0]

            cr.execute("""
                SELECT COALESCE(SUM(pce.amount), 0)
                FROM   x_petty_cash_expense pce
                WHERE  pce.advance_subcontractor_id    = %s
                  AND  pce.is_subcontractor_advance    = true
                  AND  pce.state                       = 'posted'
                  AND  pce.expense_date               <= %s
                  AND  pce.project_analytic_account_id = %s
            """, (partner_id, cutoff_date, analytic_id))
            petty_cash = cr.fetchone()[0]

            return vendor_pay + petty_cash

        prev_date = (
            self.previous_ipc_id.ipc_date
            if self.previous_ipc_id and self.previous_ipc_id.ipc_date
            else None
        )
        till_prev = _total_payments(prev_date)
        till_this = _total_payments(self.ipc_date)
        self.ho_advance_recovery = max(till_this - till_prev, 0.0)

    @api.onchange('subcontractor_id', 'project_analytic_account_id')
    def _onchange_fetch_backcharges(self):
        """Auto-populate back charge lines from all pending back charges
        for this subcontractor + project combination."""
        if not self.subcontractor_id or not self.project_analytic_account_id:
            return
        pending_bc = self.env['x.subcontractor.backcharge'].search([
            ('subcontractor_id', '=', self.subcontractor_id.id),
            ('project_analytic_account_id', '=',
             self.project_analytic_account_id.id),
            ('state', '=', 'pending'),
        ])
        new_lines = [(0, 0, {'backcharge_id': bc.id}) for bc in pending_bc]
        # Replace lines — start fresh so we don't double-add on sub/project change
        self.backcharge_line_ids = [(5, 0, 0)] + new_lines

    def action_refresh_backcharges(self):
        """Append any pending back charges not yet linked to this IPC."""
        self.ensure_one()
        if not self.subcontractor_id or not self.project_analytic_account_id:
            return {
                'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {
                    'type': 'warning',
                    'message': _('Please set Subcontractor and Project first.'),
                    'sticky': False,
                },
            }
        already_linked = self.backcharge_line_ids.mapped('backcharge_id').ids
        pending = self.env['x.subcontractor.backcharge'].search([
            ('subcontractor_id', '=', self.subcontractor_id.id),
            ('project_analytic_account_id', '=',
             self.project_analytic_account_id.id),
            ('state', '=', 'pending'),
            ('id', 'not in', already_linked),
        ])
        if pending:
            self.write({'backcharge_line_ids': [
                (0, 0, {'backcharge_id': bc.id}) for bc in pending
            ]})
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'type': 'success' if pending else 'info',
                'message': (
                    _('%d backcharge(s) added.') % len(pending)
                    if pending else _('No new pending backcharges found.')
                ),
                'sticky': False,
            },
        }

    # ─────────────────────────────────────────────────────────────────────────
    # WORKFLOW ACTIONS
    # ─────────────────────────────────────────────────────────────────────────

    def action_submit(self):
        for ipc in self:
            if ipc.state != 'draft':
                raise UserError(_('Only draft IPCs can be submitted.'))
            if not ipc.ipc_document:
                raise UserError(_(
                    'Please attach the signed IPC document before submitting.\n\n'
                    'Upload the signed/stamped IPC file using the "IPC Document" field.'
                ))
            if ipc.other_deductions_amount and not (
                    ipc.other_deductions_reason or '').strip():
                raise UserError(_(
                    'A reason is required when Other Deductions > 0.\n\n'
                    'Please fill in the "Reason (Other Deductions)" field.'
                ))
            if ipc.other_additions_amount and not (
                    ipc.other_additions_reason or '').strip():
                raise UserError(_(
                    'A reason is required when Other Additions > 0.\n\n'
                    'Please fill in the "Reason (Other Additions)" field.'
                ))
            # Update liability sheet with net payable
            ipc._update_liability_sheet()
            # Mark all included back charges as processed
            for line in ipc.backcharge_line_ids:
                if line.backcharge_id and line.backcharge_id.state == 'pending':
                    line.backcharge_id.sudo().write({
                        'state': 'processed',
                        'ipc_id': ipc.id,
                    })
            ipc.state = 'submitted'
            ipc.message_post(body=Markup(_(
                'IPC <b>%(name)s</b> (IPC No. %(num)s) submitted. '
                'Net Payable: <b>%(currency)s %(amount)s</b>. '
                'Liability sheet updated.'
            )) % {
                'name': ipc.name,
                'num': ipc.ipc_number,
                'currency': ipc.currency_id.symbol,
                'amount': f'{ipc.net_payable:,.2f}',
            })

    def action_mark_paid(self):
        for ipc in self:
            if ipc.state != 'submitted':
                raise UserError(_('Only submitted IPCs can be marked as paid.'))
            ipc.state = 'paid'
            ipc.message_post(
                body=_('IPC %s marked as paid.') % ipc.name)

    def _update_liability_sheet(self):
        """Add this IPC's net payable to the current month's liability sheet
        for the project. Creates the sheet if one does not yet exist."""
        self.ensure_one()
        if not self.project_analytic_account_id or not self.subcontractor_id:
            return

        bill_date = self.ipc_date or fields.Date.today()
        month_start = bill_date.replace(day=1)
        month_end = (
            month_start + relativedelta(months=1)) - relativedelta(days=1)

        LiabilitySheet = self.env['x.liability.sheet'].sudo()
        sheet = LiabilitySheet.search([
            ('project_analytic_account_id', '=',
             self.project_analytic_account_id.id),
            ('date_from', '=', month_start),
            ('state', 'in', ('draft', 'submitted')),
        ], limit=1)
        if not sheet:
            sheet = LiabilitySheet.create({
                'project_analytic_account_id': self.project_analytic_account_id.id,
                'date_from': month_start,
                'date_to': month_end,
            })

        amount = self.net_payable
        date_str = (
            self.ipc_date.strftime('%d-%b-%Y') if self.ipc_date else '')
        desc = _('IPC No.%s — %s — %s') % (
            self.ipc_number, self.name, date_str)

        existing_line = sheet.line_ids.filtered(
            lambda l: l.partner_id.id == self.subcontractor_id.id)
        if existing_line:
            existing_line[0].write({
                'new_liability': existing_line[0].new_liability + amount,
                'description': desc,
            })
        else:
            sheet.write({'line_ids': [(0, 0, {
                'description': desc,
                'partner_id': self.subcontractor_id.id,
                'new_liability': amount,
            })]})

        self.liability_sheet_id = sheet.id
        self.message_post(body=Markup(_(
            'Liability Sheet <b>%(sheet)s</b> updated — '
            '<b>%(sub)s</b> &nbsp;+&nbsp;<b>%(currency)s %(amount)s</b> registered.'
        )) % {
            'sheet': sheet.name,
            'sub': self.subcontractor_id.name,
            'currency': self.currency_id.symbol,
            'amount': f'{amount:,.2f}',
        })

    # ─────────────────────────────────────────────────────────────────────────
    # SMART BUTTON ACTIONS
    # ─────────────────────────────────────────────────────────────────────────

    def action_view_payments(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('IPC Payments'),
            'res_model': 'account.payment',
            'view_mode': 'list,form',
            'domain': [('x_ipc_id', '=', self.id)],
        }

    def action_view_liability_sheet(self):
        self.ensure_one()
        if not self.liability_sheet_id:
            return
        return {
            'type': 'ir.actions.act_window',
            'name': _('Liability Sheet'),
            'res_model': 'x.liability.sheet',
            'view_mode': 'form',
            'res_id': self.liability_sheet_id.id,
        }

    def action_print_ipc(self):
        return self.env.ref(
            'site_operations.action_report_subcontractor_ipc'
        ).report_action(self)

    def unlink(self):
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_('Only draft IPCs can be deleted.'))
        return super().unlink()

    def action_delete_draft(self):
        self.unlink()
        return {'type': 'ir.actions.act_window_close'}
