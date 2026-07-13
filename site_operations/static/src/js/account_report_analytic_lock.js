/**
 * Patch the AccountReportFilterAnalytic template:
 * When the backend has set `analytic_accounts_locked = True`, render a
 * read-only chip showing the pre-selected analytic account instead of the
 * interactive Dropdown.
 *
 * This prevents site accountants from switching the analytic filter to
 * another site's account.
 */
import { patch } from "@web/core/utils/patch";
import { AccountReportFilters } from "@account_reports/components/account_report/filters/filters";

patch(AccountReportFilters.prototype, {
    /**
     * Returns true when the backend has locked the analytic filter for this
     * user (i.e. they are a site accountant with a single forced account).
     */
    get isAnalyticLocked() {
        return Boolean(this.controller.cachedFilterOptions.analytic_accounts_locked);
    },

    /**
     * Returns the display name of the locked analytic account.
     * Uses `selected_analytic_account_names` which is already set by the
     * backend _init_options_analytic override.
     */
    get lockedAnalyticName() {
        const names = this.controller.cachedFilterOptions.selected_analytic_account_names;
        return (names && names.length) ? names[0] : "";
    },
});
