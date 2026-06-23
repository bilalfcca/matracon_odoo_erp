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
        ('dispatched', 'Dispatched'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
    ], string='PR Status', default='draft', tracking=True, copy=False)

    # ── HO & CEO Approval Status Badges ─────────────────────────────────────
    x_ho_status = fields.Selection([
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ], string='HO Status', default='pending', tracking=True, copy=False)

    x_ceo_status = fields.Selection([
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ], string='CEO Status', default='pending', tracking=True, copy=False)

    # ── Category ─────────────────────────────────────────────────────────────
    x_category_id = fields.Many2one(
        'product.category', string='Select Category', tracking=True,
        help='All products on lines must belong to this category.'
    )

    # ── PM Signed PR Attachment ───────────────────────────────────────────────
    x_pm_signed_pr = fields.Binary(string='PM Signed PR', attachment=True, copy=False)
    x_pm_signed_pr_filename = fields.Char(string='PM Signed PR Filename')

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

    @api.depends('x_pr_state', 'x_pm_signed_pr', 'x_category_id', 'x_is_alternative_rfq')
    def _compute_is_pr_document(self):
        for order in self:
            if order.x_is_alternative_rfq:
                order.x_is_pr_document = False
                continue
            order.x_is_pr_document = bool(
                order.x_pr_state != 'draft'   # Already progressed in workflow
                or order.x_pm_signed_pr        # PM doc attached → it's a PR
                or order.x_category_id         # Category selected → our PR form
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

    # ── Computed helper: can Submit button be enabled? ────────────────────────
    x_can_submit = fields.Boolean(compute='_compute_can_submit')

    @api.depends('x_pm_signed_pr', 'x_pr_state', 'order_line')
    def _compute_can_submit(self):
        for order in self:
            order.x_can_submit = bool(
                order.x_pm_signed_pr
                and order.x_pr_state == 'draft'
                and order.order_line
            )

    # ── Override create: set initiator + PR flag + analytic for site users ──────
    @api.model_create_multi
    def create(self, vals_list):
        is_site_store = self.env.user.has_group('purchase_demand_raise.group_site_store')
        for vals in vals_list:
            if 'x_initiator_id' not in vals:
                vals['x_initiator_id'] = self.env.user.id
            if is_site_store:
                # Auto-assign project analytic account for site store users
                if 'x_project_analytic_account_id' not in vals:
                    analytic = self.env.user.x_default_analytic_account_id
                    if analytic:
                        vals['x_project_analytic_account_id'] = analytic.id
        return super().create(vals_list)

    # ── Default picking_type_id to site user's warehouse ─────────────────────
    @api.model
    def default_get(self, fields_list):
        defaults = super().default_get(fields_list)
        if 'picking_type_id' in fields_list:
            user = self.env.user
            if user.has_group('purchase_demand_raise.group_site_store'):
                warehouse = user.x_default_warehouse_id
                if warehouse and warehouse.in_type_id:
                    defaults['picking_type_id'] = warehouse.in_type_id.id
        return defaults

    # ── When analytic account changes on PO → push to all lines ──────────────
    @api.onchange('x_project_analytic_account_id')
    def _onchange_project_analytic_account(self):
        if self.x_project_analytic_account_id:
            acc_id = str(self.x_project_analytic_account_id.id)
            for line in self.order_line:
                line.analytic_distribution = {acc_id: 100.0}

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

    def action_submit_pr(self):
        """Site Store submits the PR — routes to Procurement HO for review and vendor recommendation."""
        for order in self:
            if not order.x_pm_signed_pr:
                raise UserError(_('Please attach the PM Signed PR before submitting.'))
            if not order.order_line:
                raise UserError(_('Please add at least one product line before submitting.'))

            order.write({
                'x_pr_state': 'submitted',
                'x_ho_status': 'pending',
                'x_ceo_status': 'pending',
            })
            order._ensure_followers()
            order._schedule_approval_activities()

            # Internal log
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

    def action_ceo_final_approve(self):
        """CEO gives final line-level approval — PO confirmed and locked."""
        for order in self:
            if order.x_is_alternative_rfq:
                raise UserError(_(
                    'CEO final approval applies only to the main Purchase Requisition, '
                    'not to alternative vendor RFQs.'
                ))
            if order.x_pr_state != 'ceo_final':
                raise UserError(_('PR must be in CEO Final Review state.'))
            for line in order.order_line:
                if line.x_approved_qty <= 0:
                    raise UserError(_('Please set Approved Qty > 0 for all lines before final approval.'))

            # Sync product_qty to approved_qty for the actual PO
            for line in order.order_line:
                line.product_qty = line.x_approved_qty

            order.write({'x_pr_state': 'po_locked', 'x_ceo_status': 'approved'})
            # Confirm the PO in standard Odoo (button_confirm won't block po_locked)
            order.button_confirm()

            order.message_post(
                body=Markup('🔒 <b>CEO Final Approval</b> granted by <b>%s</b>. '
                            'PO is now locked and confirmed — ready for dispatch to vendor.') % self.env.user.name,
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
        """CEO rejects at final stage."""
        for order in self:
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

    def action_dispatch_to_vendor(self):
        """Dispatch confirmed PO to vendor — only after CEO final lock."""
        self.ensure_one()
        if self.x_pr_state != 'po_locked':
            raise UserError(_('PO must be Locked (CEO final approved) before dispatching to vendor.'))
        if not self.partner_id:
            raise UserError(_('Please set the Vendor before dispatching the PO.'))

        self.x_pr_state = 'dispatched'
        self.message_post(
            body=Markup('📤 PO dispatched to vendor <b>%(vendor)s</b> by <b>%(user)s</b>.') % {
                'vendor': self.partner_id.name, 'user': self.env.user.name,
            },
            subtype_xmlid='mail.mt_log_note',
        )
        if self.x_initiator_id and self.x_initiator_id.partner_id:
            self._notify_partners(
                [self.x_initiator_id.partner_id.id],
                Markup('📤 PO <b>%(pr)s</b> has been dispatched to vendor <b>%(vendor)s</b>. Materials are on their way!') % {
                    'pr': self.name, 'vendor': self.partner_id.name,
                }
            )
        # Print the Final PO report (CEO-approved vendor copy with prices)
        return self.env.ref(
            'purchase_demand_raise.action_report_final_po'
        ).report_action(self)

    def button_confirm(self):
        """Override: guard standard Confirm Order against bypassing the PR workflow.

        Only applies to real PR documents (x_is_pr_document=True).
        Odoo native alternative RFQs (x_is_pr_document=False) pass through freely —
        they are standard vendor quote RFQs managed by the Alternatives feature.

        For PR documents:
        - draft / submitted / rejected / cancelled → blocked
        - rfq_phase / ceo_final → allowed if vendor is set; auto-advances to po_locked
        - po_locked / dispatched → pass-through (already CEO-approved)
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

        return super().button_confirm()

    def button_send_rfq(self):
        """Override: block standard Send RFQ for Site Store & enforce approval gate."""
        if self.env.user.has_group('purchase_demand_raise.group_site_store'):
            raise UserError(_('Site Store cannot send RFQs. Please submit the PR for approval.'))
        for order in self:
            if order.x_pr_state not in ('submitted', 'ceo_final', 'po_locked', 'dispatched'):
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
