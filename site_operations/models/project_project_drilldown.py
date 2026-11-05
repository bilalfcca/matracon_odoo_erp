"""Project form smart buttons — every Matracon record tagged to the project, one click away.

Each entry in ``_mtr_drilldowns()`` describes one smart button: the model, the
domain that ties the records to this project, and (optionally) the existing
window action whose views should be reused. The same domain feeds the count on
the button and the list it opens, so the number always matches what is shown.

Counts respect the user's own access: a model the user cannot read counts as 0
and its button stays hidden, so no button ever opens an access error.
"""

import ast

from odoo import _, fields, models
from odoo.exceptions import UserError

PAYMENT_DONE = ('in_process', 'paid', 'partial', 'posted')


class ProjectProjectDrilldown(models.Model):
    _inherit = 'project.project'

    # ── Procurement ──────────────────────────────────────────────────────────
    x_mtr_po_count = fields.Integer(compute='_compute_mtr_counts', string='Requisitions / POs')
    x_mtr_cs_count = fields.Integer(compute='_compute_mtr_counts', string='Comparative Statements')
    # ── Stores ───────────────────────────────────────────────────────────────
    x_mtr_receipt_count = fields.Integer(compute='_compute_mtr_counts', string='Goods Receipts')
    x_mtr_issuance_count = fields.Integer(compute='_compute_mtr_counts', string='Issuances')
    x_mtr_return_count = fields.Integer(compute='_compute_mtr_counts', string='Returns')
    x_mtr_s2s_count = fields.Integer(compute='_compute_mtr_counts', string='Site Transfers')
    # ── Payables / receivables ───────────────────────────────────────────────
    x_mtr_bill_count = fields.Integer(compute='_compute_mtr_counts', string='Vendor Bills')
    x_mtr_invoice_count = fields.Integer(compute='_compute_mtr_counts', string='Customer Invoices')
    x_mtr_liability_count = fields.Integer(compute='_compute_mtr_counts', string='Liability Sheets')
    x_mtr_ipc_count = fields.Integer(compute='_compute_mtr_counts', string='IPCs')
    x_mtr_sub_bc_count = fields.Integer(compute='_compute_mtr_counts', string='Subcontractor Back Charges')
    x_mtr_emp_bc_count = fields.Integer(compute='_compute_mtr_counts', string='Employee Back Charges')
    # ── Finance ──────────────────────────────────────────────────────────────
    x_mtr_payment_count = fields.Integer(compute='_compute_mtr_counts', string='Vendor Payments')
    x_mtr_client_receipt_count = fields.Integer(compute='_compute_mtr_counts', string='Receipts')
    x_mtr_je_count = fields.Integer(compute='_compute_mtr_counts', string='Journal Entries')
    x_mtr_aml_count = fields.Integer(compute='_compute_mtr_counts', string='Journal Items')
    x_mtr_ipt_count = fields.Integer(compute='_compute_mtr_counts', string='Inter-Project Transfers')
    x_mtr_bg_count = fields.Integer(compute='_compute_mtr_counts', string='Bank Guarantees')
    # ── Petty cash ───────────────────────────────────────────────────────────
    x_mtr_pcr_count = fields.Integer(compute='_compute_mtr_counts', string='Petty Cash Requests')
    x_mtr_pce_count = fields.Integer(compute='_compute_mtr_counts', string='Petty Cash Expenses')
    # ── HR / payroll ─────────────────────────────────────────────────────────
    x_mtr_employee_count = fields.Integer(compute='_compute_mtr_counts', string='Employees')
    x_mtr_attendance_count = fields.Integer(compute='_compute_mtr_counts', string='Attendance Sheets')
    x_mtr_salary_count = fields.Integer(compute='_compute_mtr_counts', string='Salary Sheets')

    # ─────────────────────────────────────────────────────────────────────────
    def _mtr_drilldowns(self):
        """Return {key: spec} for this project; empty when no analytic is linked.

        spec keys: model, domain, name, count_field, and optionally action
        (xmlid whose views/search view are reused) and context.
        """
        self.ensure_one()
        aid = self.x_analytic_account_id.id
        if not aid:
            return {}
        wh = self.x_site_config_id.warehouse_id.id
        site_picking = (
            ['|', ('picking_type_id.warehouse_id', '=', wh)] if wh else []
        )
        po_link = ['|', ('x_project_analytic_account_id', '=', aid),
                   ('x_project_site_config_id', '=', self.x_site_config_id.id)] \
            if self.x_site_config_id else [('x_project_analytic_account_id', '=', aid)]
        move_on_project = ['|', ('x_project_analytic_account_id', '=', aid),
                           ('line_ids.analytic_distribution', 'in', [aid])]
        return {
            'po': {
                'model': 'purchase.order', 'action': 'purchase_demand_raise.action_prs_head_office',
                'name': _('Requisitions / POs'), 'count_field': 'x_mtr_po_count',
                'domain': [('x_is_alternative_rfq', '!=', True)] + po_link,
            },
            'cs': {
                'model': 'x.comparative.statement', 'action': 'purchase_demand_raise.action_comparative_statement',
                'name': _('Comparative Statements'), 'count_field': 'x_mtr_cs_count',
                'domain': [('x_purchase_order_id.x_project_analytic_account_id', '=', aid)],
            },
            'receipt': {
                'model': 'stock.picking', 'action': 'stock.action_picking_tree_all',
                'name': _('Goods Receipts'), 'count_field': 'x_mtr_receipt_count',
                'domain': [('picking_type_code', '=', 'incoming'),
                           ('picking_type_id.warehouse_id', '=', wh or -1)],
            },
            'issuance': {
                'model': 'stock.picking', 'action': 'site_operations.action_material_issuance_all',
                'name': _('Material Issuances'), 'count_field': 'x_mtr_issuance_count',
                'domain': [('x_transfer_purpose', '=', 'material_issuance'),
                           ('x_is_return_transfer', '!=', True)]
                          + site_picking + [('x_issuance_project_id', '=', aid)],
            },
            'return': {
                'model': 'stock.picking', 'action': 'site_operations.action_material_return',
                'name': _('Material Returns'), 'count_field': 'x_mtr_return_count',
                'domain': [('x_transfer_purpose', '=', 'material_issuance'),
                           ('x_is_return_transfer', '=', True)]
                          + site_picking + [('x_issuance_project_id', '=', aid)],
            },
            's2s': {
                'model': 'stock.picking', 'action': 'site_operations.action_site_to_site_transfers',
                'name': _('Site-to-Site Transfers'), 'count_field': 'x_mtr_s2s_count',
                'domain': [('x_transfer_purpose', '=', 'site_to_site')]
                          + site_picking + [('x_dest_project_id', '=', aid)],
            },
            'bill': {
                'model': 'account.move', 'action': 'account.action_move_in_invoice_type',
                'name': _('Vendor Bills'), 'count_field': 'x_mtr_bill_count',
                'domain': [('move_type', 'in', ('in_invoice', 'in_refund')),
                           ('x_project_analytic_account_id', '=', aid)],
            },
            'invoice': {
                'model': 'account.move', 'action': 'account.action_move_out_invoice_type',
                'name': _('Customer Invoices'), 'count_field': 'x_mtr_invoice_count',
                'domain': [('move_type', 'in', ('out_invoice', 'out_refund')),
                           ('x_project_analytic_account_id', '=', aid)],
            },
            'liability': {
                'model': 'x.liability.sheet', 'action': 'site_operations.action_liability_sheet',
                'name': _('Liability Sheets'), 'count_field': 'x_mtr_liability_count',
                'domain': [('project_analytic_account_id', '=', aid)],
            },
            'ipc': {
                'model': 'x.subcontractor.ipc', 'action': 'site_operations.action_subcontractor_ipc',
                'name': _('Subcontractor IPCs'), 'count_field': 'x_mtr_ipc_count',
                'domain': [('project_analytic_account_id', '=', aid)],
            },
            'sub_bc': {
                'model': 'x.subcontractor.backcharge', 'action': 'site_operations.action_subcontractor_backcharge',
                'name': _('Subcontractor Back Charges'), 'count_field': 'x_mtr_sub_bc_count',
                'domain': [('project_analytic_account_id', '=', aid)],
            },
            'emp_bc': {
                'model': 'x.employee.backcharge', 'action': 'site_operations.action_employee_backcharge',
                'name': _('Employee Back Charges'), 'count_field': 'x_mtr_emp_bc_count',
                'domain': [('project_analytic_account_id', '=', aid)],
            },
            'payment': {
                'model': 'account.payment', 'action': 'site_operations.action_ho_vendor_payments',
                'name': _('Vendor Payments'), 'count_field': 'x_mtr_payment_count',
                'domain': [('payment_type', '=', 'outbound'),
                           '|', '|', ('x_destination_project_id', '=', aid),
                           ('x_fund_project_id', '=', aid),
                           ('x_allocation_ids.project_analytic_account_id', '=', aid)],
            },
            'client_receipt': {
                'model': 'account.payment', 'action': 'account.action_account_payments',
                'name': _('Receipts'), 'count_field': 'x_mtr_client_receipt_count',
                'domain': [('payment_type', '=', 'inbound'),
                           '|', ('x_fund_project_id', '=', aid),
                           ('x_destination_project_id', '=', aid)],
            },
            'je': {
                'model': 'account.move', 'action': 'account.action_move_journal_line',
                'name': _('Journal Entries'), 'count_field': 'x_mtr_je_count',
                'domain': [('move_type', '=', 'entry'), '|', '|',
                           ('x_ho_source_project_id', '=', aid),
                           ('x_je_allocation_ids.project_analytic_account_id', '=', aid)]
                          + move_on_project,
            },
            'aml': {
                'model': 'account.move.line', 'action': 'account.action_account_moves_all',
                'name': _('Journal Items'), 'count_field': 'x_mtr_aml_count',
                'domain': [('parent_state', '=', 'posted'),
                           '|', ('move_id.x_project_analytic_account_id', '=', aid),
                           ('analytic_distribution', 'in', [aid])],
            },
            'ipt': {
                'model': 'x.interproject.transfer', 'action': 'site_operations.action_interproject_transfer',
                'name': _('Inter-Project Transfers'), 'count_field': 'x_mtr_ipt_count',
                'domain': ['|', ('source_analytic_id', '=', aid), ('dest_analytic_id', '=', aid)],
            },
            'bg': {
                'model': 'x.bank.guarantee', 'action': 'site_operations.action_bank_guarantee',
                'name': _('Bank Guarantees'), 'count_field': 'x_mtr_bg_count',
                'domain': [('project_id', '=', self.id)],
            },
            'pcr': {
                'model': 'x.petty.cash.request', 'action': 'site_operations.action_petty_cash_request',
                'name': _('Petty Cash Requests'), 'count_field': 'x_mtr_pcr_count',
                'domain': [('project_analytic_account_id', '=', aid)],
            },
            'pce': {
                'model': 'x.petty.cash.expense', 'action': 'site_operations.action_petty_cash_expense',
                'name': _('Petty Cash Expenses'), 'count_field': 'x_mtr_pce_count',
                'domain': [('fund_id.project_analytic_account_id', '=', aid)],
            },
            'employee': {
                'model': 'hr.employee', 'action': 'hr.open_view_employee_list_my',
                'name': _('Employees'), 'count_field': 'x_mtr_employee_count',
                'domain': [('x_project_analytic_account_id', '=', aid)],
            },
            'attendance': {
                'model': 'x.attendance.sheet', 'action': 'site_operations.action_attendance_sheet',
                'name': _('Attendance Sheets'), 'count_field': 'x_mtr_attendance_count',
                'domain': [('project_analytic_account_id', '=', aid)],
            },
            'salary': {
                'model': 'x.salary.sheet', 'action': 'site_operations.action_salary_sheet',
                'name': _('Salary Sheets'), 'count_field': 'x_mtr_salary_count',
                'domain': [('project_analytic_account_id', '=', aid)],
            },
        }

    def _compute_mtr_counts(self):
        count_fields = [
            name for name, field in self._fields.items()
            if name.startswith('x_mtr_') and name.endswith('_count')
            and field.compute == '_compute_mtr_counts'
        ]
        for project in self:
            values = dict.fromkeys(count_fields, 0)
            for spec in (project._mtr_drilldowns() if project.id else {}).values():
                Model = self.env.get(spec['model'])
                if Model is None or not Model.has_access('read'):
                    continue
                values[spec['count_field']] = Model.search_count(spec['domain'])
            project.update(values)

    def action_mtr_open(self):
        """Open the records behind one smart button (key passed in context)."""
        self.ensure_one()
        key = self.env.context.get('mtr_key')
        spec = self._mtr_drilldowns().get(key)
        if not spec:
            raise UserError(_('Link an analytic account to this project first.'))
        action = {}
        if spec.get('action'):
            ref = self.env.ref(spec['action'], raise_if_not_found=False)
            if ref:
                action = ref._get_action_dict()
        context = action.get('context') or {}
        if isinstance(context, str):
            try:
                context = ast.literal_eval(context)
            except (ValueError, SyntaxError):
                context = {}
        # Drop the source menu's default filters / groupings so the list shows
        # exactly the records counted on the button.
        context = {k: v for k, v in context.items()
                   if not k.startswith('search_default_') and k != 'group_by'}
        context.update(spec.get('context') or {}, create=False)
        action.update({
            'type': 'ir.actions.act_window',
            'name': '%s — %s' % (spec['name'], self.name),
            'res_model': spec['model'],
            'domain': spec['domain'],
            'context': context,
            'target': 'current',
        })
        if not action.get('views'):
            action['views'] = [(False, 'list'), (False, 'form')]
            action['view_mode'] = 'list,form'
        return action

    # ── Fund summary drill-downs (the records behind each total) ─────────────
    def _mtr_require_analytic(self):
        self.ensure_one()
        if not self.x_analytic_account_id:
            raise UserError(_('Link an analytic account to this project first.'))
        return self.x_analytic_account_id.id

    def _mtr_window(self, name, model, domain, xmlid=None, context=None):
        action = {}
        if xmlid:
            ref = self.env.ref(xmlid, raise_if_not_found=False)
            if ref:
                action = ref._get_action_dict()
        action.update({
            'type': 'ir.actions.act_window',
            'name': '%s — %s' % (name, self.name),
            'res_model': model,
            'domain': domain,
            'context': dict(context or {}, create=False),
            'target': 'current',
        })
        if not action.get('views'):
            action['views'] = [(False, 'list'), (False, 'form')]
            action['view_mode'] = 'list,form'
        return action

    def action_mtr_view_funds_received(self):
        aid = self._mtr_require_analytic()
        return self._mtr_window(
            _('Funds Received'), 'account.payment',
            [('payment_type', '=', 'inbound'), ('state', 'in', PAYMENT_DONE),
             ('x_fund_project_id', '=', aid)],
            'account.action_account_payments')

    def action_mtr_view_total_spent(self):
        """Payments and journal entries that make up Total Spent."""
        aid = self._mtr_require_analytic()
        Allocation = self.env['x.payment.project.allocation']
        payments = Allocation.search([
            ('project_analytic_account_id', '=', aid),
            ('payment_id.payment_type', '=', 'outbound'),
            ('payment_id.state', 'in', PAYMENT_DONE),
        ]).payment_id
        payments |= self.env['account.payment'].search([
            ('payment_type', '=', 'outbound'), ('state', 'in', PAYMENT_DONE),
            ('x_fund_project_id', '=', aid), ('x_allocation_ids', '=', False),
        ])
        moves = payments.move_id
        moves |= self.env['account.move'].search([
            ('state', '=', 'posted'), ('move_type', '=', 'entry'),
            '|', ('x_ho_source_project_id', '=', aid),
            ('x_je_allocation_ids.project_analytic_account_id', '=', aid),
        ])
        return self._mtr_window(
            _('Total Spent'), 'account.move', [('id', 'in', moves.ids)],
            'account.action_move_journal_line')

    def _mtr_payable_partner_ids(self):
        """Partners with an open payable balance on this project (same SQL basis
        as the Vendor / Subcontractor Liabilities totals)."""
        aid = self._mtr_require_analytic()
        self.env.cr.execute("""
            SELECT aml.partner_id
              FROM account_move_line aml
              JOIN account_move am  ON am.id  = aml.move_id
              JOIN account_account aa ON aa.id = aml.account_id
             WHERE am.state        = 'posted'
               AND aa.account_type = 'liability_payable'
               AND aml.partner_id  IS NOT NULL
               AND (am.x_project_analytic_account_id = %s
                    OR (aml.analytic_distribution IS NOT NULL
                        AND aml.analytic_distribution ? %s))
             GROUP BY aml.partner_id
            HAVING SUM(aml.credit - aml.debit) > 0.005
        """, [aid, str(aid)])
        partners = self.env['res.partner'].sudo().browse(
            [row[0] for row in self.env.cr.fetchall()])
        subs = partners.filtered(lambda p: p.category_id.filtered(
            lambda c: 'subcontractor' in (c.name or '').lower()))
        return aid, (partners - subs).ids, subs.ids

    def _mtr_payable_lines(self, name, partner_ids, aid):
        return self._mtr_window(
            name, 'account.move.line',
            [('parent_state', '=', 'posted'),
             ('account_id.account_type', '=', 'liability_payable'),
             ('partner_id', 'in', partner_ids),
             '|', ('move_id.x_project_analytic_account_id', '=', aid),
             ('analytic_distribution', 'in', [aid])],
            'account.action_account_moves_all',
            {'group_by': ['partner_id']})

    def action_mtr_view_vendor_liability(self):
        aid, vendor_ids, _subs = self._mtr_payable_partner_ids()
        return self._mtr_payable_lines(_('Vendor Liabilities'), vendor_ids, aid)

    def action_mtr_view_sub_liability(self):
        aid, _vendors, sub_ids = self._mtr_payable_partner_ids()
        return self._mtr_payable_lines(_('Subcontractor Liabilities'), sub_ids, aid)

    def action_mtr_view_billed(self):
        aid = self._mtr_require_analytic()
        return self._mtr_window(
            _('Billed to Client'), 'account.move',
            [('move_type', 'in', ('out_invoice', 'out_refund')),
             ('state', '=', 'posted'), ('x_project_analytic_account_id', '=', aid)],
            'account.action_move_out_invoice_type')

    def action_mtr_view_site_stock(self):
        self.ensure_one()
        wh = self.x_site_config_id.warehouse_id
        if not wh:
            raise UserError(_('No warehouse is linked to this project\'s site configuration.'))
        return self._mtr_window(
            _('Site Stock'), 'stock.quant',
            [('location_id', 'child_of', wh.view_location_id.id),
             ('location_id.usage', '=', 'internal')],
            'site_operations.action_site_store_current_stock')
