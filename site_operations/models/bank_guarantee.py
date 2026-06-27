from datetime import timedelta

from markupsafe import Markup

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

from . import matracon_notifications as matracon_notify


class BankGuaranteeFacility(models.Model):
    """Sanctioned BG limit per bank — utilized amount rolls up from live guarantees."""
    _name = 'x.bank.guarantee.facility'
    _description = 'Bank Guarantee Facility Limit'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'bank_id'

    name = fields.Char(compute='_compute_name', store=True, readonly=True)
    bank_id = fields.Many2one(
        'res.bank', string='Bank', required=True, tracking=True, index=True)
    total_limit = fields.Monetary(
        string='Total Sanctioned Limit', required=True, tracking=True,
        currency_field='currency_id',
        help='Overall bank guarantee facility limit sanctioned by the bank.',
    )
    utilized_amount = fields.Monetary(
        compute='_compute_utilization', store=True,
        currency_field='currency_id',
    )
    available_limit = fields.Monetary(
        compute='_compute_utilization', store=True,
        currency_field='currency_id',
    )
    guarantee_ids = fields.One2many(
        'x.bank.guarantee', 'facility_id', string='Guarantees')
    guarantee_count = fields.Integer(compute='_compute_guarantee_count')
    currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('bank_unique', 'unique(bank_id)', 'Each bank can have only one facility limit record.'),
    ]

    @api.depends('bank_id')
    def _compute_name(self):
        for rec in self:
            rec.name = rec.bank_id.name or _('Bank Facility')

    @api.depends(
        'guarantee_ids.state', 'guarantee_ids.bg_amount', 'total_limit',
    )
    def _compute_utilization(self):
        active_states = ('pending', 'active', 'locked')
        for rec in self:
            utilized = sum(rec.guarantee_ids.filtered(
                lambda g: g.state in active_states
            ).mapped('bg_amount'))
            rec.utilized_amount = utilized
            rec.available_limit = rec.total_limit - utilized

    def _compute_guarantee_count(self):
        for rec in self:
            rec.guarantee_count = len(rec.guarantee_ids)

    def action_view_guarantees(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Bank Guarantees'),
            'res_model': 'x.bank.guarantee',
            'view_mode': 'list,form',
            'domain': [('facility_id', '=', self.id)],
            'context': {'default_facility_id': self.id, 'default_bank_id': self.bank_id.id},
        }


class BankGuarantee(models.Model):
    _name = 'x.bank.guarantee'
    _description = 'Bank Guarantee'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'issue_date desc, id desc'

    name = fields.Char(
        string='Reference', readonly=True, default=lambda self: _('New'), copy=False)
    sr_no = fields.Char(
        string='Sr No', readonly=True, copy=False,
        help='Serial number within the financial year.')
    bank_id = fields.Many2one(
        'res.bank', string='Bank Name', required=True, tracking=True, index=True)
    facility_id = fields.Many2one(
        'x.bank.guarantee.facility', string='Bank Facility',
        ondelete='restrict', index=True)
    nature = fields.Selection([
        ('bid_bond', 'Bid Bond / Bid Guarantee'),
        ('performance', 'Performance BG'),
        ('advance_payment', 'Advance Payment Guarantee'),
        ('financial', 'Financial BG'),
        ('retention', 'Retention Money Guarantee'),
        ('other', 'Other'),
    ], string='Nature of BG', required=True, default='performance', tracking=True)
    guarantee_number = fields.Char(
        string='Guarantee No', required=True, tracking=True, index=True,
        help='Bank-issued guarantee reference number.')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('pending', 'Pending'),
        ('active', 'Active'),
        ('locked', 'Locked'),
        ('released', 'Released'),
        ('expired', 'Expired'),
        ('cancelled', 'Cancelled'),
    ], default='draft', tracking=True, required=True, index=True)
    jv_type = fields.Selection([
        ('direct', 'Direct'),
        ('jv', 'Joint Venture'),
    ], string='JV', default='direct', required=True)
    jv_name = fields.Char(
        string='JV Name', tracking=True,
        help='Joint venture name when guarantee is issued in JV capacity.')
    project_id = fields.Many2one(
        'project.project', string='Project', tracking=True, index=True)
    project_analytic_account_id = fields.Many2one(
        related='project_id.account_id', store=True, readonly=True)
    beneficiary_id = fields.Many2one(
        'res.partner', string='Beneficiary', tracking=True, index=True)
    beneficiary_name = fields.Char(
        string='Beneficiary Name', tracking=True,
        help='Beneficiary as printed on the guarantee (employer / authority).')
    issue_date = fields.Date(required=True, tracking=True)
    expiry_date = fields.Date(required=True, tracking=True)
    bg_amount = fields.Monetary(
        string='BG Amount', required=True, tracking=True,
        currency_field='currency_id')
    cash_margin_percent = fields.Float(
        string='Cash Margin (%)', digits=(5, 2), tracking=True)
    pricing_percent = fields.Float(
        string='Pricing (% p.a.)', digits=(5, 2), tracking=True,
        help='Bank commission / pricing percentage per annum.')
    margin_amount = fields.Monetary(
        compute='_compute_margin_amount', store=True,
        currency_field='currency_id',
    )
    total_limit = fields.Monetary(
        related='facility_id.total_limit', readonly=True,
        currency_field='currency_id')
    utilized_amount = fields.Monetary(
        related='facility_id.utilized_amount', readonly=True,
        currency_field='currency_id')
    available_limit = fields.Monetary(
        related='facility_id.available_limit', readonly=True,
        currency_field='currency_id')
    days_to_expiry = fields.Integer(compute='_compute_days_to_expiry')
    is_expiring_soon = fields.Boolean(compute='_compute_days_to_expiry')
    validated = fields.Boolean(default=False, tracking=True, copy=False)
    validated_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    validated_date = fields.Datetime(readonly=True, copy=False)
    release_date = fields.Date(readonly=True, copy=False, tracking=True)
    release_notes = fields.Text(readonly=True, copy=False)
    amendment_ids = fields.One2many(
        'x.bank.guarantee.amendment', 'guarantee_id', string='Amendment History')
    currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id)
    company_id = fields.Many2one(
        'res.company', default=lambda self: self.env.company, required=True)
    notes = fields.Text()

    @api.depends('bg_amount', 'cash_margin_percent')
    def _compute_margin_amount(self):
        for rec in self:
            rec.margin_amount = rec.bg_amount * (rec.cash_margin_percent or 0.0) / 100.0

    @api.depends('expiry_date', 'state')
    def _compute_days_to_expiry(self):
        today = fields.Date.context_today(self)
        for rec in self:
            if rec.expiry_date and rec.state in ('active', 'locked', 'pending'):
                rec.days_to_expiry = (rec.expiry_date - today).days
                rec.is_expiring_soon = 0 <= rec.days_to_expiry <= 30
            else:
                rec.days_to_expiry = 0
                rec.is_expiring_soon = False

    @api.onchange('bank_id')
    def _onchange_bank_id(self):
        if self.bank_id:
            facility = self.env['x.bank.guarantee.facility'].search([
                ('bank_id', '=', self.bank_id.id),
            ], limit=1)
            self.facility_id = facility

    @api.onchange('beneficiary_id')
    def _onchange_beneficiary_id(self):
        if self.beneficiary_id and not self.beneficiary_name:
            self.beneficiary_name = self.beneficiary_id.display_name

    @api.constrains('issue_date', 'expiry_date')
    def _check_dates(self):
        for rec in self:
            if rec.issue_date and rec.expiry_date and rec.expiry_date < rec.issue_date:
                raise ValidationError(_('Expiry date cannot be before issue date.'))

    @api.constrains('bg_amount', 'facility_id', 'state')
    def _check_facility_limit(self):
        active_states = ('pending', 'active', 'locked')
        for rec in self.filtered(lambda r: r.facility_id and r.state in active_states):
            others = rec.facility_id.guarantee_ids.filtered(
                lambda g: g.id != rec.id and g.state in active_states
            )
            utilized = sum(others.mapped('bg_amount')) + rec.bg_amount
            if utilized > rec.facility_id.total_limit:
                raise ValidationError(_(
                    'Total active BG amount (%(util)s) exceeds bank facility limit (%(limit)s).',
                    util=utilized, limit=rec.facility_id.total_limit,
                ))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('x.bank.guarantee') or _('New')
            if not vals.get('sr_no'):
                vals['sr_no'] = self.env['ir.sequence'].next_by_code('x.bank.guarantee.sr') or ''
            bank_id = vals.get('bank_id')
            if bank_id and not vals.get('facility_id'):
                facility = self.env['x.bank.guarantee.facility'].search([
                    ('bank_id', '=', bank_id),
                ], limit=1)
                if facility:
                    vals['facility_id'] = facility.id
        records = super().create(vals_list)
        for rec in records:
            if not rec.amendment_ids:
                rec.amendment_ids = [(0, 0, {
                    'amendment_type': 'initial',
                    'description': _('Original issuance'),
                    'amount_change': rec.bg_amount,
                    'amendment_date': rec.issue_date or fields.Date.context_today(rec),
                })]
        return records

    def write(self, vals):
        if 'bg_amount' in vals:
            for rec in self.filtered(lambda r: r.state in ('active', 'locked')):
                old_amount = rec.bg_amount
                new_amount = vals['bg_amount']
                if old_amount != new_amount:
                    delta = new_amount - old_amount
                    rec.amendment_ids = [(0, 0, {
                        'amendment_type': 'increase' if delta > 0 else 'decrease',
                        'description': _('Amount amended from %(old)s to %(new)s', old=old_amount, new=new_amount),
                        'amount_change': delta,
                        'amendment_date': fields.Date.context_today(rec),
                    })]
        return super().write(vals)

    def _ensure_facility(self):
        for rec in self:
            if not rec.bank_id:
                raise UserError(_('Select a bank before submitting the guarantee.'))
            facility = self.env['x.bank.guarantee.facility'].search([
                ('bank_id', '=', rec.bank_id.id),
            ], limit=1)
            if not facility:
                raise UserError(_(
                    'No facility limit is configured for %(bank)s. '
                    'Ask Finance to set the bank\'s sanctioned BG limit first '
                    '(Accounting → Compliance → Bank Facility Limits).',
                    bank=rec.bank_id.display_name,
                ))
            if not rec.facility_id:
                rec.facility_id = facility
            if facility.total_limit <= 0:
                raise UserError(_(
                    'Bank facility limit for %(bank)s is zero. Set the sanctioned limit first.',
                    bank=rec.bank_id.display_name,
                ))

    def action_submit(self):
        for rec in self:
            if rec.state != 'draft':
                continue
            rec._ensure_facility()
            if rec.bg_amount > rec.facility_id.available_limit:
                raise UserError(_(
                    'Cannot submit: BG amount (%(amt)s) exceeds available limit (%(avail)s).',
                    amt=rec.bg_amount, avail=rec.facility_id.available_limit,
                ))
            rec.state = 'pending'
            matracon_notify.notify_group(
                rec, 'site_operations.group_finance_ho',
                _('Bank Guarantee <b>%(ref)s</b> submitted for validation.', ref=rec.name),
                summary=_('BG Pending Validation'),
            )

    def action_validate(self):
        for rec in self:
            if rec.state != 'pending':
                continue
            rec._ensure_facility()
            rec.write({
                'state': 'active',
                'validated': True,
                'validated_by_id': self.env.uid,
                'validated_date': fields.Datetime.now(),
            })
            matracon_notify.notify_group(
                rec, 'purchase_demand_raise.group_ceo_approval',
                _('Bank Guarantee <b>%(ref)s</b> is now Active.', ref=rec.name),
                summary=_('BG Activated'),
            )
            rec._schedule_expiry_alert()

    def action_lock(self):
        self.filtered(lambda r: r.state == 'active').write({'state': 'locked'})

    def action_request_release(self):
        for rec in self.filtered(lambda r: r.state in ('active', 'locked')):
            matracon_notify.notify_group(
                rec, 'site_operations.group_finance_ho',
                _('Release requested for Bank Guarantee <b>%(ref)s</b>.', ref=rec.name),
                summary=_('BG Release Request'),
            )
            rec.message_post(body=_('Release requested from bank.'))

    def action_release(self):
        for rec in self.filtered(lambda r: r.state in ('active', 'locked', 'expired')):
            rec.write({
                'state': 'released',
                'release_date': fields.Date.context_today(rec),
            })
            rec.amendment_ids = [(0, 0, {
                'amendment_type': 'release',
                'description': _('Guarantee released by bank'),
                'amount_change': -rec.bg_amount,
                'amendment_date': fields.Date.context_today(rec),
            })]

    def action_amend(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Amend Bank Guarantee'),
            'res_model': 'x.bank.guarantee.amendment',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_guarantee_id': self.id,
                'default_amendment_date': fields.Date.context_today(self),
            },
        }

    def action_reset_draft(self):
        self.filtered(lambda r: r.state in ('pending', 'cancelled')).write({'state': 'draft'})

    def action_cancel(self):
        self.filtered(lambda r: r.state in ('draft', 'pending')).write({'state': 'cancelled'})

    def _schedule_expiry_alert(self):
        for rec in self.filtered(lambda r: r.expiry_date and r.state == 'active'):
            alert_date = rec.expiry_date - timedelta(days=30)
            if alert_date >= fields.Date.context_today(rec):
                users = self.env['res.users'].search([
                    ('group_ids', 'in', [
                        self.env.ref('site_operations.group_finance_ho').id,
                        self.env.ref('purchase_demand_raise.group_ceo_approval').id,
                    ]),
                ])
                matracon_notify.schedule_activity(
                    rec, users,
                    _('BG expiring on %(date)s', date=rec.expiry_date),
                    note=_('Bank Guarantee %(ref)s (%(guarantee)s) expires in 30 days.',
                           ref=rec.name, guarantee=rec.guarantee_number),
                )

    @api.model
    def _cron_expire_guarantees(self):
        today = fields.Date.context_today(self)
        expiring = self.search([
            ('state', 'in', ('active', 'locked')),
            ('expiry_date', '<', today),
        ])
        for rec in expiring:
            rec.state = 'expired'
            matracon_notify.notify_group(
                rec, 'site_operations.group_finance_ho',
                _('Bank Guarantee <b>%(ref)s</b> has <b>expired</b> on %(date)s.',
                  ref=rec.name, date=rec.expiry_date),
                summary=_('BG Expired'),
            )


class BankGuaranteeAmendment(models.Model):
    _name = 'x.bank.guarantee.amendment'
    _description = 'Bank Guarantee Amendment'
    _order = 'amendment_date desc, id desc'

    guarantee_id = fields.Many2one(
        'x.bank.guarantee', required=True, ondelete='cascade', index=True)
    amendment_date = fields.Date(
        string='Date', required=True, default=fields.Date.context_today)
    amendment_type = fields.Selection([
        ('initial', 'Initial'),
        ('extension', 'Extension'),
        ('increase', 'Increase'),
        ('decrease', 'Decrease'),
        ('release', 'Release'),
        ('other', 'Other'),
    ], required=True, default='other')
    description = fields.Text(required=True)
    amount_change = fields.Monetary(currency_field='currency_id')
    new_expiry_date = fields.Date(string='New Expiry Date')
    currency_id = fields.Many2one(
        related='guarantee_id.currency_id', store=True, readonly=True)

    def action_apply_extension(self):
        for rec in self.filtered(lambda a: a.new_expiry_date and a.guarantee_id):
            rec.guarantee_id.expiry_date = rec.new_expiry_date
            if rec.guarantee_id.state == 'expired':
                rec.guarantee_id.state = 'active'
