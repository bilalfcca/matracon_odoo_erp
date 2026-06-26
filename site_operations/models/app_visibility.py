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
            'site_operations.group_matracon_admin',
            'base.group_system',
        ])

        menu_rules = [
            # Hidden from all Matracon roles except Admin
            ('mail.menu_root_discuss', admin_only),
            ('project_todo.menu_todo_todos', admin_only),
            ('contacts.menu_contacts', admin_only),
            ('base.menu_management', admin_only),
            ('base.menu_administration', admin_only),
            # Role-based apps
            ('purchase.menu_purchase_root', self._matracon_group_refs([
                'site_operations.group_mtr_app_purchase',
                'site_operations.group_matracon_admin',
                'base.group_system',
            ])),
            ('stock.menu_stock_root', self._matracon_group_refs([
                'site_operations.group_mtr_app_inventory',
                'site_operations.group_matracon_admin',
                'base.group_system',
            ])),
            ('account.menu_finance', self._matracon_group_refs([
                'site_operations.group_mtr_app_accounting',
                'site_operations.group_matracon_admin',
                'base.group_system',
            ])),
            ('project.menu_main_pm', self._matracon_group_refs([
                'site_operations.group_mtr_app_project',
                'site_operations.group_matracon_admin',
                'base.group_system',
            ])),
            ('hr.menu_hr_root', self._matracon_group_refs([
                'site_operations.group_mtr_app_hr',
                'site_operations.group_matracon_admin',
                'base.group_system',
            ])),
            ('hr_attendance.menu_hr_attendance_root', self._matracon_group_refs([
                'site_operations.group_mtr_app_attendance',
                'site_operations.group_matracon_admin',
                'base.group_system',
            ])),
            ('hr_payroll.menu_hr_payroll_menu_root', self._matracon_group_refs([
                'site_operations.group_mtr_app_payroll',
                'site_operations.group_matracon_admin',
                'base.group_system',
            ])),
            # Alternate payroll root id on some Odoo builds
            ('hr_payroll.menu_hr_payroll_root', self._matracon_group_refs([
                'site_operations.group_mtr_app_payroll',
                'site_operations.group_matracon_admin',
                'base.group_system',
            ])),
        ]

        for menu_xml_id, groups in menu_rules:
            menu = self.env.ref(menu_xml_id, raise_if_not_found=False)
            if not menu or not groups:
                continue
            menu.sudo().write({'group_ids': [(6, 0, groups.ids)]})
