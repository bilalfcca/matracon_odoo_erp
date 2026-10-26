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
    cash_margin_account_id = fields.Many2one(
        'account.account', string='Cash Margin Account',
        domain="[('x_is_main', '=', False), ('deprecated', '=', False)]",
        help='Override cash margin account for guarantees issued under this facility. '
             'If left empty, the bank journal\'s cash margin account is used.',
    )
    total_limit = fields.Monetary(
        string='Total Sanctioned Limit', required=True, tracking=True,
        currency_field='currency_id',
        help='Overall bank guarantee facility limit sanctioned by the bank.',
    )
    utilized_amount = fields.Monetary(
        compute='_compute_utilization', store=True,
        currency_field='currency_id',
    )
    utilized_margin_amount = fields.Monetary(
        compute='_compute_utilization', store=True,
        string='Cash Margin Held',
        currency_field='currency_id',
        help='Total cash margin locked in bank cash margin accounts for active/locked guarantees under this facility.',
    )
    released_amount = fields.Monetary(
        compute='_compute_utilization', store=True,
        string='Released Amount',
        currency_field='currency_id',
        help='Total guarantee amount released under this facility.',
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
        'guarantee_ids.state', 'guarantee_ids.bg_amount', 'guarantee_ids.margin_amount', 'total_limit',
    )
    def _compute_utilization(self):
        active_states = ('pending', 'active', 'locked')
        for rec in self:
            utilized = sum(rec.guarantee_ids.filtered(
                lambda g: g.state in active_states
            ).mapped('bg_amount'))
            rec.utilized_amount = utilized
            rec.available_limit = rec.total_limit - utilized
            rec.utilized_margin_amount = sum(rec.guarantee_ids.filtered(
                lambda g: g.state in ('active', 'locked')
            ).mapped('margin_amount'))
            rec.released_amount = sum(rec.guarantee_ids.filtered(
                lambda g: g.state == 'released'
            ).mapped('bg_amount'))

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
        index=True, domain="[('type', '=', 'bank'), ('bg_facility_ids', '!=', False)]",
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
    project_name = fields.Char(
        string='Manual Project Name', tracking=True,
        help='Manual project name if the project is not from our registered projects.',
    )
    project_display = fields.Char(
        string='Project Display', compute='_compute_project_display', store=True,
        help='Display project name: either manual project name or linked project name.',
    )

    @api.depends('project_id', 'project_name')
    def _compute_project_display(self):
        for rec in self:
            rec.project_display = rec.project_name or rec.project_id.name or ''

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
    x_margin_move_id = fields.Many2one(
        'account.move', string='Margin Journal Entry',
        readonly=True, copy=False, ondelete='set null',
        help='Journal entry recording the cash margin deduction on guarantee activation.',
    )
    x_margin_release_move_id = fields.Many2one(
        'account.move', string='Margin Release Entry',
        readonly=True, copy=False, ondelete='set null',
        help='Journal entry recording the cash margin release back to the bank operational account.',
    )
    x_move_count = fields.Integer(
        string='Journal Entries Count',
        compute='_compute_x_move_count',
    )
    released_amount = fields.Monetary(
        string='Released Amount',
        compute='_compute_released_amount', store=True,
        currency_field='currency_id',
        help='Amount released for this bank guarantee.',
    )
    currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id)
    company_id = fields.Many2one(
        'res.company', default=lambda self: self.env.company, required=True)
    notes = fields.Text()

    @api.depends('state', 'bg_amount')
    def _compute_released_amount(self):
        for rec in self:
            rec.released_amount = rec.bg_amount if rec.state == 'released' else 0.0

    @api.depends('x_margin_move_id', 'x_margin_release_move_id')
    def _compute_x_move_count(self):
        for rec in self:
            moves = rec.x_margin_move_id | rec.x_margin_release_move_id
            rec.x_move_count = len(moves)

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

    @api.onchange('project_id')
    def _onchange_project_id(self):
        if self.project_id and not self.project_name:
            self.project_name = self.project_id.name

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

    def _get_bank_account(self):
        """Resolve the operational leaf account (Useable Balance) for this BG's journal.
        Must not be a Main/Group account where direct posting is restricted.
        """
        self.ensure_one()
        journal = self.journal_id
        if not journal:
            raise UserError(_('No bank journal selected for guarantee %s.') % (self.name or self.guarantee_number))
        bank_acc = journal.default_account_id
        if not bank_acc:
            raise UserError(_("No default account configured for bank journal '%s'.") % journal.name)
        # If the journal's default account is marked as main, find its Useable Balance ('1') leaf child
        if bank_acc.x_is_main:
            leaf = self.env['account.account'].sudo().search([
                ('company_ids', 'in', [self.env.company.id]),
                ('x_parent_account_id', '=', bank_acc.id),
                ('code', '=', bank_acc.code + '1'),
            ], limit=1)
            if leaf:
                return leaf
        return bank_acc

    def _get_cash_margin_account(self):
        """Resolve the Cash Margin leaf account for this BG's bank/journal.
        Checks in priority order:
          1. Facility override (facility_id.cash_margin_account_id)
          2. Journal override (journal_id.x_cash_margin_account_id)
          3. Suffix '2' child under default_account_id.x_parent_account_id
          4. Code pattern: default_account_id.code ending in '1' -> replace with '2'
          5. Code pattern: bank_code + '2'
          6. Sibling account by name containing 'Cash Margin'
          7. Fallback to any company leaf account named 'Cash Margin'
        """
        self.ensure_one()
        journal = self.journal_id
        if not journal:
            raise UserError(_('No bank journal selected for guarantee %s.') % (self.name or self.guarantee_number))

        Account = self.env['account.account'].sudo()
        company_id = self.company_id.id if hasattr(self, 'company_id') and self.company_id else self.env.company.id

        # 1. Facility override
        if self.facility_id and self.facility_id.cash_margin_account_id:
            return self.facility_id.cash_margin_account_id

        # 2. Journal override
        if journal.x_cash_margin_account_id:
            return journal.x_cash_margin_account_id

        bank_acc = journal.default_account_id
        if bank_acc:
            # 3. Parent's child with suffix '2' or named 'Cash Margin'
            parent = bank_acc.x_parent_account_id
            if parent:
                margin_acc = Account.search([
                    ('company_ids', 'in', [company_id]),
                    ('x_is_main', '=', False),
                    ('x_parent_account_id', '=', parent.id),
                    '|',
                    ('code', '=', parent.code + '2'),
                    ('name', 'ilike', 'Cash Margin'),
                ], limit=1)
                if margin_acc:
                    return margin_acc

            # 4. If code ends in '1' (Useable Balance child), look for code ending in '2'
            if bank_acc.code and bank_acc.code.endswith('1'):
                target_code = bank_acc.code[:-1] + '2'
                margin_acc = Account.search([
                    ('company_ids', 'in', [company_id]),
                    ('x_is_main', '=', False),
                    ('code', '=', target_code),
                ], limit=1)
                if margin_acc:
                    return margin_acc

            # 5. Look for bank_acc.code + '2'
            margin_acc = Account.search([
                ('company_ids', 'in', [company_id]),
                ('x_is_main', '=', False),
                ('code', '=', bank_acc.code + '2'),
            ], limit=1)
            if margin_acc:
                return margin_acc

        # 6. Search by bank/journal name + Cash Margin
        if journal.name:
            margin_acc = Account.search([
                ('company_ids', 'in', [company_id]),
                ('x_is_main', '=', False),
                ('name', 'ilike', journal.name),
                ('name', 'ilike', 'Cash Margin'),
            ], limit=1)
            if margin_acc:
                return margin_acc

        # 7. General fallback: any leaf account named Cash Margin
        margin_acc = Account.search([
            ('company_ids', 'in', [company_id]),
            ('x_is_main', '=', False),
            ('name', 'ilike', 'Cash Margin'),
        ], limit=1)
        if margin_acc:
            return margin_acc

        raise UserError(_(
            "Could not determine the Cash Margin account for bank '%s'. "
            "Please configure the Cash Margin Account on the bank journal or facility limit."
        ) % journal.name)

    def _create_margin_activation_move(self):
        """Create and post the Cash Margin journal entry for guarantee activation:
        Debit: Bank Cash Margin Account (increases cash margin asset)
        Credit: Bank Operational Account (decreases operational bank balance)
        """
        self.ensure_one()
        if self.margin_amount <= 0:
            return False

        if self.x_margin_move_id and self.x_margin_move_id.state == 'posted':
            return self.x_margin_move_id

        bank_acc = self._get_bank_account()
        margin_acc = self._get_cash_margin_account()
        ref_label = _("BG %(ref)s (%(num)s) - Cash Margin") % {
            'ref': self.name,
            'num': self.guarantee_number,
        }
        entry_date = self.issue_date or fields.Date.context_today(self)
        analytic_dist = {str(self.project_analytic_account_id.id): 100} if self.project_analytic_account_id else False

        existing_move = self.x_margin_move_id.sudo()
        move_vals = {
            'move_type': 'entry',
            'journal_id': self.journal_id.id,
            'ref': ref_label,
            'date': entry_date,
            'x_project_analytic_account_id': self.project_analytic_account_id.id if self.project_analytic_account_id else False,
            'line_ids': [
                (0, 0, {
                    'name': _("Cash Margin Held - BG %(num)s") % {'num': self.guarantee_number},
                    'account_id': margin_acc.id,
                    'partner_id': self.beneficiary_id.id or False,
                    'debit': self.margin_amount,
                    'credit': 0.0,
                    'analytic_distribution': analytic_dist,
                }),
                (0, 0, {
                    'name': _("Cash Margin Deducted - BG %(num)s") % {'num': self.guarantee_number},
                    'account_id': bank_acc.id,
                    'partner_id': self.beneficiary_id.id or False,
                    'debit': 0.0,
                    'credit': self.margin_amount,
                    'analytic_distribution': analytic_dist,
                }),
            ],
        }

        if existing_move and existing_move.state == 'draft':
            existing_move.line_ids.unlink()
            existing_move.write(move_vals)
            existing_move.action_post()
            move = existing_move
        else:
            move = self.env['account.move'].sudo().create(move_vals)
            move.action_post()
            self.x_margin_move_id = move.id

        self.message_post(body=Markup(_(
            "Journal entry <b>%(move)s</b> posted for Cash Margin of <b>%(amount)s</b> "
            "(Dr: %(margin_acc)s, Cr: %(bank_acc)s)."
        )) % {
            'move': move.name,
            'amount': f"{self.currency_id.symbol} {self.margin_amount:,.2f}",
            'margin_acc': f"{margin_acc.code} {margin_acc.name}",
            'bank_acc': f"{bank_acc.code} {bank_acc.name}",
        })
        return move

    def _create_margin_release_move(self):
        """Create and post the Cash Margin Release journal entry:
        Debit: Bank Operational Account (returns cash margin back to operational balance)
        Credit: Bank Cash Margin Account (clears cash margin asset)
        """
        self.ensure_one()
        if self.margin_amount <= 0:
            return False

        if self.x_margin_release_move_id and self.x_margin_release_move_id.state == 'posted':
            return self.x_margin_release_move_id

        bank_acc = self._get_bank_account()
        margin_acc = self._get_cash_margin_account()
        ref_label = _("BG %(ref)s (%(num)s) - Cash Margin Released") % {
            'ref': self.name,
            'num': self.guarantee_number,
        }
        rel_date = self.release_date or fields.Date.context_today(self)
        analytic_dist = {str(self.project_analytic_account_id.id): 100} if self.project_analytic_account_id else False

        existing_move = self.x_margin_release_move_id.sudo()
        move_vals = {
            'move_type': 'entry',
            'journal_id': self.journal_id.id,
            'ref': ref_label,
            'date': rel_date,
            'x_project_analytic_account_id': self.project_analytic_account_id.id if self.project_analytic_account_id else False,
            'line_ids': [
                (0, 0, {
                    'name': _("Cash Margin Released - BG %(num)s") % {'num': self.guarantee_number},
                    'account_id': bank_acc.id,
                    'partner_id': self.beneficiary_id.id or False,
                    'debit': self.margin_amount,
                    'credit': 0.0,
                    'analytic_distribution': analytic_dist,
                }),
                (0, 0, {
                    'name': _("Cash Margin Cleared - BG %(num)s") % {'num': self.guarantee_number},
                    'account_id': margin_acc.id,
                    'partner_id': self.beneficiary_id.id or False,
                    'debit': 0.0,
                    'credit': self.margin_amount,
                    'analytic_distribution': analytic_dist,
                }),
            ],
        }

        if existing_move and existing_move.state == 'draft':
            existing_move.line_ids.unlink()
            existing_move.write(move_vals)
            existing_move.action_post()
            move = existing_move
        else:
            move = self.env['account.move'].sudo().create(move_vals)
            move.action_post()
            self.x_margin_release_move_id = move.id

        self.message_post(body=Markup(_(
            "Journal entry <b>%(move)s</b> posted for Cash Margin Release of <b>%(amount)s</b> "
            "(Dr: %(bank_acc)s, Cr: %(margin_acc)s)."
        )) % {
            'move': move.name,
            'amount': f"{self.currency_id.symbol} {self.margin_amount:,.2f}",
            'bank_acc': f"{bank_acc.code} {bank_acc.name}",
            'margin_acc': f"{margin_acc.code} {margin_acc.name}",
        })
        return move

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
            # Post cash margin journal entry
            rec._create_margin_activation_move()
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
            # Post cash margin release journal entry
            rec._create_margin_release_move()
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
            # 1. Reset and unlink release move if any
            if rec.x_margin_release_move_id:
                rel_move = rec.x_margin_release_move_id.sudo()
                if rel_move.state == 'posted':
                    rel_move.button_draft()
                try:
                    rel_move.unlink()
                    rec.x_margin_release_move_id = False
                except Exception:
                    pass

            # 2. Reset and unlink activation margin move if any
            if rec.x_margin_move_id:
                act_move = rec.x_margin_move_id.sudo()
                if act_move.state == 'posted':
                    act_move.button_draft()
                try:
                    act_move.unlink()
                    rec.x_margin_move_id = False
                except Exception:
                    pass

            rec.write({
                'state': 'draft',
                'release_date': False,
            })
            matracon_notify.close_activities(rec)
            rec.message_post(body=_(
                "Bank Guarantee reset to Draft. Associated cash margin journal entries "
                "have been reset to draft/unlinked, ensuring no impact remains on the Chart of Accounts."
            ))

    def action_view_journal_entries(self):
        self.ensure_one()
        moves = self.x_margin_move_id | self.x_margin_release_move_id
        action = {
            'type': 'ir.actions.act_window',
            'name': _('Journal Entries — %s') % (self.name or self.guarantee_number),
            'res_model': 'account.move',
            'domain': [('id', 'in', moves.ids)],
            'context': {'create': False},
        }
        if len(moves) == 1:
            action['view_mode'] = 'form'
            action['res_id'] = moves.id
        else:
            action['view_mode'] = 'list,form'
        return action

    @api.model
    def action_admin_repair_bg_accounting(self, dry_run=False):
        """Audit and repair accounting entries for all historical and live Bank Guarantees.
        
        Eligible records:
          - State in ('active', 'locked', 'released') with margin_amount > 0.
        
        Duplicate detection:
          - Checks if rec.x_margin_move_id or rec.x_margin_release_move_id is already posted.
          - Searches account.move in the bank journal matching the BG name/number and margin amount
            to link pre-existing unlinked entries instead of creating duplicate moves.
          - Preserves existing posted accounting data.
        
        Returns:
          dict with counts and HTML audit report.
        """
        all_bgs = self.sudo().search([], order='journal_id, id')
        Move = self.env['account.move'].sudo()

        total_scanned = len(all_bgs)
        total_created = 0
        total_repaired = 0
        total_skipped = 0
        total_failed = 0
        report_rows = []

        for bg in all_bgs:
            state = bg.state
            margin = bg.margin_amount or 0.0
            row_info = {
                'id': bg.id,
                'name': bg.name or '-',
                'number': bg.guarantee_number or '-',
                'bank': bg.journal_id.name or '-',
                'state': state,
                'margin': margin,
                'action_taken': [],
                'status': 'skipped',
                'detail': '',
            }

            if state in ('draft', 'pending', 'cancelled'):
                total_skipped += 1
                row_info['status'] = 'skipped'
                row_info['detail'] = _("State is %s — no accounting entry required.") % state
                report_rows.append(row_info)
                continue

            if margin <= 0:
                total_skipped += 1
                row_info['status'] = 'skipped'
                row_info['detail'] = _("Cash Margin is 0.00 — no entry required.")
                report_rows.append(row_info)
                continue

            created_for_bg = 0
            repaired_for_bg = 0
            failed_reason = False

            # ── 1. Activation Cash Margin Entry ──────────────────────────────
            act_move = bg.x_margin_move_id
            if act_move and act_move.state == 'posted':
                row_info['action_taken'].append(_("Activation entry already posted (%s)") % act_move.name)
            else:
                dup_move = False
                if bg.guarantee_number:
                    dup_move = Move.search([
                        ('journal_id', '=', bg.journal_id.id),
                        ('state', '=', 'posted'),
                        ('ref', 'ilike', bg.guarantee_number),
                        ('ref', 'notilike', 'Released'),
                    ], limit=1)
                if not dup_move and bg.name:
                    dup_move = Move.search([
                        ('journal_id', '=', bg.journal_id.id),
                        ('state', '=', 'posted'),
                        ('ref', 'ilike', bg.name),
                        ('ref', 'notilike', 'Released'),
                    ], limit=1)

                if dup_move:
                    repaired_for_bg += 1
                    row_info['action_taken'].append(_("Linked existing activation entry (%s)") % dup_move.name)
                    if not dry_run:
                        bg.x_margin_move_id = dup_move.id
                else:
                    try:
                        bg._get_bank_account()
                        bg._get_cash_margin_account()
                        if dry_run:
                            created_for_bg += 1
                            row_info['action_taken'].append(_("Will create Cash Margin activation entry (Dr Margin / Cr Bank)"))
                        else:
                            new_move = bg._create_margin_activation_move()
                            if new_move:
                                created_for_bg += 1
                                row_info['action_taken'].append(_("Created Cash Margin activation entry (%s)") % new_move.name)
                    except Exception as e:
                        failed_reason = str(e)

            # ── 2. Release Cash Margin Entry (if released) ───────────────────
            if state == 'released' and not failed_reason:
                rel_move = bg.x_margin_release_move_id
                if rel_move and rel_move.state == 'posted':
                    row_info['action_taken'].append(_("Release entry already posted (%s)") % rel_move.name)
                else:
                    dup_rel_move = False
                    if bg.guarantee_number:
                        dup_rel_move = Move.search([
                            ('journal_id', '=', bg.journal_id.id),
                            ('state', '=', 'posted'),
                            ('ref', 'ilike', bg.guarantee_number),
                            ('ref', 'ilike', 'Released'),
                        ], limit=1)
                    if not dup_rel_move and bg.name:
                        dup_rel_move = Move.search([
                            ('journal_id', '=', bg.journal_id.id),
                            ('state', '=', 'posted'),
                            ('ref', 'ilike', bg.name),
                            ('ref', 'ilike', 'Released'),
                        ], limit=1)

                    if dup_rel_move:
                        repaired_for_bg += 1
                        row_info['action_taken'].append(_("Linked existing release entry (%s)") % dup_rel_move.name)
                        if not dry_run:
                            bg.x_margin_release_move_id = dup_rel_move.id
                    else:
                        try:
                            bg._get_bank_account()
                            bg._get_cash_margin_account()
                            if dry_run:
                                created_for_bg += 1
                                row_info['action_taken'].append(_("Will create Cash Margin release entry (Dr Bank / Cr Margin)"))
                            else:
                                new_rel_move = bg._create_margin_release_move()
                                if new_rel_move:
                                    created_for_bg += 1
                                    row_info['action_taken'].append(_("Created Cash Margin release entry (%s)") % new_rel_move.name)
                        except Exception as e:
                            failed_reason = str(e)

            if failed_reason:
                total_failed += 1
                row_info['status'] = 'failed'
                row_info['detail'] = failed_reason
            elif created_for_bg > 0:
                total_created += created_for_bg
                row_info['status'] = 'created'
                row_info['detail'] = '; '.join(row_info['action_taken'])
            elif repaired_for_bg > 0:
                total_repaired += repaired_for_bg
                row_info['status'] = 'repaired'
                row_info['detail'] = '; '.join(row_info['action_taken'])
            else:
                total_skipped += 1
                row_info['status'] = 'skipped'
                row_info['detail'] = '; '.join(row_info['action_taken']) or _("Already up to date.")

            report_rows.append(row_info)

        report_html = self._format_repair_audit_html(
            total_scanned, total_created, total_repaired, total_skipped, total_failed, report_rows, dry_run
        )

        return {
            'total_scanned': total_scanned,
            'total_created': total_created,
            'total_repaired': total_repaired,
            'total_skipped': total_skipped,
            'total_failed': total_failed,
            'report_html': report_html,
        }

    @api.model
    def _format_repair_audit_html(self, total_scanned, total_created, total_repaired, total_skipped, total_failed, report_rows, dry_run):
        run_mode_badge = (
            '<span class="badge bg-warning text-dark px-2 py-1 fs-6">Preview / Dry Run</span>'
            if dry_run else
            '<span class="badge bg-success text-white px-2 py-1 fs-6">Live Repair Applied</span>'
        )

        cards_html = f"""
        <div class="mb-3 d-flex flex-wrap gap-2 align-items-center">
            {run_mode_badge}
            <span class="badge bg-secondary p-2">Scanned: {total_scanned}</span>
            <span class="badge bg-primary p-2">{"To Create" if dry_run else "Created"}: {total_created}</span>
            <span class="badge bg-info text-dark p-2">{"To Link" if dry_run else "Linked"}: {total_repaired}</span>
            <span class="badge bg-light text-dark border p-2">Skipped: {total_skipped}</span>
            <span class="badge bg-danger p-2">Failed: {total_failed}</span>
        </div>
        """

        rows_html = []
        for i, r in enumerate(report_rows, start=1):
            status = r['status']
            if status == 'created':
                badge = '<span class="badge bg-success">Created</span>' if not dry_run else '<span class="badge bg-primary">Eligible</span>'
            elif status == 'repaired':
                badge = '<span class="badge bg-info text-dark">Linked (No Dup)</span>'
            elif status == 'failed':
                badge = '<span class="badge bg-danger">Failed</span>'
            else:
                badge = '<span class="badge bg-secondary">Skipped</span>'

            state_badge = f'<span class="badge bg-light text-dark border">{r["state"]}</span>'
            margin_str = f"{r['margin']:,.2f}"

            rows_html.append(f"""
            <tr>
                <td class="text-center">{i}</td>
                <td><strong>{r['name']}</strong></td>
                <td>{r['number']}</td>
                <td>{r['bank']}</td>
                <td class="text-center">{state_badge}</td>
                <td class="text-end font-monospace">{margin_str}</td>
                <td class="text-center">{badge}</td>
                <td class="small text-muted">{r['detail']}</td>
            </tr>
            """)

        table_html = f"""
        {cards_html}
        <div class="table-responsive" style="max-height: 480px; overflow-y: auto;">
            <table class="table table-sm table-striped table-hover align-middle border">
                <thead class="table-dark sticky-top">
                    <tr>
                        <th style="width: 40px;" class="text-center">#</th>
                        <th>BG Ref</th>
                        <th>Guarantee No</th>
                        <th>Bank Journal</th>
                        <th class="text-center">State</th>
                        <th class="text-end">Cash Margin</th>
                        <th class="text-center">Status</th>
                        <th>Audit Details / Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(rows_html) if rows_html else '<tr><td colspan="8" class="text-center text-muted py-3">No records found.</td></tr>'}
                </tbody>
            </table>
        </div>
        """
        return table_html

    @api.model
    def action_open_bg_repair_wizard(self):
        """Open the Bank Guarantee Accounting Repair Wizard from Admin -> Fix Tools."""
        return {
            'type': 'ir.actions.act_window',
            'name': _('Audit & Repair Bank Guarantee Accounting'),
            'res_model': 'x.bg.accounting.repair.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': self.env.context,
        }

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
        # res.groups no longer has a .users field in Odoo 19 — query res.users directly
        grp_ids = [
            g.id
            for xmlid in (
                'site_operations.group_finance_ho',
                'purchase_demand_raise.group_ceo_approval',
                'purchase_demand_raise.group_matracon_admin',
            )
            for g in [self.env.ref(xmlid, raise_if_not_found=False)]
            if g
        ]
        if grp_ids:
            for u in self.env['res.users'].sudo().search([
                ('active', '=', True),
                ('group_ids', 'in', grp_ids),
            ]):
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
                project=rec.project_display or rec.project_name or rec.project_id.name or '-',
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
