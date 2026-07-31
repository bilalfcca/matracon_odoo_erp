import logging
from datetime import timedelta

from markupsafe import Markup

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

from . import matracon_notifications as matracon_notify

_logger = logging.getLogger(__name__)

ALERT_DAYS = 10  # days before expiry to trigger alert email + activity


class BgExpiryConfig(models.Model):
    """Singleton — configure extra recipients for BG expiry alert emails.
    Finance HO, CEO and Admin are always included automatically."""
    _name = 'x.bg.expiry.config'
    _description = 'BG Expiry Notification Configuration'

    name = fields.Char(default='BG Expiry Notification Settings', readonly=True)
    recipient_partner_ids = fields.Many2many(
        'res.partner', string='Additional Recipients',
        help='Odoo contacts (users or external) to CC on BG expiry alerts. '
             'Finance HO, CEO and Admin are always included automatically.',
    )
    extra_emails = fields.Text(
        string='Extra Email Addresses (one per line)',
        help='Raw email addresses for persons not in Odoo at all '
             '(e.g. CFO, external auditor). One email per line.',
    )

    @api.model
    def _get_config(self):
        config = self.search([], limit=1)
        if not config:
            config = self.sudo().create({'name': 'BG Expiry Notification Settings'})
        return config


class BankGuaranteeNature(models.Model):
    """Configurable Nature of BG types (Bid Security, Performance, etc.)."""
    _name = 'x.bg.nature'
    _description = 'Bank Guarantee Nature'
    _order = 'sequence, name'

    name = fields.Char(string='Nature', required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    # Internal code used by dashboards — set only on seed records, never edited
    code = fields.Char(string='Code', readonly=True, copy=False)

    guarantee_count = fields.Integer(compute='_compute_guarantee_count')

    def _compute_guarantee_count(self):
        BG = self.env['x.bank.guarantee'].sudo()
        for rec in self:
            rec.guarantee_count = BG.search_count([('nature_id', '=', rec.id)])

    _name_unique = models.Constraint(
        'unique(name)', 'Each nature type must have a unique name.'
    )


class BankGuaranteeFacility(models.Model):
    """Sanctioned BG limit per bank — utilized amount rolls up from live guarantees."""
    _name = 'x.bank.guarantee.facility'
    _description = 'Bank Guarantee Facility Limit'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'journal_id'

    name = fields.Char(compute='_compute_name', store=True, readonly=True)
    journal_id = fields.Many2one(
        'account.journal', string='Bank / Journal', required=True, tracking=True,
        index=True, domain=[('type', '=', 'bank')],
    )
    reference_no = fields.Char(
        string='Reference No', tracking=True,
        help='Bank-issued facility reference / sanction letter number.')
    date_of_issuance = fields.Date(
        string='Date of Issuance', tracking=True,
        help='Date the facility was sanctioned / issued by the bank.')
    expiry_date = fields.Date(
        string='Expiry Date', tracking=True,
        help='Date on which the facility limit expires.')
    cash_margin_percent = fields.Float(
        string='Cash Margin (%)', digits=(5, 2), tracking=True,
        help='Default cash margin percentage applicable under this facility.')
    commission_percent = fields.Float(
        string='Commission (Bank) / Pricing (%)', digits=(5, 2), tracking=True,
        help='Bank commission / pricing rate applicable under this facility.')
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
    security_details = fields.Html(
        string='Security Details',
        help='Details of collateral / security pledged against this facility.')
    cash_margin_slab_ids = fields.One2many(
        'x.bg.facility.cash.margin.slab', 'facility_id',
        string='Cash Margin Slabs',
        help='Tiered cash margin rates for this facility. '
             'These are auto-copied to each BG issued under this facility. '
             'If no slabs are defined, the "Cash Margin (%)" field above is used.')
    renewal_ids = fields.One2many(
        'x.bank.guarantee.facility.renewal', 'facility_id',
        string='Renewal History')
    guarantee_ids = fields.One2many(
        'x.bank.guarantee', 'facility_id', string='Guarantees')
    guarantee_count = fields.Integer(compute='_compute_guarantee_count')
    currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id)
    active = fields.Boolean(default=True)

    _bank_unique = models.Constraint(
        'unique(journal_id)',
        'Each bank journal can have only one facility limit record.',
    )

    @api.depends('journal_id')
    def _compute_name(self):
        for rec in self:
            rec.name = rec.journal_id.name or _('Bank Facility')

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

    def action_renew_facility(self):
        """Archive the current facility letter into Renewal History and clear
        the header fields so the user can enter the new sanction letter details."""
        self.ensure_one()
        if not (self.reference_no or self.date_of_issuance or self.expiry_date):
            raise UserError(_(
                'Please fill in at least the Reference No or dates before renewing — '
                'there is no current letter to archive.'
            ))
        self.env['x.bank.guarantee.facility.renewal'].create({
            'facility_id': self.id,
            'reference_no': self.reference_no,
            'date_of_issuance': self.date_of_issuance,
            'expiry_date': self.expiry_date,
            'total_limit': self.total_limit,
            'cash_margin_percent': self.cash_margin_percent,
            'commission_percent': self.commission_percent,
        })
        # Clear the header so the user fills the new letter's details
        self.write({
            'reference_no': False,
            'date_of_issuance': False,
            'expiry_date': False,
        })
        self.message_post(body=_(
            'Facility renewed. Previous sanction letter details have been '
            'archived in the <b>Renewal History</b> tab. '
            'Please enter the new letter details above.'
        ))

    def action_view_guarantees(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Bank Guarantees'),
            'res_model': 'x.bank.guarantee',
            'view_mode': 'list,form',
            'domain': [('facility_id', '=', self.id)],
            'context': {'default_facility_id': self.id, 'default_journal_id': self.journal_id.id},
        }

    def action_open_import_wizard(self):
        """Open the Bank Facility import wizard from the list view header."""
        return {
            'type': 'ir.actions.act_window',
            'name': 'Import Bank Facility Limits',
            'res_model': 'x.bg.facility.import.wizard',
            'view_mode': 'form',
            'target': 'new',
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
    journal_id = fields.Many2one(
        'account.journal', string='Bank', required=True, tracking=True,
        index=True, domain=[('type', '=', 'bank')],
    )
    facility_id = fields.Many2one(
        'x.bank.guarantee.facility', string='Bank Facility',
        ondelete='restrict', index=True)
    nature_id = fields.Many2one(
        'x.bg.nature', string='Nature of BG', required=True, tracking=True,
        ondelete='restrict',
        default=lambda self: self.env.ref(
            'site_operations.bg_nature_performance', raise_if_not_found=False),
    )
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
        ('direct', 'MPPL'),
        ('jv', 'Joint Venture'),
        ('on_behalf', 'On Behalf Of'),
    ], string='BIDDER . Matracon / JV', default='direct', required=True)
    jv_name = fields.Char(
        string='JV Name', tracking=True,
        help='Joint venture name when guarantee is issued in JV capacity.')
    on_behalf_entity = fields.Char(
        string='Issuing Entity', tracking=True,
        help='Name of the third-party entity issuing the guarantee on behalf of Matracon (e.g. ZKB).')
    bidder_display = fields.Char(
        string='Bidder', compute='_compute_bidder_display', store=True,
        help='Human-readable bidder label: MPPL, JV name, or the on-behalf entity name.')
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
    margin_amount = fields.Monetary(
        string='Cash Margin Amount',
        compute='_compute_charges', store=True,
        currency_field='currency_id',
    )
    pricing_percent = fields.Float(
        string='Bank Commission/Pricing (%)', digits=(5, 2), tracking=True,
        help='Bank commission / pricing percentage applied on BG amount.')
    commission_amount = fields.Monetary(
        string='Commission Amount',
        compute='_compute_charges', store=True,
        currency_field='currency_id',
    )
    fed_percent = fields.Float(
        string='FED (%)', digits=(5, 2), tracking=True,
        help='Federal Excise Duty percentage applied on commission amount.')
    fed_amount = fields.Monetary(
        string='FED Amount',
        compute='_compute_charges', store=True,
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
    is_expiring_soon = fields.Boolean(
        compute='_compute_days_to_expiry', search='_search_is_expiring_soon')

    # ── Project details — available in export dialog (not shown in list by default) ──
    x_proj_start_date = fields.Date(
        string='Project Start Date',
        related='project_id.x_site_config_id.x_project_start_date',
        store=False, readonly=True,
        help='Project commencement date (from Site Configuration → Project Details).')
    x_proj_original_deadline = fields.Date(
        string='Project Original Deadline',
        related='project_id.x_site_config_id.x_project_deadline',
        store=False, readonly=True,
        help='Baseline contractual deadline before any EOTs.')
    x_proj_current_deadline = fields.Date(
        string='Project Current Deadline',
        related='project_id.x_site_config_id.x_current_deadline',
        store=False, readonly=True,
        help='Deadline after applying all approved Extensions of Time.')
    x_proj_contract_value = fields.Monetary(
        string='Contract Value',
        related='project_id.x_contract_value',
        currency_field='currency_id',
        store=False, readonly=True,
        help='Total project contract value with the client.')
    x_proj_billed_to_client = fields.Monetary(
        string='Billed to Client',
        related='project_id.x_billed_to_client',
        currency_field='currency_id',
        store=False, readonly=True,
        help='Cumulative client invoices posted for this project.')
    x_proj_work_completion_pct = fields.Float(
        string='Work Completion (%)',
        related='project_id.x_work_completion_pct',
        digits=(5, 1),
        store=False, readonly=True,
        help='Manual physical work completion percentage.')
    x_proj_financial_completion_pct = fields.Float(
        string='Financial Completion (%)',
        related='project_id.x_financial_completion_pct',
        digits=(16, 2),
        store=False, readonly=True,
        help='Billed to Client ÷ Contract Value × 100.')
    x_proj_remaining_to_bill = fields.Monetary(
        string='Remaining to Bill',
        related='project_id.x_remaining_work_value',
        currency_field='currency_id',
        store=False, readonly=True,
        help='Contract Value − Billed to Client.')
    x_proj_analytic_account = fields.Char(
        string='Project Analytic Account',
        related='project_id.x_analytic_account_id.name',
        store=False, readonly=True)
    validated = fields.Boolean(default=False, tracking=True, copy=False)
    validated_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    validated_date = fields.Datetime(readonly=True, copy=False)
    release_date = fields.Date(readonly=True, copy=False, tracking=True)
    release_notes = fields.Text(readonly=True, copy=False)
    x_expiry_alert_sent = fields.Boolean(
        string='Expiry Alert Sent', default=False, copy=False,
        help='True once the 10-day expiry alert email has been dispatched. '
             'Auto-reset when expiry date is extended.',
    )
    amendment_ids = fields.One2many(
        'x.bank.guarantee.amendment', 'guarantee_id', string='Amendment History')
    amendment_count = fields.Integer(
        string='Amendments', compute='_compute_amendment_count', store=True)
    cash_margin_slab_ids = fields.One2many(
        'x.bg.cash.margin.slab', 'bg_id', string='Cash Margin Slabs',
        help='Tiered cash margin rates for this BG. '
             'When slabs are present the total margin is computed from the slabs; '
             'the "Cash Margin (%)" flat field is ignored.')
    currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id)
    company_id = fields.Many2one(
        'res.company', default=lambda self: self.env.company, required=True)
    notes = fields.Text()

    @api.depends(
        'bg_amount', 'cash_margin_percent', 'pricing_percent', 'fed_percent',
        'cash_margin_slab_ids', 'cash_margin_slab_ids.slab_amount',
    )
    def _compute_charges(self):
        for rec in self:
            if rec.cash_margin_slab_ids:
                # Tiered: sum each slab's computed margin amount
                rec.margin_amount = sum(rec.cash_margin_slab_ids.mapped('slab_amount'))
            else:
                # Simple flat rate (backward-compatible with existing BGs)
                rec.margin_amount = rec.bg_amount * (rec.cash_margin_percent or 0.0) / 100.0
            rec.commission_amount = rec.bg_amount * (rec.pricing_percent or 0.0) / 100.0
            rec.fed_amount = rec.commission_amount * (rec.fed_percent or 0.0) / 100.0

    @api.depends('jv_type', 'jv_name', 'on_behalf_entity')
    def _compute_bidder_display(self):
        for rec in self:
            if rec.jv_type == 'jv':
                rec.bidder_display = rec.jv_name or 'Joint Venture'
            elif rec.jv_type == 'on_behalf':
                rec.bidder_display = rec.on_behalf_entity or 'On Behalf Of'
            else:
                rec.bidder_display = 'MPPL'

    @api.depends('amendment_ids')
    def _compute_amendment_count(self):
        for rec in self:
            rec.amendment_count = len(rec.amendment_ids)

    @api.depends('expiry_date', 'state')
    def _compute_days_to_expiry(self):
        today = fields.Date.context_today(self)
        for rec in self:
            if rec.expiry_date and rec.state in ('active', 'locked', 'pending'):
                rec.days_to_expiry = (rec.expiry_date - today).days
                rec.is_expiring_soon = 0 <= rec.days_to_expiry <= ALERT_DAYS
            else:
                rec.days_to_expiry = 0
                rec.is_expiring_soon = False

    @api.model
    def _search_is_expiring_soon(self, operator, value):
        if operator not in ('=', '!='):
            return []
        today = fields.Date.context_today(self)
        limit = today + timedelta(days=ALERT_DAYS)
        active_states = ('active', 'locked', 'pending')
        expiring_domain = [
            ('state', 'in', active_states),
            ('expiry_date', '>=', today),
            ('expiry_date', '<=', limit),
        ]
        is_true = (operator == '=' and value) or (operator == '!=' and not value)
        if is_true:
            return expiring_domain
        return [
            '|', '|',
            ('state', 'not in', active_states),
            ('expiry_date', '<', today),
            ('expiry_date', '>', limit),
        ]

    @api.onchange('journal_id')
    def _onchange_journal_id(self):
        if self.journal_id:
            facility = self.env['x.bank.guarantee.facility'].search([
                ('journal_id', '=', self.journal_id.id),
            ], limit=1)
            self.facility_id = facility
            if facility:
                self.pricing_percent = facility.commission_percent
                if facility.cash_margin_slab_ids:
                    # Facility has tiered slabs — copy them to this BG
                    self.cash_margin_slab_ids = [(5, 0, 0)]  # clear any existing
                    self.cash_margin_slab_ids = [
                        (0, 0, {
                            'from_amount': s.from_amount,
                            'to_amount': s.to_amount,
                            'margin_percent': s.margin_percent,
                        })
                        for s in facility.cash_margin_slab_ids
                    ]
                    self.cash_margin_percent = 0.0
                else:
                    # No slabs on facility — use the simple flat rate
                    self.cash_margin_slab_ids = [(5, 0, 0)]
                    self.cash_margin_percent = facility.cash_margin_percent

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
        # Skip during bulk imports (Excel import wizard sets this context)
        if self.env.context.get('no_check_limit'):
            return
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
        Analytic = self.env['account.analytic.account']
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                # project_analytic_account_id is related from project_id.account_id —
                # not in vals directly, so resolve from project_id when available.
                analytic_id = vals.get('project_analytic_account_id')
                if not analytic_id and vals.get('project_id'):
                    project = self.env['project.project'].browse(vals['project_id'])
                    analytic_id = project.account_id.id
                site_code = Analytic._matracon_site_code_for_id(analytic_id)
                vals['name'] = Analytic._matracon_ref_with_site('x.bank.guarantee', site_code)
            if not vals.get('sr_no'):
                vals['sr_no'] = self.env['ir.sequence'].next_by_code('x.bank.guarantee.sr') or ''
            journal_id = vals.get('journal_id')
            if journal_id and not vals.get('facility_id'):
                facility = self.env['x.bank.guarantee.facility'].search([
                    ('journal_id', '=', journal_id),
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
        result = super().write(vals)
        # When expiry date or state changes, refresh stored dashboard KPIs so
        # the BG expiry alert banner reflects current data without manual refresh.
        if vals.keys() & {'expiry_date', 'state'}:
            self._refresh_dashboards_after_bg_change()
        return result

    def _refresh_dashboards_after_bg_change(self):
        """Trigger a lightweight dashboard refresh after BG expiry/state change."""
        Dash = self.env['x.management.dashboard'].sudo()
        dashboards = Dash.search([])
        for dash in dashboards:
            try:
                Dash._refresh_dashboard_data(dash)
            except Exception as exc:
                # Never block BG save due to dashboard refresh failure
                _logger.warning('Dashboard refresh failed after BG change: %s', exc)

    def _ensure_facility(self):
        for rec in self:
            if not rec.journal_id:
                raise UserError(_('Select a bank / journal before submitting the guarantee.'))
            facility = self.env['x.bank.guarantee.facility'].search([
                ('journal_id', '=', rec.journal_id.id),
            ], limit=1)
            if not facility:
                raise UserError(_(
                    'No facility limit is configured for %(bank)s. '
                    'Ask Finance to set the bank\'s sanctioned BG limit first '
                    '(Accounting → Compliance → Bank Facility Limits).',
                    bank=rec.journal_id.display_name,
                ))
            if not rec.facility_id:
                rec.facility_id = facility
            if facility.total_limit <= 0:
                raise UserError(_(
                    'Bank facility limit for %(bank)s is zero. Set the sanctioned limit first.',
                    bank=rec.journal_id.display_name,
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
            # Close all pending activities (expiry alerts, etc.)
            matracon_notify.close_activities(rec)

    def action_amend(self):
        self.ensure_one()
        next_no = len(self.amendment_ids) + 1
        return {
            'type': 'ir.actions.act_window',
            'name': _('Amend Bank Guarantee'),
            'res_model': 'x.bank.guarantee.amendment',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_guarantee_id': self.id,
                'default_amendment_date': fields.Date.context_today(self),
                'default_amendment_no': next_no,
            },
        }

    def action_reset_draft(self):
        resettable = ('pending', 'active', 'locked', 'released', 'expired', 'cancelled')
        for rec in self.filtered(lambda r: r.state in resettable):
            rec.write({'state': 'draft'})
            matracon_notify.close_activities(rec)

    def action_cancel(self):
        for rec in self.filtered(lambda r: r.state in ('draft', 'pending')):
            rec.write({'state': 'cancelled'})
            matracon_notify.close_activities(rec)

    # ── Notification helpers ─────────────────────────────────────────────────

    @api.model
    def _get_expiry_alert_recipients(self):
        """Return set of all email addresses for BG expiry notifications."""
        config = self.env['x.bg.expiry.config'].sudo()._get_config()
        emails = set()
        for gid in [
            'site_operations.group_finance_ho',
            'purchase_demand_raise.group_ceo_approval',
            'purchase_demand_raise.group_matracon_admin',
        ]:
            grp = self.env.ref(gid, raise_if_not_found=False)
            if grp:
                for u in grp.sudo().users.filtered('active'):
                    if u.email:
                        emails.add(u.email)
        for p in config.recipient_partner_ids:
            if p.email:
                emails.add(p.email)
        if config.extra_emails:
            for line in config.extra_emails.splitlines():
                e = line.strip()
                if e and '@' in e:
                    emails.add(e)
        return emails

    def _send_expiry_notification_email(self, subject, intro_html):
        """Send a BG expiry alert email for each record in self."""
        recipient_emails = self._get_expiry_alert_recipients()
        if not recipient_emails:
            return
        email_to = ','.join(recipient_emails)
        for rec in self:
            body = Markup("""
<div style="font-family:Arial,sans-serif;font-size:14px;">
  <p>{intro}</p>
  <table style="border-collapse:collapse;width:480px;font-size:13px;">
    <tr style="background:#f8f8f8;">
      <td style="padding:6px 14px;font-weight:bold;width:40%;">Reference</td>
      <td style="padding:6px 14px;">{name}</td></tr>
    <tr>
      <td style="padding:6px 14px;font-weight:bold;">Guarantee No</td>
      <td style="padding:6px 14px;">{gno}</td></tr>
    <tr style="background:#f8f8f8;">
      <td style="padding:6px 14px;font-weight:bold;">Bank</td>
      <td style="padding:6px 14px;">{bank}</td></tr>
    <tr>
      <td style="padding:6px 14px;font-weight:bold;">Project</td>
      <td style="padding:6px 14px;">{project}</td></tr>
    <tr style="background:#f8f8f8;">
      <td style="padding:6px 14px;font-weight:bold;">Nature</td>
      <td style="padding:6px 14px;">{nature}</td></tr>
    <tr>
      <td style="padding:6px 14px;font-weight:bold;">BG Amount</td>
      <td style="padding:6px 14px;">{amount}</td></tr>
    <tr style="background:#fff3cd;">
      <td style="padding:6px 14px;font-weight:bold;">Expiry Date</td>
      <td style="padding:6px 14px;color:#dc3545;font-weight:bold;">{expiry}</td></tr>
  </table>
  <p style="font-size:11px;color:#888;margin-top:16px;">
    — Matracon Pakistan Pvt. Ltd. · System Notification
  </p>
</div>""").format(
                intro=Markup(intro_html),
                name=rec.name,
                gno=rec.guarantee_number or '-',
                bank=rec.journal_id.name or '-',
                project=rec.project_id.name or '-',
                nature=rec.nature_id.name or '-',
                amount=f'{rec.bg_amount:,.0f} {rec.currency_id.symbol}',
                expiry=rec.expiry_date.strftime('%d %b %Y') if rec.expiry_date else '-',
            )
            self.env['mail.mail'].sudo().create({
                'subject': subject.format(ref=rec.name, guarantee=rec.guarantee_number or '-'),
                'body_html': body,
                'email_to': email_to,
                'auto_delete': True,
            }).send()

    def _cancel_expiry_activities(self):
        """Cancel all pending expiry-related mail activities on self."""
        self.env['mail.activity'].search([
            ('res_model', '=', 'x.bank.guarantee'),
            ('res_id', 'in', self.ids),
            ('summary', 'ilike', 'BG expir'),
        ]).unlink()

    def _schedule_expiry_alert(self):
        """Schedule Odoo activity for users 10 days before expiry
        and a second one on the actual expiry date."""
        today = fields.Date.context_today(self)
        fin_grp = self.env.ref('site_operations.group_finance_ho', raise_if_not_found=False)
        ceo_grp = self.env.ref('purchase_demand_raise.group_ceo_approval', raise_if_not_found=False)
        adm_grp = self.env.ref('purchase_demand_raise.group_matracon_admin', raise_if_not_found=False)
        group_ids = [g.id for g in (fin_grp, ceo_grp, adm_grp) if g]
        users = self.env['res.users'].search([('group_ids', 'in', group_ids)])

        for rec in self.filtered(lambda r: r.expiry_date and r.state == 'active'):
            alert_date = rec.expiry_date - timedelta(days=ALERT_DAYS)
            if alert_date >= today:
                # Activity: 10-day advance warning
                matracon_notify.schedule_activity(
                    rec, users,
                    _('BG expiring on %(date)s', date=rec.expiry_date),
                    note=_('Bank Guarantee %(ref)s (%(guarantee)s) expires in %(days)s days.',
                           ref=rec.name, guarantee=rec.guarantee_number,
                           days=ALERT_DAYS),
                )
            if rec.expiry_date >= today:
                # Activity: on the actual expiry date
                matracon_notify.schedule_activity(
                    rec, users,
                    _('BG EXPIRED: %(ref)s', ref=rec.name),
                    note=_('Bank Guarantee %(ref)s (%(guarantee)s) expired today. '
                           'Arrange release or renewal immediately.',
                           ref=rec.name, guarantee=rec.guarantee_number),
                )

    @api.model
    def _cron_alert_expiring_bgs(self):
        """Daily cron: send email alert for BGs entering the %(days)s-day expiry window."""
        today = fields.Date.context_today(self)
        threshold = today + timedelta(days=ALERT_DAYS)
        entering = self.search([
            ('state', 'in', ('active', 'locked')),
            ('expiry_date', '>', today),
            ('expiry_date', '<=', threshold),
            ('x_expiry_alert_sent', '=', False),
        ])
        for rec in entering:
            rec._send_expiry_notification_email(
                subject='⚠️ BG Expiry Alert — {ref} expires in %d days' % ALERT_DAYS,
                intro_html=(
                    'Bank Guarantee <strong>{ref}</strong> '
                    '(Guarantee No: <strong>{guarantee}</strong>) is expiring in '
                    '<strong>%d day(s)</strong>. Please arrange renewal or release.'
                ) % ALERT_DAYS,
            )
            rec.x_expiry_alert_sent = True
            # Push in-app notification as well
            matracon_notify.notify_group(
                rec, 'site_operations.group_finance_ho',
                _('<b>%(ref)s</b> expires in %(days)s days — action required.',
                  ref=rec.name, days=ALERT_DAYS),
                summary=_('BG Expiry Alert'),
            )

    @api.model
    def _cron_expire_guarantees(self):
        today = fields.Date.context_today(self)
        expiring = self.search([
            ('state', 'in', ('active', 'locked')),
            ('expiry_date', '<', today),
        ])
        fin_grp = self.env.ref('site_operations.group_finance_ho', raise_if_not_found=False)
        ceo_grp = self.env.ref('purchase_demand_raise.group_ceo_approval', raise_if_not_found=False)
        adm_grp = self.env.ref('purchase_demand_raise.group_matracon_admin', raise_if_not_found=False)
        group_ids = [g.id for g in (fin_grp, ceo_grp, adm_grp) if g]
        users = self.env['res.users'].search([('group_ids', 'in', group_ids)])
        for rec in expiring:
            rec.state = 'expired'
            # In-app notification + push
            matracon_notify.notify_users(
                rec, users,
                _('Bank Guarantee <b>%(ref)s</b> has <b>expired</b> on %(date)s.',
                  ref=rec.name, date=rec.expiry_date),
                summary=_('BG Expired'),
            )
            # Odoo activity on expiry day
            matracon_notify.schedule_activity(
                rec, users,
                _('BG EXPIRED: %(ref)s', ref=rec.name),
                note=_('Bank Guarantee %(ref)s expired on %(date)s. '
                       'Take immediate action — release or renew.',
                       ref=rec.name, date=rec.expiry_date),
            )
            # Email to all configured recipients
            rec._send_expiry_notification_email(
                subject='🔴 BG Expired — {ref} ({guarantee})',
                intro_html=(
                    'Bank Guarantee <strong>{ref}</strong> '
                    '(Guarantee No: <strong>{guarantee}</strong>) has '
                    '<span style="color:#dc3545;font-weight:bold;">EXPIRED</span>. '
                    'Immediate action required — release or renewal.'
                ),
            )

    def action_print_bank_guarantee(self):
        return self.env.ref(
            'site_operations.action_report_bank_guarantee'
        ).report_action(self)

    def action_direct_print_bg(self):
        """Open the Bank Guarantee report in a new tab and auto-trigger print dialog."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'url': f'/site_operations/print/x.bank.guarantee/{self.id}',
            'target': 'new',
        }

    def unlink(self):
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_('Only draft bank guarantees can be deleted.'))
        return super().unlink()

    def action_delete_draft(self):
        self.unlink()
        return {'type': 'ir.actions.act_window_close'}

    def action_open_import_wizard(self):
        """Open the BG import wizard — called from the list view header button."""
        return {
            'type': 'ir.actions.act_window',
            'name': 'Import BGs from Excel',
            'res_model': 'x.bg.import.wizard',
            'view_mode': 'form',
            'target': 'new',
        }


class BankGuaranteeAmendment(models.Model):
    _name = 'x.bank.guarantee.amendment'
    _description = 'Bank Guarantee Amendment'
    _order = 'amendment_date asc, id asc'

    guarantee_id = fields.Many2one(
        'x.bank.guarantee', required=True, ondelete='cascade', index=True)
    amendment_no = fields.Integer(
        string='No.', readonly=True, copy=False, default=0,
        help='Sequential amendment number within this bank guarantee (1-based, ordered by creation ID).')
    amendment_date = fields.Date(
        string='Date', required=True, default=fields.Date.context_today)
    amendment_type = fields.Selection([
        ('initial', 'Original'),
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
    attachment_ids = fields.Many2many(
        'ir.attachment',
        'x_bg_amendment_attachment_rel',
        'amendment_id', 'attachment_id',
        string='Supporting Documents',
        help='Attach bank letters, extensions, or other supporting documentation for this amendment.',
    )

    # ── Related fields from parent BG — available as columns in standalone list / export ──
    bg_guarantee_number = fields.Char(
        related='guarantee_id.guarantee_number', string='Guarantee No',
        store=False, readonly=True)
    bg_journal_id = fields.Many2one(
        related='guarantee_id.journal_id', string='Bank',
        store=False, readonly=True)
    bg_project_id = fields.Many2one(
        related='guarantee_id.project_id', string='Project',
        store=False, readonly=True)
    bg_beneficiary_name = fields.Char(
        related='guarantee_id.beneficiary_name', string='Beneficiary',
        store=False, readonly=True)
    bg_state = fields.Selection(
        related='guarantee_id.state', string='BG Status',
        store=False, readonly=True)
    bg_amount = fields.Monetary(
        related='guarantee_id.bg_amount', string='BG Amount',
        currency_field='currency_id', store=False, readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        # Assign amendment_no sequentially at creation time.
        # We group by guarantee_id so a bulk create across multiple BGs works correctly.
        bg_counts = {}
        for vals in vals_list:
            bg_id = vals.get('guarantee_id')
            if bg_id and not vals.get('amendment_no'):
                if bg_id not in bg_counts:
                    bg_counts[bg_id] = self.search_count([('guarantee_id', '=', bg_id)])
                bg_counts[bg_id] += 1
                vals['amendment_no'] = bg_counts[bg_id]
        records = super().create(vals_list)
        # Auto-apply extension immediately when added via inline list or any other path
        records.filtered(
            lambda a: a.amendment_type == 'extension' and a.new_expiry_date
        )._do_apply_extension()
        return records

    def write(self, vals):
        result = super().write(vals)
        # Re-apply whenever new_expiry_date or amendment_type changes on an extension line
        if 'new_expiry_date' in vals or 'amendment_type' in vals:
            self.filtered(
                lambda a: a.amendment_type == 'extension' and a.new_expiry_date
            )._do_apply_extension()
        return result

    def _do_apply_extension(self):
        """Core logic: update parent BG expiry date and reschedule alerts.
        Called both from action_apply_extension (button) and from create/write hooks."""
        for rec in self.filtered(lambda a: a.new_expiry_date and a.guarantee_id):
            bg = rec.guarantee_id
            bg.expiry_date = rec.new_expiry_date
            if bg.state == 'expired':
                bg.state = 'active'
            # Cancel any pending expiry activities — they were based on old date
            bg._cancel_expiry_activities()
            # Reset alert flag so the cron re-triggers if still within window
            bg.x_expiry_alert_sent = False
            # Re-schedule activities with updated expiry date
            bg._schedule_expiry_alert()

    def action_apply_extension(self):
        """Button handler — delegates to _do_apply_extension."""
        self._do_apply_extension()

    def action_open_import_wizard(self):
        """Open the BG Amendment import wizard from the list view header."""
        return {
            'type': 'ir.actions.act_window',
            'name': 'Import BG Amendments',
            'res_model': 'x.bg.amendment.import.wizard',
            'view_mode': 'form',
            'target': 'new',
        }


class BankGuaranteeFacilityRenewal(models.Model):
    """Historical record of each facility sanction letter before renewal."""
    _name = 'x.bank.guarantee.facility.renewal'
    _description = 'Bank Guarantee Facility Renewal History'
    _order = 'date_of_issuance desc, id desc'

    facility_id = fields.Many2one(
        'x.bank.guarantee.facility', required=True,
        ondelete='cascade', index=True)
    reference_no = fields.Char(string='Reference No')
    date_of_issuance = fields.Date(string='Date of Issuance')
    expiry_date = fields.Date(string='Expiry Date')
    total_limit = fields.Monetary(
        string='Sanctioned Limit', currency_field='currency_id')
    cash_margin_percent = fields.Float(
        string='Cash Margin (%)', digits=(5, 2))
    commission_percent = fields.Float(
        string='Commission (%)', digits=(5, 2))
    notes = fields.Text(string='Notes')
    currency_id = fields.Many2one(
        related='facility_id.currency_id', store=True, readonly=True)
