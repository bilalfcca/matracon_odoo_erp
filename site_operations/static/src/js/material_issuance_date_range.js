/** @odoo-module **/
/**
 * Matracon search-panel pickers (Filters column of the search dropdown).
 *
 *  • Date Range   — From / To date pickers → one removable facet, e.g.
 *                   "Issuance Date: 01/11/2026 → 20/11/2026"
 *  • Debit Account (Petty Cash Expenses only) — pick one or more accounts from
 *                   the chart of accounts → shows vouchers with at least one line
 *                   debited to them, e.g. "Debit Account: 6290 Mess Expense"
 *
 * A search view opts in by declaring its marker filter (see RANGE_CONFIGS):
 *   Material Issuance      → <filter name="filter_issuance_date" …/>
 *   Petty Cash Expenses    → <filter name="filter_expense_date" …/>
 */
import { useState } from "@odoo/owl";
import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import { Domain } from "@web/core/domain";
import { useService } from "@web/core/utils/hooks";
import { deserializeDate, formatDate } from "@web/core/l10n/dates";
import { MultiRecordSelector } from "@web/core/record_selectors/multi_record_selector";
import { SearchBarMenu } from "@web/search/search_bar_menu/search_bar_menu";

const RANGE_CONFIGS = [
    {
        marker: "filter_issuance_date",
        field: "x_issuance_date",
        label: "Issuance Date",
    },
    {
        marker: "filter_expense_date",
        field: "expense_date",
        label: "Expense Date",
        account: {
            requiredField: "line_ids",
            path: "line_ids.x_summary_account_id",
            label: "Debit Account",
        },
    },
];

SearchBarMenu.components = { ...SearchBarMenu.components, MultiRecordSelector };

patch(SearchBarMenu.prototype, {
    setup() {
        super.setup(...arguments);
        this.mdrState = useState({ dateFrom: "", dateTo: "", error: "" });
        this.mdaState = useState({ accountIds: [], error: "" });
        this.mdrNameService = useService("name");
    },

    /** Active picker config for this search view, or null. */
    get mdrConfig() {
        const searchModel = this.env.searchModel;
        const fields = searchModel.searchViewFields || {};
        for (const config of RANGE_CONFIGS) {
            if (
                config.field in fields &&
                searchModel.getSearchItems((item) => item.name === config.marker).length
            ) {
                return config;
            }
        }
        return null;
    },

    // Kept for the template condition name used before
    get showMaterialDateRange() {
        return Boolean(this.mdrConfig);
    },

    get mdrRangeLabel() {
        return _t(this.mdrConfig.label);
    },

    get showDebitAccountFilter() {
        const config = this.mdrConfig;
        return Boolean(
            config &&
                config.account &&
                config.account.requiredField in (this.env.searchModel.searchViewFields || {})
        );
    },

    _mdrActiveGroupIds(flag) {
        const searchModel = this.env.searchModel;
        const groupIds = new Set();
        for (const queryElem of searchModel.query) {
            const item = searchModel.searchItems[queryElem.searchItemId];
            if (item && item[flag]) {
                groupIds.add(item.groupId);
            }
        }
        return [...groupIds];
    },

    _mdrDeactivate(flag) {
        for (const groupId of this._mdrActiveGroupIds(flag)) {
            this.env.searchModel.deactivateGroup(groupId);
        }
    },

    // ── Date Range ──────────────────────────────────────────────────────────
    get mdrIsActive() {
        return this._mdrActiveGroupIds("isMaterialDateRange").length > 0;
    },

    onMdrClear() {
        this._mdrDeactivate("isMaterialDateRange");
        this.mdrState.dateFrom = "";
        this.mdrState.dateTo = "";
        this.mdrState.error = "";
    },

    onMdrKeydown(ev) {
        if (ev.key === "Enter") {
            ev.preventDefault();
            this.onMdrApply();
        }
    },

    onMdrApply() {
        const config = this.mdrConfig;
        const { dateFrom, dateTo } = this.mdrState;
        this.mdrState.error = "";
        if (!dateFrom && !dateTo) {
            this.mdrState.error = _t("Select at least one date.");
            return;
        }
        if (dateFrom && dateTo && dateFrom > dateTo) {
            this.mdrState.error = _t('"From" cannot be after "To".');
            return;
        }
        const leaves = [];
        if (dateFrom) {
            leaves.push([config.field, ">=", dateFrom]);
        }
        if (dateTo) {
            leaves.push([config.field, "<=", dateTo]);
        }
        const fmt = (value) => formatDate(deserializeDate(value));
        let label;
        if (dateFrom && dateTo) {
            label = `${fmt(dateFrom)} → ${fmt(dateTo)}`;
        } else if (dateFrom) {
            label = _t("from %s", fmt(dateFrom));
        } else {
            label = _t("until %s", fmt(dateTo));
        }
        this._mdrDeactivate("isMaterialDateRange");
        this.env.searchModel.createNewFilters([
            {
                description: `${_t(config.label)}: ${label}`,
                domain: new Domain(leaves).toString(),
                isMaterialDateRange: true,
            },
        ]);
        this.props.dropdownState?.close?.();
    },

    // ── Debit Account ───────────────────────────────────────────────────────
    get mdaIsActive() {
        return this._mdrActiveGroupIds("isMatraconDebitAccount").length > 0;
    },

    onMdaUpdate(resIds) {
        this.mdaState.accountIds = resIds;
        this.mdaState.error = "";
    },

    onMdaClear() {
        this._mdrDeactivate("isMatraconDebitAccount");
        this.mdaState.accountIds = [];
        this.mdaState.error = "";
    },

    async onMdaApply() {
        const config = this.mdrConfig;
        const ids = this.mdaState.accountIds;
        this.mdaState.error = "";
        if (!ids.length) {
            this.mdaState.error = _t("Select at least one account.");
            return;
        }
        const names = await this.mdrNameService.loadDisplayNames("account.account", ids);
        const label = ids.map((id) => names[id] || `#${id}`).join(", ");
        this._mdrDeactivate("isMatraconDebitAccount");
        this.env.searchModel.createNewFilters([
            {
                description: `${_t(config.account.label)}: ${label}`,
                domain: new Domain([[config.account.path, "in", ids]]).toString(),
                isMatraconDebitAccount: true,
            },
        ]);
        this.props.dropdownState?.close?.();
    },
});
