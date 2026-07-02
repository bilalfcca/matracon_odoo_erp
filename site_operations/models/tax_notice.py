from datetime import timedelta

from markupsafe import Markup

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

from . import matracon_notifications as matracon_notify


class TaxNoticeOrder(models.Model):
    _name = 'x.tax.notice.order'
    _description = 'Tax Notice & Order'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'document_date desc, id desc'

    name = fields.Char(
        string='Notice Ref', readonly=True, default=lambda self: _('New'), copy=False)
    ref_no = fields.Char(
        string='Ref No', tracking=True, index=True,
        help='Internal or FBR/SRB reference number for the notice.')
    taxpayer_name = fields.Char(
        string='Name of JV / Taxpayer', required=True, tracking=True, index=True)
    partner_id = fields.Many2one(
        'res.partner', string='Taxpayer Entity', tracking=True, index=True,
        help='Linked company or JV partner when available in contacts.')
    notice_section = fields.Char(
        string='Notice U/S Received', tracking=True,
        help='Legal section under which notice was issued, e.g. 122(5A) Sales Tax Act.')
    description = fields.Text(tracking=True)
    tax_year = fields.Char(string='Tax Year', tracking=True, index=True)
    document_date = fields.Date(string='Document Date', required=True, tracking=True)
    due_date = fields.Date(string='Due Date', tracking=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('open', 'Open'),
        ('reply_submitted', 'Reply Submitted'),
        ('under_proceeding', 'Under Proceeding'),
        ('order_filed', 'Order Filed'),
        ('annulled', 'Annulled'),
        ('vacated', 'Vacated'),
        ('closed', 'Closed'),
        ('paid', 'Paid'),
    ], default='draft', tracking=True, required=True, index=True)
    case_status = fields.Selection([
        ('open', 'Open'),
        ('contested', 'Contested'),
        ('in_process', 'In Process'),
        ('annulled', 'Annulled'),
        ('vacated', 'Vacated'),
        ('closed', 'Closed'),
    ], string='Status', default='open', tracking=True, required=True)
    liability_notice_amount = fields.Monetary(
        string='Amount of Tax Liability in Notice',
        currency_field='currency_id', tracking=True)
    tax_ordered_amount = fields.Monetary(
        string='Amount of Tax Ordered',
        currency_field='currency_id', tracking=True,
        help='Final demand amount after assessment order, if different from notice.')
    paid_officially = fields.Monetary(
        compute='_compute_payment_totals', store=True,
        currency_field='currency_id',
    )
    paid_unofficially = fields.Monetary(
        compute='_compute_payment_totals', store=True,
        currency_field='currency_id',
    )
    consultant_fee = fields.Monetary(
        compute='_compute_payment_totals', store=True,
        currency_field='currency_id',
    )
    total_payment = fields.Monetary(
        compute='_compute_payment_totals', store=True,
        currency_field='currency_id',
    )
    remaining_liability = fields.Monetary(
        compute='_compute_payment_totals', store=True,
        currency_field='currency_id',
    )
    days_to_due = fields.Integer(compute='_compute_due_alerts')
    is_overdue = fields.Boolean(
        compute='_compute_due_alerts', search='_search_is_overdue')
    is_due_soon = fields.Boolean(
        compute='_compute_due_alerts', search='_search_is_due_soon')
    payment_line_ids = fields.One2many(
        'x.tax.notice.payment.line', 'notice_id', string='Payments & Costs')
    auditor_remarks = fields.Text(string='Auditor Remarks')
    currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id)
    company_id = fields.Many2one(
        'res.company', default=lambda self: self.env.company, required=True)

    @api.depends(
        'payment_line_ids.amount', 'payment_line_ids.category',
        'liability_notice_amount', 'tax_ordered_amount',
    )
    def _compute_payment_totals(self):
        for rec in self:
            official = unofficial = consultant = 0.0
            for line in rec.payment_line_ids:
                if line.category == 'paid_officially':
                    official += line.amount
                elif line.category == 'paid_unofficially':
                    unofficial += line.amount
                elif line.category == 'consultant_fee':
                    consultant += line.amount
            rec.paid_officially = official
            rec.paid_unofficially = unofficial
            rec.consultant_fee = consultant
            rec.total_payment = official + unofficial + consultant
            base = rec.tax_ordered_amount or rec.liability_notice_amount
            rec.remaining_liability = base - rec.total_payment

    @api.depends('due_date', 'state')
    def _compute_due_alerts(self):
        today = fields.Date.context_today(self)
        for rec in self:
            if rec.due_date and rec.state not in ('closed', 'paid', 'annulled', 'vacated'):
                rec.days_to_due = (rec.due_date - today).days
                rec.is_overdue = rec.due_date < today
                rec.is_due_soon = 0 <= rec.days_to_due <= 14
            else:
                rec.days_to_due = 0
                rec.is_overdue = False
                rec.is_due_soon = False

    @api.model
    def _search_is_overdue(self, operator, value):
        if operator not in ('=', '!='):
            return []
        today = fields.Date.context_today(self)
        open_states = ('draft', 'open', 'reply_submitted', 'under_proceeding', 'order_filed')
        overdue_domain = [
            ('state', 'in', open_states),
            ('due_date', '<', today),
        ]
        is_true = (operator == '=' and value) or (operator == '!=' and not value)
        if is_true:
            return overdue_domain
        return [
            '|',
            ('state', 'not in', open_states),
            ('due_date', '>=', today),
        ]

    @api.model
    def _search_is_due_soon(self, operator, value):
        if operator not in ('=', '!='):
            return []
        today = fields.Date.context_today(self)
        limit = today + timedelta(days=14)
        open_states = ('draft', 'open', 'reply_submitted', 'under_proceeding', 'order_filed')
        due_soon_domain = [
            ('state', 'in', open_states),
            ('due_date', '>=', today),
            ('due_date', '<=', limit),
        ]
        is_true = (operator == '=' and value) or (operator == '!=' and not value)
        if is_true:
            return due_soon_domain
        return [
            '|', '|',
            ('state', 'not in', open_states),
            ('due_date', '<', today),
            ('due_date', '>', limit),
        ]

    @api.onchange('partner_id')
    def _onchange_partner_id(self):
        if self.partner_id and not self.taxpayer_name:
            self.taxpayer_name = self.partner_id.display_name

    @api.constrains('document_date', 'due_date')
    def _check_dates(self):
        for rec in self:
            if rec.document_date and rec.due_date and rec.due_date < rec.document_date:
                raise ValidationError(_('Due date cannot be before document date.'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('x.tax.notice.order') or _('New')
        records = super().create(vals_list)
        for rec in records:
            rec._schedule_due_alert()
        return records

    def write(self, vals):
        res = super().write(vals)
        if 'due_date' in vals or 'state' in vals:
            for rec in self:
                rec._schedule_due_alert()
        return res

    def action_open(self):
        for rec in self.filtered(lambda r: r.state == 'draft'):
            rec.state = 'open'
            rec.case_status = 'open'
            matracon_notify.notify_group(
                rec, 'purchase_demand_raise.group_ceo_approval',
                _('Tax Notice <b>%(ref)s</b> opened — due %(due)s.',
                  ref=rec.name, due=rec.due_date or _('N/A')),
                summary=_('Tax Notice Opened'),
            )

    def action_reply_submitted(self):
        self.filtered(lambda r: r.state == 'open').write({
            'state': 'reply_submitted',
            'case_status': 'in_process',
        })

    def action_under_proceeding(self):
        self.filtered(lambda r: r.state in ('open', 'reply_submitted')).write({
            'state': 'under_proceeding',
            'case_status': 'in_process',
        })

    def action_order_filed(self):
        self.filtered(lambda r: r.state in ('under_proceeding', 'reply_submitted', 'open')).write({
            'state': 'order_filed',
            'case_status': 'contested',
        })

    def action_mark_contested(self):
        self.write({'case_status': 'contested'})

    def action_annul(self):
        self.filtered(lambda r: r.state not in ('paid', 'closed')).write({
            'state': 'annulled',
            'case_status': 'annulled',
        })

    def action_vacate(self):
        self.filtered(lambda r: r.state not in ('paid', 'closed')).write({
            'state': 'vacated',
            'case_status': 'vacated',
        })

    def action_close(self):
        for rec in self.filtered(lambda r: r.state not in ('paid', 'annulled', 'vacated')):
            if rec.remaining_liability > 0.01:
                raise UserError(_(
                    'Cannot close %(ref)s — remaining liability is %(amt)s.',
                    ref=rec.name, amt=rec.remaining_liability,
                ))
            rec.write({'state': 'closed', 'case_status': 'closed'})

    def action_mark_paid(self):
        for rec in self.filtered(lambda r: r.state not in ('paid', 'annulled', 'vacated', 'closed')):
            if rec.remaining_liability > 0.01:
                raise UserError(_(
                    'Remaining liability must be zero before marking paid. '
                    'Add payment lines or adjust ordered amount.',
                ))
            rec.write({'state': 'paid', 'case_status': 'closed'})

    def action_add_payment(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Add Payment'),
            'res_model': 'x.tax.notice.payment.line',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_notice_id': self.id},
        }

    def action_update_status(self):
        """Open form for case status update — keeps legal workflow on statusbar."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'x.tax.notice.order',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _schedule_due_alert(self):
        for rec in self.filtered(lambda r: r.due_date and r.state in ('open', 'under_proceeding', 'order_filed')):
            alert_date = rec.due_date - timedelta(days=14)
            if alert_date >= fields.Date.context_today(rec):
                users = self.env['res.users'].search([
                    ('group_ids', 'in', [
                        self.env.ref('site_operations.group_finance_ho').id,
                        self.env.ref('purchase_demand_raise.group_ceo_approval').id,
                    ]),
                ])
                matracon_notify.schedule_activity(
                    rec, users,
                    _('Tax notice due on %(date)s', date=rec.due_date),
                    note=_('Tax Notice %(ref)s — reply/payment due in 14 days.', ref=rec.name),
                )

    @api.model
    def _cron_due_date_alerts(self):
        today = fields.Date.context_today(self)
        threshold = today + timedelta(days=14)
        notices = self.search([
            ('state', 'in', ('open', 'reply_submitted', 'under_proceeding', 'order_filed')),
            ('due_date', '<=', threshold),
            ('due_date', '>=', today),
        ])
        for rec in notices:
            body = _(
                'Tax Notice <b>%(ref)s</b> is due on <b>%(date)s</b> (%(days)s days remaining).',
                ref=rec.name, date=rec.due_date, days=rec.days_to_due,
            )
            matracon_notify.notify_group(
                rec, 'site_operations.group_finance_ho',
                Markup(body),
                summary=_('Tax Notice Due Soon'),
            )

    def action_print_tax_notice(self):
        return self.env.ref(
            'site_operations.action_report_tax_notice'
        ).report_action(self)


class TaxNoticePaymentLine(models.Model):
    _name = 'x.tax.notice.payment.line'
    _description = 'Tax Notice Payment Line'
    _order = 'payment_date desc, id'

    notice_id = fields.Many2one(
        'x.tax.notice.order', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(string='Sr No', default=10)
    category = fields.Selection([
        ('paid_officially', 'Paid Officially'),
        ('paid_unofficially', 'Paid Unofficially'),
        ('consultant_fee', 'Consultant Fee'),
        ('penalty', 'Penalty / Surcharge'),
        ('other', 'Other Cost'),
    ], required=True, default='paid_officially')
    description = fields.Char(required=True)
    payment_date = fields.Date(default=fields.Date.context_today)
    reference = fields.Char(
        help='CPR number, challan, or consultant invoice reference.')
    amount = fields.Monetary(required=True, currency_field='currency_id')
    currency_id = fields.Many2one(
        related='notice_id.currency_id', store=True, readonly=True)
