"""Restrict Odoo home-screen apps per Matracon role."""

from odoo import api, models


class MatraconAppVisibility(models.AbstractModel):
    _name = 'x.matracon.app.visibility'
    _description = 'Matracon App Menu Visibility'

    @api.model
    def _matracon_group_refs(self, xml_ids):
        groups = self.env['res.groups']
        for xml_id in xml_ids:
            group = self.env.ref(xml_id, raise_if_not_found=False)
            if group:
                groups |= group
        return groups

    @api.model
    def apply_menu_visibility(self):
        """Apply root-menu groups for the Matracon app switcher (idempotent)."""
        admin_only = self._matracon_group_refs([
            'purchase_demand_raise.group_matracon_admin',
            'base.group_system',
        ])

        menu_rules = [
            # ── Hidden from all Matracon roles except Admin ───────────────────
            ('mail.menu_root_discuss', admin_only),
            ('project_todo.menu_todo_todos', admin_only),
            ('base.menu_management', admin_only),
            ('base.menu_administration', admin_only),
            # Calendar and Time Off — not needed by any site/FO/CEO role
            ('calendar.calendar_menu', admin_only),
            ('hr_holidays.menu_open_root', admin_only),
            # Dashboards (spreadsheet) — admin only
            ('spreadsheet_dashboard.menu_spreadsheet_dashboard', admin_only),
            ('board.menu_board_note_global', admin_only),

            # ── Role-based apps ───────────────────────────────────────────────
            # Purchase — Site Store, Procurement, CEO, Admin
            ('purchase.menu_purchase_root', self._matracon_group_refs([
                'site_operations.group_mtr_app_purchase',
                'purchase_demand_raise.group_matracon_admin',
                'base.group_system',
            ])),
            # Inventory — Site Store, Procurement, Admin
            ('stock.menu_stock_root', self._matracon_group_refs([
                'site_operations.group_mtr_app_inventory',
                'purchase_demand_raise.group_matracon_admin',
                'base.group_system',
            ])),
            # Accounting — Site Accountant, FO, CEO, Admin
            ('account.menu_finance', self._matracon_group_refs([
                'site_operations.group_mtr_app_accounting',
                'purchase_demand_raise.group_matracon_admin',
                'base.group_system',
            ])),
            # Project — Admin only (no role needs Project app)
            ('project.menu_main_pm', self._matracon_group_refs([
                'site_operations.group_mtr_app_project',
                'purchase_demand_raise.group_matracon_admin',
                'base.group_system',
            ])),
            # Employees (HR) — FO, Admin
            ('hr.menu_hr_root', self._matracon_group_refs([
                'site_operations.group_mtr_app_hr',
                'purchase_demand_raise.group_matracon_admin',
                'base.group_system',
            ])),
            # Attendance — Site Accountant, FO, Admin
            ('hr_attendance.menu_hr_attendance_root', self._matracon_group_refs([
                'site_operations.group_mtr_app_attendance',
                'purchase_demand_raise.group_matracon_admin',
                'base.group_system',
            ])),
            # Payroll — FO, Admin
            ('hr_payroll.menu_hr_payroll_menu_root', self._matracon_group_refs([
                'site_operations.group_mtr_app_payroll',
                'purchase_demand_raise.group_matracon_admin',
                'base.group_system',
            ])),
            # Alternate payroll root id on some Odoo builds
            ('hr_payroll.menu_hr_payroll_root', self._matracon_group_refs([
                'site_operations.group_mtr_app_payroll',
                'purchase_demand_raise.group_matracon_admin',
                'base.group_system',
            ])),
            # Contacts — Procurement + Admin (for vendor management)
            ('contacts.menu_contacts', self._matracon_group_refs([
                'site_operations.group_mtr_app_contacts',
                'purchase_demand_raise.group_matracon_admin',
                'base.group_system',
            ])),
            # Barcode — Inventory users only (Site Store + Procurement + Admin)
            ('stock_barcode.menu_barcode_root', self._matracon_group_refs([
                'site_operations.group_mtr_app_inventory',
                'purchase_demand_raise.group_matracon_admin',
                'base.group_system',
            ])),
        ]

        for menu_xml_id, groups in menu_rules:
            menu = self.env.ref(menu_xml_id, raise_if_not_found=False)
            if not menu or not groups:
                continue
            menu.sudo().write({'group_ids': [(6, 0, groups.ids)]})
