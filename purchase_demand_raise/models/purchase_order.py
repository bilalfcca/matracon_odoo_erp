from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    # ── Fix: Make vendor NOT required (Site Store raises PR without vendor) ──
    partner_id = fields.Many2one('res.partner', string='Vendor', required=False, tracking=True)

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

    # ── Category ─────────────────────────────────────────────────────────────
    x_category_id = fields.Many2one(
        'product.category', string='Select Category', tracking=True,
        help='All products on lines must belong to this category.'
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

    @api.depends('x_pr_state', 'x_pm_signed_pr', 'x_category_id', 'x_is_alternative_rfq', 'x_pr_origin')
    def _compute_is_pr_document(self):
        for order in self:
            if order.x_is_alternative_rfq:
                order.x_is_pr_document = False
                continue
            order.x_is_pr_document = bool(
                order.x_pr_state != 'draft'              # Already progressed in workflow
                or order.x_pm_signed_pr                   # PM doc attached → it's a PR
                or order.x_category_id                    # Category selected → our PR form
                or order.x_pr_origin == 'procurement_ho'  # PO-raised PR (always PR workflow)
            )

    # ── Role flags (used in view expressions — Odoo 19 forbids groups() in attrs) ──
    x_is_site_store = fields.Boolean(compute='_compute_role_flags')
    x_is_ho = fields.Boolean(compute='_compute_role_flags')
    x_is_ceo = fields.Boolean(compute='_compute_role_flags')

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
        return super().create(vals_list)

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

    # ── Default picking_type_id to site user's warehouse ─────────────────────
    @api.model
    def default_get(self, fields_list):
        defaults = super().default_get(fields_list)
        user = self.env.user
        is_ho = user.has_group('purchase_demand_raise.group_procurement_ho')
        is_site_store = user.has_group('purchase_demand_raise.group_site_store')
        # PO creating a new RFQ → mark as procurement_ho PR so PR workflow shows immediately
        if is_ho and not is_site_store:
            defaults['x_pr_origin'] = 'procurement_ho'
        if 'picking_type_id' in fields_list:
            if is_site_store:
                pt = self._matracon_pr_receipt_picking_type(user)
                if pt:
                    defaults['picking_type_id'] = pt.id
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
        if partner_ids:
            self.message_subscribe(partner_ids=list(set(partner_ids)))

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

    def action_submit_pr(self):
        """Submit PR — Site Store (with PM doc) or Procurement Officer (direct to CEO)."""
        for order in self:
            is_ho = self.env.user.has_group('purchase_demand_raise.group_procurement_ho')
            if not is_ho and not order.x_pm_signed_pr:
                raise UserError(_('Please attach the PM Signed PR before submitting.'))
            if not order.order_line:
                raise UserError(_('Please add at least one product line before submitting.'))

            ho_raised = is_ho and order.x_pr_origin != 'site_store'
            if ho_raised:
                order.x_pr_origin = 'procurement_ho'
                if not order.x_project_site_config_id:
                    raise UserError(_('Please select the Project before submitting.'))
                # Auto-set recommended = requested for lines where HO hasn't changed qty
                for line in order.order_line:
                    if not line.x_recommended_qty and line.x_requested_qty:
                        line.x_recommended_qty = line.x_requested_qty

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
                           'Please review, select a vendor, and approve to route to CEO.') % {
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
                    'PO is now locked and confirmed — ready for dispatch to vendor.'
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
                       'PO is confirmed. You may now dispatch it to the vendor.') % {'pr': order.name}
            )
            if order.x_initiator_id and order.x_initiator_id.partner_id:
                order._notify_partners(
                    [order.x_initiator_id.partner_id.id],
                    Markup('✅ Your PR <b>%(pr)s</b> has been fully approved. '
                           'The Purchase Order is confirmed and materials will be ordered.') % {'pr': order.name}
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
        """
        for order in self:
            if order.state not in ('purchase', 'done', 'to approve'):
                continue
            incoming = order.picking_ids.filtered(
                lambda p: p.picking_type_code == 'incoming'
                and p.state not in ('done', 'cancel')
            )
            if incoming:
                continue
            if order.state == 'to approve' and hasattr(order, 'button_approve'):
                order.sudo().button_approve()
                continue
            if hasattr(order, '_create_picking'):
                order.sudo()._create_picking()
        self.invalidate_recordset(['picking_ids', 'incoming_picking_count'])

    def action_view_picking(self):
        """Open the incoming receipt form (Site Store) — not a list view."""
        self.ensure_one()
        self._matracon_ensure_receipt_pickings()
        incoming = self.picking_ids.filtered(
            lambda p: p.picking_type_code == 'incoming'
        ).sorted(key=lambda p: (p.state in ('done', 'cancel'), p.id))
        open_pickings = incoming.filtered(lambda p: p.state not in ('done', 'cancel'))
        picking = open_pickings[:1] or incoming[:1]
        if picking:
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

    def button_send_rfq(self):
        """Override: block standard Send RFQ for Site Store & enforce approval gate."""
        if self.env.user.has_group('purchase_demand_raise.group_site_store'):
            raise UserError(_('Site Store cannot send RFQs. Please submit the PR for approval.'))
        for order in self:
            if order.x_pr_state not in ('submitted', 'ceo_final', 'po_locked'):
                raise UserError(_(
                    'RFQ cannot be sent at this stage.\n'
                    'Current PR Status: %s'
                ) % dict(self._fields['x_pr_state'].selection).get(order.x_pr_state, order.x_pr_state))
        return super().button_send_rfq()

    def button_cancel(self):
        """Override: block cancellation of rejected PR documents (already in terminal state)."""
        for order in self:
            if order.x_is_pr_document and order.x_pr_state == 'rejected':
                raise UserError(_('This PR has already been rejected. Cancellation is not allowed on a rejected PR.'))
        return super().button_cancel()

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

    # ── Internal PR Print Report ──────────────────────────────────────────────
    def action_print_pr_internal(self):
        """Print the internal PR document with PM & Store Keeper signature boxes."""
        self.ensure_one()
        return self.env.ref(
            'purchase_demand_raise.action_report_pr_internal'
        ).report_action(self)
