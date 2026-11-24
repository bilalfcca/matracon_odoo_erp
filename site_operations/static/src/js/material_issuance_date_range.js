/** @odoo-module **/
/**
 * Material Issuance — "Date Range" picker inside the search panel (Filters column).
 *
 * Shown only on search views that declare the marker filter
 * <filter name="filter_issuance_date" .../> (the Material Issuance search view).
 * Picking From / To and pressing Apply adds one removable facet:
 *     Issuance Date: 01/11/2026 → 20/11/2026
 * filtering on the stored local date field x_issuance_date.
 */
import { useState } from "@odoo/owl";
import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import { Domain } from "@web/core/domain";
import { deserializeDate, formatDate } from "@web/core/l10n/dates";
import { SearchBarMenu } from "@web/search/search_bar_menu/search_bar_menu";

const MARKER_FILTER = "filter_issuance_date";
const RANGE_FIELD = "x_issuance_date";

patch(SearchBarMenu.prototype, {
    setup() {
        super.setup(...arguments);
        this.mdrState = useState({ dateFrom: "", dateTo: "", error: "" });
    },

    get showMaterialDateRange() {
        const searchModel = this.env.searchModel;
        return (
            RANGE_FIELD in (searchModel.searchViewFields || {}) &&
            searchModel.getSearchItems((item) => item.name === MARKER_FILTER).length > 0
        );
    },

    _mdrActiveGroupIds() {
        const searchModel = this.env.searchModel;
        const groupIds = new Set();
        for (const queryElem of searchModel.query) {
            const item = searchModel.searchItems[queryElem.searchItemId];
            if (item && item.isMaterialDateRange) {
                groupIds.add(item.groupId);
            }
        }
        return [...groupIds];
    },

    get mdrIsActive() {
        return this._mdrActiveGroupIds().length > 0;
    },

    onMdrClear() {
        for (const groupId of this._mdrActiveGroupIds()) {
            this.env.searchModel.deactivateGroup(groupId);
        }
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
            leaves.push([RANGE_FIELD, ">=", dateFrom]);
        }
        if (dateTo) {
            leaves.push([RANGE_FIELD, "<=", dateTo]);
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

        // Replace any previously applied range instead of stacking them
        for (const groupId of this._mdrActiveGroupIds()) {
            this.env.searchModel.deactivateGroup(groupId);
        }
        this.env.searchModel.createNewFilters([
            {
                description: _t("Issuance Date: %s", label),
                domain: new Domain(leaves).toString(),
                isMaterialDateRange: true,
            },
        ]);
        this.props.dropdownState?.close?.();
    },
});
