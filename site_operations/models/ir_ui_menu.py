from odoo import api, models, tools


class IrUiMenu(models.Model):
    """Accountant HO sees only Petty Cash → Expenses inside the Invoicing app.

    Many standard Accounting menus have no groups and show for anyone who can
    read their action's model; Accountant HO needs read access to journal
    entries, accounts, journals and employees for the expense screen, which
    would otherwise surface Invoices, Bills, Products, Taxes and so on. Every
    other menu under the Invoicing app is removed for this role.
    """
    _inherit = 'ir.ui.menu'

    @api.model
    @tools.ormcache('frozenset(self.env.user._get_group_ids())', 'debug')
    def _visible_menu_ids(self, debug=False):
        visible = super()._visible_menu_ids(debug)
        user = self.env.user
        if not user.has_group('site_operations.group_accountant_ho') or user.has_group('base.group_system'):
            return visible
        finance = self.env.ref('account.menu_finance', raise_if_not_found=False)
        if not finance:
            return visible
        allowed = {finance.id} | {
            menu.id for menu in (
                self.env.ref('site_operations.menu_petty_cash_root', raise_if_not_found=False),
                self.env.ref('site_operations.menu_petty_cash_expenses', raise_if_not_found=False),
            ) if menu
        }
        under_finance = set(
            self.sudo().with_context(active_test=False).search([('id', 'child_of', finance.id)]).ids
        )
        return frozenset(visible - (under_finance - allowed))
