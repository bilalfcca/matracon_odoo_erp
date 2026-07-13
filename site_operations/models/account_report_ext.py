from odoo import models


class AccountReport(models.Model):
    """
    Override accounting report option initialisation so that site accountants
    always see their own analytic account pre-selected and cannot switch to
    another site's account.

    Logic
    -----
    * Site accountants (`group_site_accountant`) who are NOT head-office users
      (`group_head_office`) have exactly one analytic account set as their
      default: `res.users.x_default_analytic_account_id`.
    * Whenever the report loads (or reloads with `previous_options`), we force
      `analytic_accounts` to that account and set `analytic_accounts_locked`
      so the JS layer can render a read-only badge instead of a dropdown.
    * HO users, Finance HO, and admins are unaffected.
    """

    _inherit = 'account.report'

    def _init_options_analytic(self, options, previous_options):
        # Run the standard initialisation first (sets display_analytic etc.)
        super()._init_options_analytic(options, previous_options)

        user = self.env.user
        is_site_accountant = user.has_group('site_operations.group_site_accountant')
        is_ho = user.has_group('purchase_demand_raise.group_head_office')

        if is_site_accountant and not is_ho:
            analytic = user.sudo().x_default_analytic_account_id
            if analytic:
                options['display_analytic'] = True
                options['analytic_accounts'] = [analytic.id]
                options['selected_analytic_account_names'] = [analytic.display_name]
                options['analytic_accounts_locked'] = True
