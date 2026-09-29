from odoo import _, fields, models


class AccountReport(models.Model):
    """
    Extends accounting reports with two site-specific behaviours:

    1. All Dates filter
       Adds an "All Dates" option to every date-filter dropdown that runs the
       report from 1900-01-01 to today — effectively "no date restriction".
       Implemented by intercepting _init_options_date when filter == 'all_dates'
       and substituting a wide custom range before delegating to super().

    2. Site-accountant analytic lock
       Site accountants always see their own analytic account pre-selected and
       cannot switch to another site's account.  HO / Finance HO / admins are
       unaffected.
    """

    _inherit = 'account.report'

    # ── All Dates ──────────────────────────────────────────────────────────────

    def _init_options_date(self, options, previous_options):
        date_opts = previous_options.get('date', {})
        if date_opts.get('filter') == 'all_dates':
            # Pretend to super() that this is a custom range from the
            # beginning of recorded time to today.  Super fills all required
            # keys (date_from, date_to, mode, period_type, string, …), then
            # we restore our filter name and set a human-friendly string.
            today = fields.Date.to_string(fields.Date.context_today(self))
            modified_prev = dict(previous_options)
            modified_prev['date'] = dict(date_opts,
                filter='custom',
                date_from='1900-01-01',
                date_to=today,
            )
            super()._init_options_date(options, modified_prev)
            options['date']['filter'] = 'all_dates'
            options['date']['string'] = _('All Dates')
        else:
            super()._init_options_date(options, previous_options)

    # ── Analytic lock for site accountants ────────────────────────────────────

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
