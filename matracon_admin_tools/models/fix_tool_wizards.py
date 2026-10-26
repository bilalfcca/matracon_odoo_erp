"""Tiny "pick a record, then run" wizards for fix tools whose underlying
method is inherently record-specific (it corrects one particular
petty cash request / site configuration / fleet log entry), unlike the
global sweep-style fix tools which just run directly from a Server Action
with no picker needed.

Each wizard only ever calls a pre-existing model method - none of the
underlying fix logic lives here or is changed here. See
data/fix_tools_data.xml for the Server Actions that open these wizards.
"""
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class XPettyCashGlFixWizard(models.TransientModel):
    """Fix Petty Cash GL Entry - was a button on the x.petty.cash.request
    form (site_operations/models/petty_cash.py: action_fix_petty_cash_entry,
    unchanged). Picking the request here replaces opening it and clicking
    the button there.
    """
    _name = 'x.petty.cash.gl.fix.wizard'
    _description = 'Fix Petty Cash GL Entry'

    petty_cash_request_id = fields.Many2one(
        'x.petty.cash.request', string='Petty Cash Request', required=True,
        domain=[('state', 'in', ('released', 'confirmed')), ('payment_id', '!=', False)],
        help='Only requests that have been released (a payment exists) can be fixed.',
    )

    def action_apply(self):
        self.ensure_one()
        if not (self.env.user.has_group('site_operations.group_finance_ho')
                or self.env.user.has_group('purchase_demand_raise.group_matracon_admin')):
            raise UserError(_('Only Finance HO or Matracon Admin can run this action.'))
        return self.petty_cash_request_id.action_fix_petty_cash_entry()


class XSiteConfigFixWizard(models.TransientModel):
    """Shared picker for the three Site Configuration fix tools - was three
    separate buttons on the x.project.site.config form
    (site_operations/models/project_site_config.py:
    action_fix_old_interproject_entries, action_fix_material_issue_accounts,
    action_fix_analytic_visibility - all unchanged). Each Server Action in
    Fix Tools opens this same wizard with a different default fix_type via
    context, so the list still shows three distinct, clearly-named entries.
    """
    _name = 'x.site.config.fix.wizard'
    _description = 'Fix Site Configuration Data'

    fix_type = fields.Selection([
        ('interproject', 'Merge Old Inter-Project Accounts'),
        ('material_issue', 'Apply Material Issue Account to Old Issuances'),
        ('analytic_visibility', 'Rebuild Analytic Visibility Links'),
    ], required=True, default='interproject')
    site_config_id = fields.Many2one(
        'x.project.site.config', string='Site Configuration', required=True,
        help='The fix runs for this site (Inter-Project merge additionally sweeps '
             'all sites at once, since the account is shared).',
    )
    fix_type_help = fields.Text(compute='_compute_fix_type_help')

    @api.depends('fix_type')
    def _compute_fix_type_help(self):
        help_by_type = {
            'interproject': _(
                'Updates ALL previously posted inter-project journal lines (across all '
                'sites) from the old hardcoded accounts (13100/21100) to this site\'s '
                'configured Inter-Project Account. Run this once after setting the account.'
            ),
            'material_issue': _(
                'Updates all previously posted material issuance journal entries for this '
                'site to use its configured Material Issue Account.'
            ),
            'analytic_visibility': _(
                'Scans all posted accounting entries for this site\'s analytic account and '
                'ensures they are visible to the site accountant and appear correctly in '
                'all accounting reports.'
            ),
        }
        for wizard in self:
            wizard.fix_type_help = help_by_type.get(wizard.fix_type, '')

    def action_apply(self):
        self.ensure_one()
        if not (self.env.user.has_group('purchase_demand_raise.group_matracon_admin')
                or self.env.user.has_group('base.group_system')):
            raise UserError(_('Only Matracon Admin or System Administrator can run this action.'))
        config = self.site_config_id
        if self.fix_type == 'interproject':
            return config.action_fix_old_interproject_entries()
        if self.fix_type == 'material_issue':
            return config.action_fix_material_issue_accounts()
        return config.action_fix_analytic_visibility()


class XFleetGlFixWizard(models.TransientModel):
    """Unlink GL - was a button on the fleet.vehicle.log.services form
    (matracon_fleet/models/fleet_log_entry_ext.py: action_unlink_gl,
    unchanged).
    """
    _name = 'x.fleet.gl.fix.wizard'
    _description = 'Fix Fleet Service Log GL Link'

    log_entry_id = fields.Many2one(
        'fleet.vehicle.log.services', string='Fleet Service Log', required=True,
        domain=[('x_account_move_id', '!=', False)],
        help='Only log entries with a linked journal entry can be fixed.',
    )

    def action_apply(self):
        self.ensure_one()
        if not (self.env.user.has_group('purchase_demand_raise.group_matracon_admin')
                or self.env.user.has_group('base.group_system')):
            raise UserError(_('Only Matracon Admin or System Administrator can run this action.'))
        log_entry = self.log_entry_id
        # action_unlink_gl() itself returns nothing (it just clears the link)
        # - show a confirmation notification instead of leaving the dialog open.
        log_entry.action_unlink_gl()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('GL Link Removed'),
                'message': _('The journal entry link on %(name)s has been detached. '
                             'The journal entry itself was not reversed.', name=log_entry.display_name),
                'type': 'success',
                'sticky': False,
            },
        }


class XPoPriceFixWizard(models.TransientModel):
    """Force Fix PO Price & Inventory Valuation - corrects locked or confirmed
    Purchase Orders in-place without requiring unlock, receipt reversal or
    cancellation. Also synchronizes all linked inventory stock moves, valuation
    layers, and accounting entries.
    """
    _name = 'x.po.price.fix.wizard'
    _description = 'Force Fix PO Price & Inventory Valuation'

    purchase_order_id = fields.Many2one(
        'purchase.order', string='Purchase Order', required=True,
        default=lambda self: self._default_purchase_order(),
        help='Select the Purchase Order to update.',
    )
    product_id = fields.Many2one(
        'product.product', string='Product (Optional)',
        help='If specified, only lines for this product are updated. Otherwise all product lines on the PO are updated.',
    )
    current_unit_price = fields.Float(
        string='Current Unit Price', compute='_compute_current_price', readonly=True,
    )
    new_unit_price = fields.Float(
        string='New Unit Price', digits='Product Price', required=True, default=398.76,
        help='The corrected unit price to apply.',
    )
    update_inventory = fields.Boolean(
        string='Update Inventory Valuation & Stock Moves', default=True,
        help='Updates all linked stock moves and valuation layers to match the new unit price.',
    )
    update_journal_entries = fields.Boolean(
        string='Update Linked Stock Accounting Entries', default=True,
        help='Updates debit and credit amounts on stock move journal entries to match the new valuation.',
    )

    @api.model
    def _default_purchase_order(self):
        active_id = self.env.context.get('active_id')
        if active_id and self.env.context.get('active_model') == 'purchase.order':
            return active_id
        po = self.env['purchase.order'].sudo().search([('name', '=', 'P-STP-0140')], limit=1)
        return po.id if po else False

    @api.depends('purchase_order_id', 'product_id')
    def _compute_current_price(self):
        for wizard in self:
            price = 0.0
            if wizard.purchase_order_id:
                lines = wizard.purchase_order_id.order_line.filtered(lambda l: not l.display_type)
                if wizard.product_id:
                    lines = lines.filtered(lambda l: l.product_id == wizard.product_id)
                if lines:
                    price = lines[0].price_unit
            wizard.current_unit_price = price

    @api.model
    def action_force_fix_po_stp_0140_server(self):
        """Server Action entry point: Forcefully fixes P-STP-0140 to 398.76 Rs."""
        po = self.env['purchase.order'].sudo().search([('name', '=', 'P-STP-0140')], limit=1)
        if not po:
            raise UserError(_("Purchase Order 'P-STP-0140' was not found in this database."))
        wizard = self.create({
            'purchase_order_id': po.id,
            'new_unit_price': 398.76,
            'update_inventory': True,
            'update_journal_entries': True,
        })
        return wizard.action_apply()

    def action_apply(self):
        self.ensure_one()
        if not (self.env.user.has_group('purchase_demand_raise.group_matracon_admin')
                or self.env.user.has_group('site_operations.group_finance_ho')
                or self.env.user.has_group('base.group_system')):
            raise UserError(_('Only Matracon Admin, Finance HO or System Administrator can run this fix.'))

        po = self.purchase_order_id
        if not po:
            raise UserError(_('Please select a Purchase Order.'))

        new_price = self.new_unit_price
        if new_price <= 0:
            raise UserError(_('New unit price must be greater than zero.'))

        # 1. Update Purchase Order Lines
        lines = po.order_line.filtered(lambda l: not l.display_type)
        if self.product_id:
            lines = lines.filtered(lambda l: l.product_id == self.product_id)

        if not lines:
            raise UserError(_('No matching product lines found on %s.') % po.name)

        updated_line_names = []
        for line in lines:
            old_price = line.price_unit
            qty = line.product_qty or line.x_approved_qty or 0.0
            new_subtotal = round(qty * new_price, 2)
            line.sudo().write({
                'price_unit': new_price,
                'price_subtotal': new_subtotal,
                'price_total': new_subtotal,
            })
            updated_line_names.append(f"{line.product_id.display_name} ({old_price} -> {new_price})")

        # 2. Recalculate Purchase Order Total
        po_untaxed = sum(l.price_subtotal for l in po.order_line)
        po_total = sum(l.price_total for l in po.order_line)
        po.sudo().write({
            'amount_untaxed': po_untaxed,
            'amount_total': po_total,
        })

        # 3. Update Inventory Stock Moves
        moves_count = 0
        if self.update_inventory:
            moves = self.env['stock.move'].sudo().search([
                '|', ('purchase_line_id', 'in', lines.ids),
                '|', ('origin', '=', po.name),
                ('picking_id.purchase_id', '=', po.id),
            ])
            if self.product_id:
                moves = moves.filtered(lambda m: m.product_id == self.product_id)

            for move in moves:
                m_qty = move.quantity or move.product_uom_qty or 0.0
                new_move_val = round(m_qty * new_price, 2)
                move.sudo().write({
                    'price_unit': new_price,
                    'value': new_move_val,
                })
                moves_count += 1

                # 4. Update Stock Valuation Layers (if model exists)
                if 'stock.valuation.layer' in self.env:
                    layers = self.env['stock.valuation.layer'].sudo().search([
                        '|', ('stock_move_id', '=', move.id),
                        ('description', 'ilike', po.name),
                    ])
                    for svl in layers:
                        svl_val = round(svl.quantity * new_price, 2)
                        rem_val = round(svl.remaining_qty * new_price, 2)
                        svl.write({
                            'unit_cost': new_price,
                            'value': svl_val,
                            'remaining_value': rem_val,
                        })

                # 5. Update Stock Move Accounting Entries (if any and requested)
                if self.update_journal_entries and move.account_move_id:
                    je = move.account_move_id
                    for aml in je.line_ids:
                        if aml.debit > 0:
                            self.env.cr.execute(
                                "UPDATE account_move_line SET debit = %s, balance = %s WHERE id = %s",
                                (new_move_val, new_move_val, aml.id)
                            )
                        elif aml.credit > 0:
                            self.env.cr.execute(
                                "UPDATE account_move_line SET credit = %s, balance = %s WHERE id = %s",
                                (new_move_val, -new_move_val, aml.id)
                            )
                    self.env.cr.execute(
                        "UPDATE account_move SET amount_total = %s WHERE id = %s",
                        (new_move_val, je.id)
                    )

        # 6. Update draft vendor bill lines (if any)
        draft_bills = self.env['account.move'].sudo().search([
            ('move_type', '=', 'in_invoice'),
            ('state', '=', 'draft'),
            '|', ('x_purchase_order_id', '=', po.id),
            ('invoice_origin', '=', po.name),
        ])
        for bill in draft_bills:
            for bline in bill.invoice_line_ids.filtered(lambda l: l.product_id and l.product_id.id in lines.product_id.ids):
                bline.write({'price_unit': new_price})
            try:
                bill._recompute_dynamic_lines(recompute_all_taxes=True)
            except Exception:
                pass

        # Invalidate caches
        self.env['purchase.order'].invalidate_model()
        self.env['purchase.order.line'].invalidate_model()
        self.env['stock.move'].invalidate_model()
        self.env['stock.quant'].invalidate_model()
        if 'stock.valuation.layer' in self.env:
            self.env['stock.valuation.layer'].invalidate_model()

        # 7. Post chatter audit note on PO
        po.message_post(
            body=_(
                "<b>[Admin Fix Tool] Force Price &amp; Valuation Update:</b><br/>"
                "Unit price forcefully updated to <b>%(price).2f Rs.</b><br/>"
                "PO Untaxed / Total Amount updated to: <b>PKR %(total)s</b>.<br/>"
                "Updated <b>%(moves)d</b> inventory stock move(s) and valuation layers.",
                price=new_price,
                total=f"{po_total:,.2f}",
                moves=moves_count,
            ),
            subtype_xmlid='mail.mt_note',
        )

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('PO Price & Valuation Updated'),
                'message': _('Successfully updated %(po)s unit price to %(price).2f Rs. (Total: PKR %(total)s). Updated %(moves)d inventory moves and valuation layers.',
                             po=po.name, price=new_price, total=f"{po_total:,.2f}", moves=moves_count),
                'type': 'success',
                'sticky': False,
                'next': {
                    'type': 'ir.actions.act_window',
                    'res_model': 'purchase.order',
                    'res_id': po.id,
                    'view_mode': 'form',
                    'views': [(False, 'form')],
                    'target': 'current',
                },
            },
        }


class XBgAccountingRepairWizard(models.TransientModel):
    """Admin Fix Tool: Audit & Repair Bank Guarantee Accounting Entries.
    Provides a preview/audit report, duplicate detection, and execution summary.
    """
    _name = 'x.bg.accounting.repair.wizard'
    _description = 'Bank Guarantee Accounting Repair Wizard'

    total_scanned = fields.Integer(string='Scanned Guarantees', readonly=True)
    total_created = fields.Integer(string='Entries Created', readonly=True)
    total_repaired = fields.Integer(string='Entries Linked/Repaired', readonly=True)
    total_skipped = fields.Integer(string='Guarantees Skipped', readonly=True)
    total_failed = fields.Integer(string='Failed / Needs Attention', readonly=True)
    audit_report_html = fields.Html(string='Audit Report', readonly=True)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        audit_res = self.env['x.bank.guarantee'].action_admin_repair_bg_accounting(dry_run=True)
        res.update({
            'total_scanned': audit_res.get('total_scanned', 0),
            'total_created': audit_res.get('total_created', 0),
            'total_repaired': audit_res.get('total_repaired', 0),
            'total_skipped': audit_res.get('total_skipped', 0),
            'total_failed': audit_res.get('total_failed', 0),
            'audit_report_html': audit_res.get('report_html', ''),
        })
        return res

    def action_preview(self):
        self.ensure_one()
        audit_res = self.env['x.bank.guarantee'].action_admin_repair_bg_accounting(dry_run=True)
        self.write({
            'total_scanned': audit_res.get('total_scanned', 0),
            'total_created': audit_res.get('total_created', 0),
            'total_repaired': audit_res.get('total_repaired', 0),
            'total_skipped': audit_res.get('total_skipped', 0),
            'total_failed': audit_res.get('total_failed', 0),
            'audit_report_html': audit_res.get('report_html', ''),
        })
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_apply_repair(self):
        self.ensure_one()
        if not (self.env.user.has_group('site_operations.group_finance_ho')
                or self.env.user.has_group('purchase_demand_raise.group_matracon_admin')
                or self.env.user.has_group('base.group_system')):
            raise UserError(_('Only Finance HO, Matracon Admin or System Administrator can run this repair.'))
        repair_res = self.env['x.bank.guarantee'].action_admin_repair_bg_accounting(dry_run=False)
        self.write({
            'total_scanned': repair_res.get('total_scanned', 0),
            'total_created': repair_res.get('total_created', 0),
            'total_repaired': repair_res.get('total_repaired', 0),
            'total_skipped': repair_res.get('total_skipped', 0),
            'total_failed': repair_res.get('total_failed', 0),
            'audit_report_html': repair_res.get('report_html', ''),
        })
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }
