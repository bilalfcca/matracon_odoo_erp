import logging
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    # ── Fix: Make vendor NOT required (Site Store raises PR without vendor) ──
    # Domain restricts the vendor dropdown to contacts tagged as "Vendor" in the Contacts app.
    partner_id = fields.Many2one(
        'res.partner',
        string='Vendor',
        required=False,
        tracking=True,
        domain="[('category_id.name', '=', 'Vendor')]",
    )

    # ── Custom PR State ──────────────────────────────────────────────────────
    x_pr_state = fields.Selection([
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),
        ('ceo_final', 'Pending CEO Approval'),
        ('po_locked', 'PO Locked'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
    ], string='PR Status', default='draft', tracking=True)

    # ── HO & CEO Approval Status Badges ─────────────────────────────────────
    x_ho_status = fields.Selection([
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ], string='HO Status', default='pending', tracking=True)

    x_ceo_status = fields.Selection([
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ], string='CEO Status', default='pending', tracking=True)

    # ── Procurement Type ─────────────────────────────────────────────────────
    x_procurement_type = fields.Selection([
        ('ho_procurement', 'HO Procurement'),
        ('site_procurement', 'Site Procurement'),
    ], string='Procurement Type', default='ho_procurement',
       tracking=True, required=True,
       help='HO Procurement: full approval chain (Site Store → HO → CEO → PO).\n'
            'Site Procurement: PM-approved, skip HO/CEO — receipt triggers petty cash expense.')

    # ── Vendor (info only — Site Procurement, legacy Char kept for migration) ──
    x_site_vendor_name = fields.Char(
        string='Vendor (Info)',
        help='Legacy text field — replaced by x_site_vendor_id.',
    )

    # ── Local Supplier (Site Procurement) ────────────────────────────────────
    x_site_vendor_id = fields.Many2one(
        'res.partner',
        string='Local Supplier',
        domain="[('category_id.name', '=', 'Local Supplier')]",
        tracking=True,
        help='Local supplier for Site Procurement. Must be tagged "Local Supplier" in Contacts.',
    )

    # ── Add to Liability Sheet (Site Procurement) ─────────────────────────────
    x_add_to_liability_sheet = fields.Boolean(
        string='Add to Liability Sheet?',
        default=False,
        tracking=True,
        help='If enabled, validating the GRN will create a draft Vendor Bill (liability) '
             'instead of a Petty Cash Expense.',
    )

    # ── Category ─────────────────────────────────────────────────────────────
    x_category_id = fields.Many2one(
        'product.category', string='Select Category', tracking=True,
        help='All products on lines must belong to this category. '
             'Not applicable for Site Procurement.'
    )

    # ── PM Signed PR Attachment ───────────────────────────────────────────────
    x_pm_signed_pr = fields.Binary(string='PM Signed PR', attachment=True)
    x_pm_signed_pr_filename = fields.Char(string='PM Signed PR Filename')

    # ── Project Site Config (PO selects when raising a PR directly) ──────────
    x_project_site_config_id = fields.Many2one(
        'x.project.site.config',
        string='Site Project',
        tracking=True,
        help='Select the project for which this procurement is being raised. '
             'Auto-fills the analytic account and delivery warehouse.',
    )

    # ── Project Analytic Account ──────────────────────────────────────────────
    x_project_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Project Analytic Account', tracking=True,
    )

    # ── Initiator (actual user who raised PR — fixes the list-view bug) ───────
    x_initiator_id = fields.Many2one(
        'res.users', string='Raised By',
        default=lambda self: self.env.user,
        readonly=True, copy=False, tracking=True,
    )

    # ── Comparative Statement Link ────────────────────────────────────────────
    x_comparative_statement_id = fields.Many2one(
        'x.comparative.statement', string='Comparative Statement', copy=False,
    )

    # ── Rejection Reason ──────────────────────────────────────────────────────
    x_rejection_reason = fields.Text(string='Rejection Reason', copy=False)

    # ── First product (for list views / dashboard) ───────────────────────────────
    x_first_product_name = fields.Char(
        string='Product',
        compute='_compute_first_product_name',
        help='Name of the first order line product. Used in list and dashboard views.',
    )

    @api.depends('order_line', 'order_line.product_id')
    def _compute_first_product_name(self):
        for order in self:
            first_line = order.order_line[:1]
            order.x_first_product_name = (
                first_line.product_id.display_name if first_line and first_line.product_id else ''
            )

    # ── PR Document flag ─────────────────────────────────────────────────────────
    # Computed (stored) so it recomputes for ALL existing records on module update.
    # Logic: any record that has a category set, has a PM doc, or has progressed
    # beyond draft is a real PR document. Alternative RFQs never have a category
    # (HO creates them without selecting one), so they correctly get False.
    x_is_pr_document = fields.Boolean(
        string='Is PR Document',
        compute='_compute_is_pr_document',
        store=True, copy=False,
        help='True for Purchase Requisitions raised through our PR workflow. '
             'False for Odoo native alternative RFQs.',
    )

    @api.depends('x_pr_state', 'x_pm_signed_pr', 'x_category_id', 'x_is_alternative_rfq', 'x_pr_origin', 'x_procurement_type')
    def _compute_is_pr_document(self):
        for order in self:
            if order.x_is_alternative_rfq:
                order.x_is_pr_document = False
                continue
            order.x_is_pr_document = bool(
                order.x_pr_state != 'draft'                        # Already progressed in workflow
                or order.x_pm_signed_pr                             # PM doc attached → it's a PR
                or order.x_category_id                              # Category selected → our PR form
                or order.x_pr_origin == 'procurement_ho'            # PO-raised PR (always PR workflow)
                or order.x_procurement_type == 'site_procurement'   # Site Procurement type selected
            )

    # ── Role flags (used in view expressions — Odoo 19 forbids groups() in attrs) ──
    x_is_site_store = fields.Boolean(compute='_compute_role_flags')
    x_is_ho = fields.Boolean(compute='_compute_role_flags')
    x_is_ceo = fields.Boolean(compute='_compute_role_flags')

    # ── Site warehouse restriction for "Deliver To" (picking_type_id) ────────
    # Incoming picking types for every site project the current user belongs to.
    # Used to restrict the Deliver To dropdown to site-store-specific warehouses.
    x_site_incoming_type_ids = fields.Many2many(
        'stock.picking.type',
        compute='_compute_site_warehouse_fields',
        string='Site Incoming Types',
    )
    # True when the user belongs to more than one site — unlocks the dropdown.
    x_site_has_multi_warehouse = fields.Boolean(
        compute='_compute_site_warehouse_fields',
    )

    def _compute_site_warehouse_fields(self):
        """Resolve incoming picking types for the current user's site(s)."""
        user = self.env.user
        if user.has_group('purchase_demand_raise.group_site_store'):
            configs = self.env['x.project.site.config'].sudo().search([
                ('site_user_ids', 'in', user.id),
            ])
            pt_ids = configs.mapped('warehouse_id.in_type_id').filtered(bool).ids
            has_multi = len(pt_ids) > 1
        else:
            pt_ids = []
            has_multi = False
        for order in self:
            order.x_site_incoming_type_ids = [(6, 0, pt_ids)]
            order.x_site_has_multi_warehouse = has_multi

    def _compute_role_flags(self):
        is_ss = self.env.user.has_group('purchase_demand_raise.group_site_store')
        is_ho = self.env.user.has_group('purchase_demand_raise.group_procurement_ho')
        is_ceo = self.env.user.has_group('purchase_demand_raise.group_ceo_approval')
        for order in self:
            order.x_is_site_store = is_ss
            order.x_is_ho = is_ho
            order.x_is_ceo = is_ceo

    x_ceo_bypass_ho = fields.Boolean(
        string='CEO Bypassed HO',
        default=False, copy=False, readonly=True,
        help='Set when CEO approves directly from Submitted without HO review.',
    )

    x_pr_origin = fields.Selection([
        ('site_store', 'Site Store'),
        ('procurement_ho', 'Procurement Officer'),
    ], string='PR Origin', default='site_store', tracking=True)

    # ── T&C Template ──────────────────────────────────────────────────────────
    x_terms_template_id = fields.Many2one(
        'x.po.terms.template',
        string='T&C Template',
        domain="[('active', '=', True)]",
        tracking=True,
        copy=False,
        help='Select a Terms & Conditions template to auto-fill the '
             'Terms & Conditions field below. The populated text remains '
             'fully editable per order after selection.',
    )

    @api.onchange('x_terms_template_id')
    def _onchange_terms_template_id(self):
        """Copy template content into the order's Terms & Conditions field."""
        if self.x_terms_template_id:
            self.note = self.x_terms_template_id.content

    # ── Computed helper: can Submit button be enabled? ────────────────────────
    x_can_submit = fields.Boolean(compute='_compute_can_submit')

    @api.depends('x_pm_signed_pr', 'x_pr_state', 'order_line', 'x_pr_origin')
    def _compute_can_submit(self):
        is_ho = self.env.user.has_group('purchase_demand_raise.group_procurement_ho')
        for order in self:
            if order.x_pr_state != 'draft' or not order.order_line:
                order.x_can_submit = False
                continue
            if order.x_pr_origin == 'procurement_ho' or is_ho:
                order.x_can_submit = True
            else:
                order.x_can_submit = bool(order.x_pm_signed_pr)

    # ── Override create: set initiator + PR flag + analytic for site users ──────
    @api.model
    def _matracon_pr_receipt_picking_type(self, user=None):
        """Deliver To on PRs — always the site's configured warehouse, not main WH."""
        user = user or self.env.user
        config = user.sudo().x_site_config_id
        warehouse = config.warehouse_id if config else user.x_default_warehouse_id
        if warehouse and warehouse.in_type_id:
            return warehouse.in_type_id
        return self.env['stock.picking.type']

    @api.model_create_multi
    def create(self, vals_list):
        user = self.env.user
        is_site_store = user.has_group('purchase_demand_raise.group_site_store')
        is_ho = user.has_group('purchase_demand_raise.group_procurement_ho')
        SiteConfig = self.env['x.project.site.config']
        for vals in vals_list:
            if 'x_initiator_id' not in vals:
                vals['x_initiator_id'] = user.id
            if is_ho and not is_site_store:
                vals.setdefault('x_pr_origin', 'procurement_ho')
            elif is_site_store:
                vals.setdefault('x_pr_origin', 'site_store')
            if is_site_store or vals.get('x_pr_origin') == 'site_store':
                if 'x_project_analytic_account_id' not in vals:
                    analytic = user.x_default_analytic_account_id
                    if analytic:
                        vals['x_project_analytic_account_id'] = analytic.id
                if not vals.get('picking_type_id'):
                    pt = self._matracon_pr_receipt_picking_type()
                    if pt:
                        vals['picking_type_id'] = pt.id
                # Auto-set site config from user if not already in vals
                if not vals.get('x_project_site_config_id'):
                    user_config = user.sudo().x_site_config_id
                    if user_config:
                        vals['x_project_site_config_id'] = user_config.id

            # ── Assign site-specific PR sequence ─────────────────────────────
            # Replaces the generic P00001 Odoo default with P-MCH-0001, P-HO-0001, etc.
            if vals.get('name', _('New')) in (_('New'), 'New', False, None, '/'):
                site_config_id = vals.get('x_project_site_config_id')
                if site_config_id:
                    config = SiteConfig.browse(site_config_id)
                    vals['name'] = config._matracon_next_pr_name()
                else:
                    vals['name'] = (
                        self.env['ir.sequence'].next_by_code('purchase.order.ho')
                        or _('New')
                    )
        return super().create(vals_list)

    @api.model
    def _matracon_migrate_pr_names(self):
        """One-time migration: rename all existing PRs to P-SITE-XXXX format.

        Groups PRs by site (detected via x_project_site_config_id or, as
        fallback, x_project_analytic_account_id) and by HO Procurement.
        Assigns sequential names within each group and updates any
        stock.picking.origin that referenced the old name.

        Call once from a shell/migrate script after upgrading the module.
        Idempotent: already-renamed records (name starts with 'P-') are skipped.
        """
        SiteConfig = self.env['x.project.site.config'].sudo()
        Picking = self.env['stock.picking'].sudo()
        IrSeq = self.env['ir.sequence'].sudo()

        all_orders = self.sudo().search([], order='id asc')
        # Track highest used counter per group so sequences continue correctly
        site_counters = {}  # site_config.id → next counter
        ho_counter = 1

        for po in all_orders:
            # Skip already-renamed records
            if po.name and po.name.startswith('P-'):
                continue

            old_name = po.name

            # Detect site config: explicit field, then fall back to analytic account
            config = po.x_project_site_config_id
            if not config and po.x_project_analytic_account_id:
                config = SiteConfig.search([
                    ('analytic_account_id', '=', po.x_project_analytic_account_id.id),
                ], limit=1)

            # Use site sequence only for site_procurement; HO procurement → P-HO-
            if config and po.x_procurement_type == 'site_procurement':
                cid = config.id
                wh_code = (
                    config.warehouse_id.code.strip().upper()
                    if config.warehouse_id else 'SITE'
                )
                counter = site_counters.get(cid, 1)
                new_name = 'P-%s-%04d' % (wh_code, counter)
                site_counters[cid] = counter + 1
                # Bump the ir.sequence so future PRs continue from the right number
                seq_code = config._matracon_pr_sequence_code()
                seq = IrSeq.search([('code', '=', seq_code)], limit=1)
                if seq and seq.number_next <= counter:
                    seq.write({'number_next': counter + 1})
            else:
                new_name = 'P-HO-%04d' % ho_counter
                ho_counter += 1
                # Bump HO sequence
                ho_seq = IrSeq.search([('code', '=', 'purchase.order.ho')], limit=1)
                if ho_seq and ho_seq.number_next < ho_counter:
                    ho_seq.write({'number_next': ho_counter})

            # Write new name on the PO
            po.sudo().write({'name': new_name})
            _logger.info('Renamed PR %s → %s', old_name, new_name)

            # Update any stock.picking.origin that references the old PO name
            pickings = Picking.search([('origin', '=', old_name)])
            if pickings:
                pickings.write({'origin': new_name})
                _logger.info(
                    '  Updated %d picking(s) origin: %s → %s',
                    len(pickings), old_name, new_name,
                )

    def copy(self, default=None):
        """Duplicate resets the full PR workflow — only products are carried over.
        Category, project, and analytic account are kept as useful defaults.
        """
        default = dict(default or {})
        default.update({
            # Reset all workflow state
            'x_pr_state': 'draft',
            'x_ho_status': 'pending',
            'x_ceo_status': 'pending',
            'x_ceo_bypass_ho': False,
            # Clear PM-signed document
            'x_pm_signed_pr': False,
            'x_pm_signed_pr_filename': False,
            # Clear vendor — selected by HO during approval
            'partner_id': False,
        })
        return super().copy(default)

    # ── Default picking_type_id and analytic account for site users ──────────
    @api.model
    def default_get(self, fields_list):
        defaults = super().default_get(fields_list)
        user = self.env.user
        is_ho = user.has_group('purchase_demand_raise.group_procurement_ho')
        is_site_store = user.has_group('purchase_demand_raise.group_site_store')
        # PO creating a new RFQ → mark as procurement_ho PR so PR workflow shows immediately
        if is_ho and not is_site_store:
            defaults['x_pr_origin'] = 'procurement_ho'
        if is_site_store:
            if 'picking_type_id' in fields_list:
                pt = self._matracon_pr_receipt_picking_type(user)
                if pt:
                    defaults['picking_type_id'] = pt.id
            # Auto-populate analytic account so it is visible in the form
            # BEFORE lines are added — enables _onchange_product_id_analytic on lines.
            if 'x_project_analytic_account_id' in fields_list:
                analytic = user.sudo().x_default_analytic_account_id
                if analytic:
                    defaults['x_project_analytic_account_id'] = analytic.id
        return defaults

    # ── When PO selects a project → auto-fill analytic + warehouse ───────────
    @api.onchange('x_project_site_config_id')
    def _onchange_project_site_config_id(self):
        config = self.x_project_site_config_id
        if config:
            if config.analytic_account_id:
                self.x_project_analytic_account_id = config.analytic_account_id
            if config.warehouse_id and config.warehouse_id.in_type_id:
                self.picking_type_id = config.warehouse_id.in_type_id
        else:
            self.x_project_analytic_account_id = False

    # ── When analytic account changes on PO → push to all lines ──────────────
    @api.onchange('x_project_analytic_account_id')
    def _onchange_project_analytic_account(self):
        if self.x_project_analytic_account_id:
            acc_id = str(self.x_project_analytic_account_id.id)
            for line in self.order_line:
                line.analytic_distribution = {acc_id: 100.0}
            config = self.env['x.project.site.config'].sudo().search([
                ('analytic_account_id', '=', self.x_project_analytic_account_id.id),
            ], limit=1)
            if config and config.warehouse_id and config.warehouse_id.in_type_id:
                self.picking_type_id = config.warehouse_id.in_type_id

    # ── Category change warning ───────────────────────────────────────────────
    @api.onchange('x_category_id')
    def _onchange_category_id(self):
        if self.x_category_id and self.order_line:
            mismatched = self.order_line.filtered(
                lambda l: l.product_id and l.product_id.categ_id != self.x_category_id
            )
            if mismatched:
                return {
                    'warning': {
                        'title': _('Category Mismatch'),
                        'message': _(
                            'Some existing lines belong to a different category. '
                            'New products added will be restricted to "%s" only. '
                            'Existing lines are not removed.'
                        ) % self.x_category_id.name,
                    }
                }

    # ════════════════════════════════════════════════════════
    #  NOTIFICATION HELPERS
    # ════════════════════════════════════════════════════════

    def _get_group_partners(self, xml_id):
        """Return partner IDs of all users in a given group."""
        group = self.env.ref(xml_id, raise_if_not_found=False)
        if group:
            return group.user_ids.mapped('partner_id').ids  # Odoo 19: user_ids not users
        return []

    def _get_group_users(self, xml_id):
        """Return user recordset for all users in a given group."""
        group = self.env.ref(xml_id, raise_if_not_found=False)
        return group.user_ids if group else self.env['res.users']

    def _notify_partners(self, partner_ids, body):
        """Post an HTML message that notifies specific partners."""
        if partner_ids:
            self.message_post(
                body=body,
                partner_ids=partner_ids,
                subtype_xmlid='mail.mt_comment',
                message_type='comment',
            )

    def _ensure_followers(self):
        """Make sure HO, CEO, and Initiator are all followers on this PR."""
        partner_ids = (
            self._get_group_partners('purchase_demand_raise.group_procurement_ho')
            + self._get_group_partners('purchase_demand_raise.group_ceo_approval')
        )
        if self.x_initiator_id and self.x_initiator_id.partner_id:
            partner_ids.append(self.x_initiator_id.partner_id.id)
        if not partner_ids:
            return
        partner_ids = list(set(partner_ids))
        # Use a savepoint so that a concurrent-duplicate DB error (unique constraint on
        # mail_followers) only rolls back this subscribe attempt rather than the whole
        # transaction.  This race condition is common on multi-worker staging/production
        # (two workers both read "no followers yet", both try to INSERT the same partner).
        # On failure we fall back to one-by-one subscribes so new partners still get through.
        try:
            with self.env.cr.savepoint():
                self.message_subscribe(partner_ids=partner_ids)
        except Exception:
            for pid in partner_ids:
                try:
                    with self.env.cr.savepoint():
                        self.message_subscribe(partner_ids=[pid])
                except Exception:
                    pass

    def _schedule_approval_activities(self):
        """Create To-Do activities for every HO user when a PR is submitted.
        CEO activities are scheduled separately when HO recommends the vendor."""
        ho_users = self._get_group_users('purchase_demand_raise.group_procurement_ho')
        initiator_name = self.x_initiator_id.name or self.env.user.name
        for user in ho_users:
            self.activity_schedule(
                'mail.mail_activity_data_todo',
                summary=_('PR Approval Required: %s') % self.name,
                note=_('PR raised by %s awaits your review. Please set the recommended vendor, then approve to route to CEO.') % initiator_name,
                user_id=user.id,
            )

    # ════════════════════════════════════════════════════════
    #  WORKFLOW ACTIONS
    # ════════════════════════════════════════════════════════

    def _get_project_site_store_partners(self):
        """Return partner IDs of site store users assigned to the order's project."""
        self.ensure_one()
        config = self.x_project_site_config_id
        if not config:
            return []
        return config.site_user_ids.mapped('partner_id').ids

    def _action_submit_site_procurement(self):
        """Site Procurement: PM-approved → skip HO/CEO, lock PO, create receipt picking."""
        self.ensure_one()
        self.write({
            'x_pr_state': 'po_locked',
            'x_ho_status': 'approved',   # auto-approved (no HO review required)
            'x_ceo_status': 'approved',  # auto-approved (no CEO review required)
        })
        self._ensure_followers()
        # Confirm PO directly (no vendor required for site procurement)
        self.sudo().write({
            'state': 'purchase',
            'date_approve': fields.Datetime.now(),
            'date_order': self.date_order or fields.Datetime.now(),
        })
        self._matracon_ensure_receipt_pickings()
        self.message_post(
            body=Markup(
                '📦 <b>Site Procurement PR submitted</b> by <b>%(user)s</b>.<br/>'
                'PM-signed approval received — no HO/CEO approval required.<br/>'
                'PO confirmed. Material receipt is ready for Site Store.'
            ) % {'user': self.env.user.name},
            subtype_xmlid='mail.mt_log_note',
        )
        site_store_partners = self._get_project_site_store_partners()
        if site_store_partners:
            self._notify_partners(
                site_store_partners,
                Markup(
                    '📦 <b>Site Procurement PR Ready for Receipt: %(pr)s</b><br/>'
                    'PM-signed approval received. '
                    'You can now proceed with receiving the material.'
                ) % {'pr': self.name}
            )

    def action_submit_pr(self):
        """Submit PR — Site Store (with PM doc) or Procurement Officer (direct to CEO)."""
        for order in self:
            is_ho = self.env.user.has_group('purchase_demand_raise.group_procurement_ho')
            if not is_ho and not order.x_pm_signed_pr:
                raise UserError(_('Please attach the PM Signed PR before submitting.'))
            if not order.order_line:
                raise UserError(_('Please add at least one product line before submitting.'))

            # ── Site Procurement: skip HO/CEO, go directly to po_locked ─────
            if order.x_procurement_type == 'site_procurement':
                order._action_submit_site_procurement()
                continue

            ho_raised = is_ho and order.x_pr_origin != 'site_store'
            if ho_raised:
                order.x_pr_origin = 'procurement_ho'
                if not order.x_project_site_config_id:
                    raise UserError(_('Please select the Project before submitting.'))
                # Vendor must be selected before submitting to CEO
                if not order.partner_id:
                    raise UserError(_(
                        'Please select a Vendor before submitting to CEO.\n'
                        'Go to the Vendor field (top of the form) and pick the supplier.'
                    ))
                # Recommended Qty is mandatory on HO-raised PRs
                missing = [
                    l.product_id.display_name or l.name or '#%d' % l.id
                    for l in order.order_line
                    if not (l.x_recommended_qty or 0.0)
                ]
                if missing:
                    raise UserError(_(
                        'Recommended Quantity is required for all lines before submitting to CEO.\n'
                        'Missing on:\n• %s'
                    ) % '\n• '.join(missing))

            if ho_raised:
                # ── PO-raised PR: skip HO review → route directly to CEO ──────
                order.write({
                    'x_pr_state': 'ceo_final',
                    'x_ho_status': 'approved',  # Auto-approved (PO is HO)
                    'x_ceo_status': 'pending',
                })
                order._ensure_followers()

                order.message_post(
                    body=Markup(
                        '📋 PR <b>%(pr)s</b> submitted by Procurement Officer <b>%(user)s</b> '
                        'for project <b>%(project)s</b>. Routed directly to CEO for final approval.'
                    ) % {
                        'pr': order.name,
                        'user': self.env.user.name,
                        'project': order.x_project_site_config_id.name,
                    },
                    subtype_xmlid='mail.mt_log_note',
                )

                # Schedule CEO activities
                ceo_users = order._get_group_users('purchase_demand_raise.group_ceo_approval')
                for user in ceo_users:
                    order.activity_schedule(
                        'mail.mail_activity_data_todo',
                        summary=_('CEO Approval Required: %s') % order.name,
                        note=_('Procurement Officer %(po)s has raised PR %(pr)s for project %(project)s. Please review and give final approval.') % {
                            'po': self.env.user.name,
                            'pr': order.name,
                            'project': order.x_project_site_config_id.name,
                        },
                        user_id=user.id,
                    )

                # Notify CEO
                ceo_partners = order._get_group_partners('purchase_demand_raise.group_ceo_approval')
                order._notify_partners(
                    ceo_partners,
                    Markup(
                        '📋 <b>Action Required — CEO Approval: %(pr)s</b><br/>'
                        'Procurement Officer <b>%(po)s</b> has submitted a PR for project <b>%(project)s</b>.<br/>'
                        'Please review and give final approval.'
                    ) % {
                        'pr': order.name,
                        'po': self.env.user.name,
                        'project': order.x_project_site_config_id.name,
                    }
                )

                # Notify project's site store users
                site_store_partners = order._get_project_site_store_partners()
                if site_store_partners:
                    order._notify_partners(
                        site_store_partners,
                        Markup(
                            '📦 <b>Purchase Requisition Raised for Your Project</b><br/>'
                            'Procurement Officer <b>%(po)s</b> has submitted PR <b>%(pr)s</b> '
                            'for project <b>%(project)s</b>. It is now awaiting CEO approval.'
                        ) % {
                            'pr': order.name,
                            'po': self.env.user.name,
                            'project': order.x_project_site_config_id.name,
                        }
                    )

            else:
                # ── Site Store PR: standard flow → HO review ─────────────────
                order.write({
                    'x_pr_state': 'submitted',
                    'x_ho_status': 'pending',
                    'x_ceo_status': 'pending',
                })
                order._ensure_followers()
                order._schedule_approval_activities()

                # Auto-create a Comparative Statement so HO can start evaluating vendors immediately
                if not order.x_comparative_statement_id:
                    cs = self.env['x.comparative.statement'].sudo().create({
                        'x_purchase_order_id': order.id,
                        'name': _('CS - %s') % order.name,
                    })
                    order.x_comparative_statement_id = cs

                order.message_post(
                    body=Markup('PR <b>%s</b> submitted by <b>%s</b>. Awaiting Procurement HO review and vendor recommendation.') % (order.name, self.env.user.name),
                    subtype_xmlid='mail.mt_log_note',
                )

                # Notify Procurement HO
                ho_partners = order._get_group_partners('purchase_demand_raise.group_procurement_ho')
                order._notify_partners(
                    ho_partners,
                    Markup('📋 <b>Action Required — Purchase Requisition Submitted</b><br/>'
                           'PR <b>%(pr)s</b> has been raised by <b>%(user)s</b>. '
                           'Please open the <b>Comparative Statement</b> on this PR to add vendors, '
                           'enter quotation prices, and select the recommended vendor.') % {
                        'pr': order.name, 'user': self.env.user.name,
                    }
                )

    def action_ho_approve(self):
        """HO approves PR, sets vendor, pulls prices from pricelist → routes to CEO for final approval.

        This is HO's single action at 'submitted'. Vendor must be set first.
        Prices are auto-fetched from the vendor's pricelist so CEO sees full financials.
        """
        for order in self:
            if order.x_pr_state != 'submitted':
                raise UserError(_('PR must be in Submitted state for HO approval.'))
            order._validate_ho_qty_adjustment_reasons()
            if not order.partner_id:
                raise UserError(_(
                    'Please select the recommended Vendor before approving.\n'
                    'You can use the Odoo Alternatives feature to compare vendors, '
                    'then set the final vendor here.\n\n'
                    'The vendor and its prices will be shown to CEO for final quantity decisions.'
                ))

            # ── Auto-populate prices from vendor pricelist ──────────────────
            today = fields.Date.today()
            for line in order.order_line:
                if line.product_id:
                    seller = line.product_id._select_seller(
                        partner_id=order.partner_id,
                        quantity=line.x_recommended_qty or line.x_requested_qty or line.product_qty or 1.0,
                        date=order.date_order or today,
                        uom_id=line.product_uom_id,
                    )
                    if seller:
                        line.price_unit = seller.price
                    elif not line.price_unit:
                        # Fallback: product standard price
                        line.price_unit = line.product_id.standard_price

            order.write({
                'x_ho_status': 'approved',
                'x_pr_state': 'ceo_final',
            })

            # Close HO's activities
            order.activity_ids.filtered(
                lambda a: a.user_id == self.env.user
            ).action_feedback(
                feedback=_('Approved — Vendor: %s') % order.partner_id.name
            )

            order.message_post(
                body=Markup(
                    '✅ <b>HO approved</b> by <b>%(user)s</b>. '
                    'Recommended vendor: <b>%(vendor)s</b>. '
                    'Prices fetched from vendor pricelist. Submitted to CEO for final approval.'
                ) % {'user': self.env.user.name, 'vendor': order.partner_id.name},
                subtype_xmlid='mail.mt_log_note',
            )

            # Schedule CEO activities
            ceo_users = order._get_group_users('purchase_demand_raise.group_ceo_approval')
            for user in ceo_users:
                order.activity_schedule(
                    'mail.mail_activity_data_todo',
                    summary=_('Final Approval Required: %s') % order.name,
                    note=_('HO approved and recommended %(vendor)s. Please review quantities and give final CEO approval.') % {
                        'vendor': order.partner_id.name
                    },
                    user_id=user.id,
                )

            # Notify CEO
            ceo_partners = order._get_group_partners('purchase_demand_raise.group_ceo_approval')
            order._notify_partners(
                ceo_partners,
                Markup(
                    '📋 <b>Action Required — Final Approval: %(pr)s</b><br/>'
                    'Procurement HO has approved and recommended <b>%(vendor)s</b> as the vendor.<br/>'
                    'Please review quantities, set your decision per line, and give final CEO approval.'
                ) % {'pr': order.name, 'vendor': order.partner_id.name}
            )

    def action_ho_reject(self):
        """Procurement HO rejects the PR."""
        for order in self:
            order.write({'x_ho_status': 'rejected', 'x_pr_state': 'rejected'})
            # Mark HO's activities as done
            order.activity_ids.filtered(
                lambda a: a.user_id == self.env.user
            ).action_feedback(feedback=_('Rejected by %s') % self.env.user.name)
            order.message_post(
                body=Markup('❌ PR rejected by <b>Procurement HO</b> (%s).') % self.env.user.name,
                subtype_xmlid='mail.mt_log_note',
            )
            if order.x_initiator_id and order.x_initiator_id.partner_id:
                order._notify_partners(
                    [order.x_initiator_id.partner_id.id],
                    Markup('❌ Your PR <b>%(pr)s</b> was rejected by Procurement HO. Please review and re-submit if needed.') % {'pr': order.name}
                )

    def _matracon_fetch_vendor_prices(self):
        """Populate line prices from vendor pricelist or product cost."""
        self.ensure_one()
        today = fields.Date.today()
        for line in self.order_line:
            if not line.product_id:
                continue
            qty = (
                line.x_recommended_qty or line.x_requested_qty
                or line.product_qty or 1.0
            )
            if self.partner_id:
                seller = line.product_id._select_seller(
                    partner_id=self.partner_id,
                    quantity=qty,
                    date=self.date_order or today,
                    uom_id=line.product_uom_id,
                )
                if seller:
                    line.price_unit = seller.price
            if not line.price_unit:
                line.price_unit = line.product_id.standard_price

    def action_ceo_final_approve(self):
        """CEO gives final line-level approval — PO confirmed, locked, others cancelled."""
        for order in self:
            bypass_ho = order.x_pr_state == 'submitted'
            if order.x_pr_state not in ('ceo_final', 'submitted'):
                raise UserError(_(
                    'PR must be in Submitted or Pending CEO Approval state.'
                ))

            if bypass_ho:
                if not order.partner_id:
                    raise UserError(_(
                        'Select a vendor before CEO final approval when bypassing '
                        'Procurement Officer review.'
                    ))
                for line in order.order_line:
                    if not line.x_recommended_qty and line.x_requested_qty:
                        line.x_recommended_qty = line.x_requested_qty
                order._matracon_fetch_vendor_prices()
                order.x_ceo_bypass_ho = True

            if not order.x_is_alternative_rfq:
                for line in order.order_line:
                    base_qty = line._get_ceo_qty_base()
                    if line.x_approved_qty <= 0 and base_qty > 0:
                        line.x_approved_qty = base_qty
                    if line.x_approved_qty <= 0:
                        raise UserError(_(
                            'Please set Approved Qty > 0 for all lines before final approval.'
                        ))
                for line in order.order_line:
                    line.product_qty = line.x_approved_qty

            order.write({
                'x_pr_state': 'po_locked',
                'x_ceo_status': 'approved',
                'x_ho_status': order.x_ho_status if not bypass_ho else 'pending',
            })

            # ── Confirm the PO and ensure receipt picking is created ──────────
            order.button_confirm()
            if order.state == 'to approve' and hasattr(order, 'button_approve'):
                order.sudo().button_approve()
            elif order.state not in ('purchase', 'done', 'cancel'):
                order.sudo().write({
                    'state': 'purchase',
                    'date_approve': fields.Datetime.now(),
                })
            order._matracon_ensure_receipt_pickings()

            # ── Cancel all other open RFQs in the same alternatives group ───────
            if getattr(order, 'purchase_group_id', False) and order.purchase_group_id:
                others = order.purchase_group_id.order_ids.filtered(
                    lambda o: o.id != order.id and o.state not in ('purchase', 'done', 'cancel')
                )
                for other in others:
                    try:
                        other.with_context(cancel_procurement=True).button_cancel()
                        if other.x_pr_state != 'cancelled':
                            other.x_pr_state = 'cancelled'
                    except Exception:
                        pass  # Never block the main approval if a cancel fails

            order.message_post(
                body=Markup(
                    '🔒 <b>CEO Final Approval</b> granted by <b>%(user)s</b>.%(bypass)s '
                    'PO is now locked and confirmed — Purchase Order sent to vendor.'
                ) % {
                    'user': self.env.user.name,
                    'bypass': Markup('<br/><i>Procurement HO review was bypassed.</i>')
                    if bypass_ho else Markup(''),
                },
                subtype_xmlid='mail.mt_log_note',
            )

            ho_partners = order._get_group_partners('purchase_demand_raise.group_procurement_ho')
            order._notify_partners(
                ho_partners,
                Markup('🔒 PO <b>%(pr)s</b> — CEO has given final approval. '
                       'PO is confirmed and Purchase Order has been sent to vendor.') % {'pr': order.name}
            )
            if order.x_initiator_id and order.x_initiator_id.partner_id:
                order._notify_partners(
                    [order.x_initiator_id.partner_id.id],
                    Markup('✅ Your PR <b>%(pr)s</b> has been fully approved. '
                           'The Purchase Order is confirmed and materials will be ordered.') % {'pr': order.name}
                )

            # ── Auto-send PO confirmation email to vendor (once) ─────────────
            if order.partner_id and order.partner_id.email:
                try:
                    po_template = self.env.ref(
                        'purchase_demand_raise.matracon_po_confirmation_email_template',
                        raise_if_not_found=False,
                    ) or self.env.ref(
                        'purchase.email_template_edi_purchase_done',
                        raise_if_not_found=False,
                    )
                    if po_template:
                        po_template.with_context(
                            lang=order.partner_id.lang or 'en_US'
                        ).send_mail(order.id, force_send=True, raise_exception=False)
                        order.message_post(
                            body=Markup(
                                '📧 Purchase Order confirmation email automatically sent to '
                                '<b>%(vendor)s</b> (%(email)s).'
                            ) % {
                                'vendor': order.partner_id.name,
                                'email': order.partner_id.email,
                            },
                            subtype_xmlid='mail.mt_log_note',
                        )
                except Exception as exc:
                    _logger.warning(
                        'Failed to auto-send PO confirmation email for %s: %s',
                        order.name, exc,
                    )
                    order.message_post(
                        body=Markup(
                            '⚠️ <b>Purchase Order email could not be sent to the vendor.</b><br/>'
                            'The PO is confirmed and locked. Please send the PO PDF manually '
                            'using the <b>Send by Email</b> button or print it.<br/>'
                            '<small class="text-muted">Technical details have been recorded '
                            'in the server log.</small>'
                        ),
                        subtype_xmlid='mail.mt_log_note',
                    )

    def action_ceo_final_reject(self):
        """CEO rejects at final stage (or bypass review from submitted)."""
        for order in self:
            if order.x_pr_state not in ('ceo_final', 'submitted'):
                raise UserError(_('PR is not awaiting CEO decision.'))
            order.write({'x_ceo_status': 'rejected', 'x_pr_state': 'rejected'})
            order.message_post(
                body=Markup('❌ PO rejected by <b>CEO</b> at final stage (%s).') % self.env.user.name,
                subtype_xmlid='mail.mt_log_note',
            )
            ho_partners = order._get_group_partners('purchase_demand_raise.group_procurement_ho')
            order._notify_partners(
                ho_partners,
                Markup('❌ PO <b>%(pr)s</b> rejected by CEO at final stage. Please review and take corrective action.') % {'pr': order.name}
            )
            if order.x_initiator_id and order.x_initiator_id.partner_id:
                order._notify_partners(
                    [order.x_initiator_id.partner_id.id],
                    Markup('❌ PO for your PR <b>%(pr)s</b> was rejected by CEO at the final stage.') % {'pr': order.name}
                )

    def button_confirm(self):
        """Override: guard standard Confirm Order against bypassing the PR workflow.

        Only applies to real PR documents (x_is_pr_document=True).
        Odoo native alternative RFQs (x_is_pr_document=False) pass through freely —
        they are standard vendor quote RFQs managed by the Alternatives feature.

        For PR documents:
        - draft / submitted / rejected / cancelled → blocked
        - rfq_phase / ceo_final → allowed if vendor is set; auto-advances to po_locked
        - po_locked → pass-through (already CEO-approved)
        """
        _hard_block = ('draft', 'submitted', 'rejected', 'cancelled')
        _soft_check = ('ceo_final',)

        for order in self:
            if not order.x_is_pr_document:
                # Not a PR document (alternative RFQ, direct admin PO, etc.) — no restriction
                continue

            if order.x_pr_state in _hard_block:
                state_label = dict(self._fields['x_pr_state'].selection).get(
                    order.x_pr_state, order.x_pr_state
                )
                raise UserError(_(
                    'This PR has not completed the full approval process.\n'
                    'Current PR Status: %s\n\n'
                    'The PR must go through: Submit → HO+CEO Approval → Vendor Selection → CEO Final Approve.'
                ) % state_label)

            if order.x_pr_state in _soft_check:
                if not order.partner_id:
                    raise UserError(_('Please select a Vendor before confirming the order.'))
                # Advance state to po_locked (both approvals done, vendor chosen)
                order.x_pr_state = 'po_locked'

        res = super().button_confirm()
        self._matracon_ensure_receipt_pickings()
        return res

    def _matracon_ensure_receipt_pickings(self):
        """Create incoming receipt pickings when a PO is confirmed but none exist.

        CEO approval sometimes force-sets state to purchase without running
        button_approve(), which is where purchase_stock normally creates receipts.

        Uses direct DB searches (not self.picking_ids computed field) to avoid
        ORM-cache false-negatives on freshly-created pickings.
        """
        # Use sudo() so record rules on stock.picking don't hide existing
        # pickings — the operational warehouse override in
        # _matracon_sync_user_operational_warehouse() can temporarily set
        # x_default_warehouse_id to the main company warehouse, which causes
        # the site-store record rule to hide the receipt picking.
        Picking = self.env['stock.picking'].sudo()

        def _has_open_incoming(order):
            """Check for open incoming pickings via direct SQL — bypasses cache."""
            by_moves = Picking.search_count([
                ('move_ids.purchase_line_id.order_id', '=', order.id),
                ('picking_type_id.code', '=', 'incoming'),
                ('state', 'not in', ('done', 'cancel')),
            ], limit=1)
            if by_moves:
                return True
            # Also check by origin (covers fallback pickings without stock moves)
            return bool(Picking.search_count([
                ('origin', '=', order.name),
                ('picking_type_id.code', '=', 'incoming'),
                ('state', 'not in', ('done', 'cancel')),
            ], limit=1))

        for order in self:
            if order.state not in ('purchase', 'done', 'to approve'):
                continue

            # Direct DB search — avoids the stored-computed picking_ids field
            # whose recomputation may be deferred in the same transaction.
            if _has_open_incoming(order):
                continue

            if order.state == 'to approve' and hasattr(order, 'button_approve'):
                order.sudo().button_approve()
                continue

            if hasattr(order, '_create_picking'):
                order.sudo()._create_picking()

            # Verify the picking was actually created.  _create_picking silently
            # skips when all products are service-type.  As a fallback, create a
            # minimal receipt picking manually so the site store can still open
            # the receipt form and register the delivery.
            if not _has_open_incoming(order) and hasattr(order, '_prepare_picking'):
                try:
                    vals = order.sudo()._prepare_picking()
                    Picking.sudo().create(vals)
                except Exception:
                    pass  # Cannot create picking — receipt form unavailable

        self.invalidate_recordset(['picking_ids', 'incoming_picking_count'])

    def action_view_picking(self):
        """Open the incoming receipt form (Site Store) — not a list view.

        Uses a direct DB search instead of the computed self.picking_ids to
        avoid ORM-cache stale reads after picking creation in the same request.
        """
        self.ensure_one()
        self._matracon_ensure_receipt_pickings()
        # Use sudo() so record rules on stock.picking don't hide the receipt.
        # The site-store record rule uses x_default_warehouse_id which can be
        # temporarily set to 'My Company' when the site warehouse has no stock.
        # sudo() bypasses that; the form view itself enforces its own access.
        Picking = self.env['stock.picking'].sudo()
        # Primary: search via stock moves (the normal picking path)
        picking = Picking.search([
            ('move_ids.purchase_line_id.order_id', '=', self.id),
            ('picking_type_id.code', '=', 'incoming'),
            ('state', 'not in', ('done', 'cancel')),
        ], limit=1, order='id asc')
        if not picking:
            # Secondary: search by origin name — covers fallback pickings without moves
            picking = Picking.search([
                ('origin', '=', self.name),
                ('picking_type_id.code', '=', 'incoming'),
                ('state', 'not in', ('done', 'cancel')),
            ], limit=1, order='id asc')
        if not picking:
            # Also check done receipts (already-received POs — open for review)
            picking = Picking.search([
                ('move_ids.purchase_line_id.order_id', '=', self.id),
                ('picking_type_id.code', '=', 'incoming'),
            ], limit=1, order='state asc, id asc')
        if not picking:
            picking = Picking.search([
                ('origin', '=', self.name),
                ('picking_type_id.code', '=', 'incoming'),
            ], limit=1, order='state asc, id asc')
        if picking:
            # Plain act_window dict — no path key — keeps URL nested in the PO:
            #   /odoo/purchase/{po_id}/stock.picking/{picking_id}
            # exactly as in the staging environment.
            return {
                'type': 'ir.actions.act_window',
                'name': _('Receipt'),
                'res_model': 'stock.picking',
                'view_mode': 'form',
                'res_id': picking.id,
                'target': 'current',
                'context': dict(self.env.context),
            }
        return super().action_view_picking()

    def action_rfq_send(self):
        """Override Send RFQ to enforce Matracon gates and inject custom template.

        1. Block Site Store users from sending RFQs directly.
        2. Enforce PR state gate — RFQ can only be sent after Submitted state.
        3. Swap the default_template_id to our standalone matracon_rfq_email_template
           so the compose dialog uses the correct formal letter body AND attaches
           our custom RFQ PDF (blank Unit Price / Tax % / Total columns).

        Why not rely on the data/mail_templates.xml override?
        The base purchase template is wrapped in <data noupdate="1">, which
        prevents report_template_ids from being reliably updated on upgrades.
        Injecting the template ID here bypasses that limitation entirely.
        """
        # ── Site Store gate ───────────────────────────────────────────────────
        if self.env.user.has_group('purchase_demand_raise.group_site_store'):
            raise UserError(_('Site Store cannot send RFQs. Please submit the PR for approval.'))

        # ── Vendor gate — HO-raised PRs must have a vendor selected ──────────
        for order in self:
            if order.x_pr_origin == 'procurement_ho' and not order.partner_id:
                raise UserError(_(
                    'Please select a Vendor before sending the RFQ.\n'
                    'Go to the Vendor field (top of the form) and pick the supplier.'
                ))

        # ── PR state gate (only for site-store-originated PRs) ───────────────
        # HO-raised PRs (procurement_ho) can email the vendor at any stage (draft → sent)
        for order in self:
            if (order.x_is_pr_document
                    and order.x_pr_origin != 'procurement_ho'
                    and order.x_pr_state not in ('submitted', 'ceo_final', 'po_locked')):
                raise UserError(_(
                    'RFQ cannot be sent at this stage.\n'
                    'Current PR Status: %s'
                ) % dict(self._fields['x_pr_state'].selection).get(
                    order.x_pr_state, order.x_pr_state
                ))

        # ── Open the mail compose wizard ──────────────────────────────────────
        result = super().action_rfq_send()

        # ── Swap to the correct Matracon template based on PO state ──────────
        # Locked POs (state='purchase') → PO Confirmation template
        # RFQs / draft / sent         → RFQ template (formal invite letter)
        # The standalone templates have their own IDs so noupdate never blocks.
        if self.state == 'purchase':
            our_template = self.env.ref(
                'purchase_demand_raise.matracon_po_confirmation_email_template',
                raise_if_not_found=False,
            )
        else:
            our_template = self.env.ref(
                'purchase_demand_raise.matracon_rfq_email_template',
                raise_if_not_found=False,
            )
        if our_template:
            ctx = result.get('context')
            if isinstance(ctx, dict):
                ctx['default_template_id'] = our_template.id
        return result

    def button_cancel(self):
        """Override: block cancellation of rejected PR documents (already in terminal state)."""
        for order in self:
            if order.x_is_pr_document and order.x_pr_state == 'rejected':
                raise UserError(_('This PR has already been rejected. Cancellation is not allowed on a rejected PR.'))
        return super().button_cancel()

    def action_reset_to_draft(self):
        """Reset PR back to Draft.

        Rules:
        • CEO / Matracon Admin  — can reset from any state except po_locked
          with confirmed receipts (stock moves already dispatched).
        • Site Store / Site Accountant — can reset only when HO has NOT yet
          acted (x_ho_status == 'pending') and state is submitted/cancelled/rejected.
        • HO users — blocked; they should reject/re-route instead.
        """
        is_ceo   = self.env.user.has_group('purchase_demand_raise.group_ceo_approval')
        is_admin = self.env.user.has_group('purchase_demand_raise.group_matracon_admin')
        is_ho    = self.env.user.has_group('purchase_demand_raise.group_procurement_ho')
        is_privileged = is_ceo or is_admin

        for order in self:
            if not order.x_is_pr_document:
                # Non-PR purchase orders: just use Odoo's standard reset
                if order.state == 'cancel':
                    super(type(self), order).button_draft()
                continue

            if order.x_pr_state == 'po_locked' and order.incoming_picking_count:
                raise UserError(_(
                    'PO %(name)s is locked with %(n)s receipt(s) already created. '
                    'Reverse the receipts before resetting to draft.',
                    name=order.name, n=order.incoming_picking_count,
                ))

            if not is_privileged:
                # Site Store / other non-HO non-privileged users
                if is_ho:
                    raise UserError(_(
                        'HO users cannot reset a PR to draft. '
                        'Use Reject to send it back to the site.'
                    ))
                if order.x_ho_status != 'pending':
                    raise UserError(_(
                        'Head Office has already reviewed %(name)s. '
                        'You can no longer reset it to draft. '
                        'Please contact HO Procurement.',
                        name=order.name,
                    ))
                if order.x_pr_state not in ('submitted', 'cancelled', 'rejected'):
                    raise UserError(_(
                        'PR %(name)s cannot be reset to draft at this stage (%(state)s).',
                        name=order.name, state=order.x_pr_state,
                    ))

            # Reset Odoo's purchase state from cancel back to draft
            if order.state == 'cancel':
                super(type(self), order).button_draft()
            elif order.state == 'purchase':
                # PO was confirmed; cancel it first so we can reset
                super(type(self), order).button_cancel()
                super(type(self), order).button_draft()

            order.write({
                'x_pr_state':  'draft',
                'x_ho_status': 'pending',
                'x_ceo_status': 'pending',
            })
            order.message_post(
                body=Markup(
                    '↩️ PR reset to <b>Draft</b> by <b>%(user)s</b>.'
                ) % {'user': self.env.user.name},
                subtype_xmlid='mail.mt_log_note',
            )

    # ── CS helpers ───────────────────────────────────────────────────────────

    def _validate_ho_qty_adjustment_reasons(self):
        """Every line where HO changed recommended qty must have a logged reason."""
        for order in self:
            for line in order.order_line.filtered(lambda l: l.product_id and not l.display_type):
                if not line._ho_qty_differs_from_requested():
                    continue
                if not (line.x_qty_adjustment_reason or '').strip():
                    raise UserError(_(
                        'Line "%(product)s": recommended qty (%(rec).2f) differs from '
                        'requested (%(req).2f). Enter an adjustment reason on the line '
                        'before approving.',
                        product=line.product_id.display_name,
                        rec=line.x_recommended_qty,
                        req=line.x_requested_qty,
                    ))

    def _post_ho_qty_adjustment_log(self, line, old_recommended):
        """Internal chatter log when HO adjusts recommended quantity."""
        self.ensure_one()
        self.message_post(
            body=Markup(
                '📝 <b>Qty adjustment</b> by <b>%(user)s</b><br/>'
                'Product: <b>%(product)s</b><br/>'
                'Requested: %(req).2f → Recommended: %(rec).2f '
                '(was %(old).2f)<br/>'
                'Reason: %(reason)s'
            ) % {
                'user': self.env.user.name,
                'product': line.product_id.display_name,
                'req': line.x_requested_qty,
                'rec': line.x_recommended_qty,
                'old': old_recommended,
                'reason': line.x_qty_adjustment_reason,
            },
            subtype_xmlid='mail.mt_log_note',
        )

    def action_create_comparative_statement(self):
        self.ensure_one()
        cs = self.env['x.comparative.statement'].create({
            'x_purchase_order_id': self.id,
            'name': _('CS - %s') % self.name,
        })
        self.x_comparative_statement_id = cs
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'x.comparative.statement',
            'res_id': cs.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_open_comparative_statement(self):
        self.ensure_one()
        if not self.x_comparative_statement_id:
            return self.action_create_comparative_statement()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'x.comparative.statement',
            'res_id': self.x_comparative_statement_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_confirm_cs(self, cs):
        """Called from CS.action_confirm() — set vendor+prices on PR and route to CEO.

        This replaces the manual 'Approve & Submit to CEO' flow when a Comparative
        Statement is used: the CS sets the vendor and transfers prices, then advances
        the PR to the 'ceo_final' state.
        """
        self.ensure_one()
        if self.x_pr_state != 'submitted':
            raise UserError(_(
                'PR must be in Submitted state to confirm the Comparative Statement.\n'
                'Current state: %s'
            ) % dict(self._fields['x_pr_state'].selection).get(
                self.x_pr_state, self.x_pr_state
            ))

        recommended = cs.x_recommended_vendor_line_id
        if not recommended or not recommended.x_partner_id:
            raise UserError(_('No recommended vendor is set on the Comparative Statement.'))

        # ── Set vendor on PR ──────────────────────────────────────────────────
        self.partner_id = recommended.x_partner_id

        # ── Copy winning vendor prices AND quantities to PR order lines ─────────
        for cs_line in recommended.x_line_ids:
            if not cs_line.x_product_id:
                continue
            pr_line = self.order_line.filtered(
                lambda l, p=cs_line.x_product_id: l.product_id == p and not l.display_type
            )[:1]
            if pr_line:
                if cs_line.x_unit_price:
                    pr_line.price_unit = cs_line.x_unit_price
                # Carry the quoted quantity as the recommended quantity on the PR line
                if cs_line.x_qty and cs_line.x_qty > 0:
                    pr_line.x_recommended_qty = cs_line.x_qty

        # ── Auto-set recommended qty = requested if not set by HO ────────────
        # (CEO will still decide final approved qty per line)
        for line in self.order_line.filtered(lambda l: l.product_id and not l.display_type):
            if not line.x_recommended_qty and line.x_requested_qty:
                line.x_recommended_qty = line.x_requested_qty

        # ── Advance PR state ──────────────────────────────────────────────────
        self.write({
            'x_ho_status': 'approved',
            'x_pr_state': 'ceo_final',
        })

        # ── Close any pending HO activities ──────────────────────────────────
        self.activity_ids.filtered(
            lambda a: a.user_id == self.env.user
        ).action_feedback(
            feedback=_('Approved via Comparative Statement — Vendor: %s')
            % recommended.x_partner_id.name
        )

        self.message_post(
            body=Markup(
                '✅ <b>Comparative Statement confirmed</b> by <b>%(user)s</b>.<br/>'
                'Selected vendor: <b>%(vendor)s</b>.<br/>'
                'PR routed to CEO for final approval.'
            ) % {
                'user': self.env.user.name,
                'vendor': recommended.x_partner_id.name,
            },
            subtype_xmlid='mail.mt_log_note',
        )

        # ── Schedule CEO activities ───────────────────────────────────────────
        ceo_users = self._get_group_users('purchase_demand_raise.group_ceo_approval')
        for user in ceo_users:
            self.activity_schedule(
                'mail.mail_activity_data_todo',
                summary=_('Final Approval Required: %s') % self.name,
                note=_(
                    'HO approved via Comparative Statement and recommended %(vendor)s. '
                    'Please review quantities and give final CEO approval.'
                ) % {'vendor': recommended.x_partner_id.name},
                user_id=user.id,
            )

        # ── Notify CEO ────────────────────────────────────────────────────────
        ceo_partners = self._get_group_partners('purchase_demand_raise.group_ceo_approval')
        self._notify_partners(
            ceo_partners,
            Markup(
                '📋 <b>Action Required — Final Approval: %(pr)s</b><br/>'
                'Procurement HO confirmed the Comparative Statement and '
                'recommended <b>%(vendor)s</b> as the vendor.<br/>'
                'Please review quantities and give final CEO approval.'
            ) % {'pr': self.name, 'vendor': recommended.x_partner_id.name},
        )

    # ── Override _prepare_picking: Site Procurement has no vendor partner ────
    def _prepare_picking(self):
        """For Site Procurement (no vendor), use the generic virtual supplier
        location instead of partner.property_stock_supplier, which fails when
        partner_id is False and raises 'You must set a Vendor Location'."""
        if not self.partner_id:
            vendor_location = self.env.ref(
                'stock.stock_location_suppliers', raise_if_not_found=False
            )
            if not vendor_location:
                raise UserError(_(
                    'Cannot create the receipt picking: no vendor is set and '
                    'the default "Virtual Locations/Vendors" location could not '
                    'be found. Please verify your Inventory configuration.'
                ))
            # Mirror what super() does for reference_ids but skip the partner check
            if not self.reference_ids:
                self.reference_ids = self.reference_ids.create(
                    self._prepare_reference_vals()
                )
            return {
                'picking_type_id': self.picking_type_id.id,
                'partner_id': False,
                'user_id': False,
                'origin': self.name,
                'location_dest_id': self._get_destination_location(),
                'location_id': vendor_location.id,
                'company_id': self.company_id.id,
                'state': 'draft',
                'reference_ids': [(6, 0, self.reference_ids.ids)],
            }
        return super()._prepare_picking()

    # ── Internal PR Print Report ──────────────────────────────────────────────
    def action_print_pr_internal(self):
        """Print the internal PR document with PM & Store Keeper signature boxes."""
        self.ensure_one()
        return self.env.ref(
            'purchase_demand_raise.action_report_pr_internal'
        ).report_action(self)
