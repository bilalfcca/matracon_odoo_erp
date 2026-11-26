/** @odoo-module **/
/**
 * Smart search for the control-panel search bar.
 *
 * 1. Amount search fields (x_amt_*) are offered only when the typed text is an
 *    amount expression (>5000, <1000, =2500, 1000-5000, between 1 lac and 5 lac).
 * 2. On Products and Contacts, the dropdown also lists the best matching
 *    records while typing; picking one opens it directly.
 */
import { patch } from "@web/core/utils/patch";
import { KeepLast } from "@web/core/utils/concurrency";
import { SearchBar } from "@web/search/search_bar/search_bar";

const UNIT = "(k|thousand|lacs?|lakhs?|mn|m|million|crores?|cr)?";
const NUM = `(-?\\d[\\d,]*(\\.\\d+)?|-?\\.\\d+)\\s*${UNIT}`;
const AMOUNT_RE = new RegExp(
    `^\\s*((between\\s+)?${NUM}\\s*(-|\\.\\.|to|and)\\s*${NUM}|(>=|<=|=>|=<|>|<|==|=)?\\s*${NUM})\\s*$`,
    "i"
);
export const isAmountExpression = (text) => AMOUNT_RE.test(text || "");

const SMART_MODELS = ["res.partner", "product.template", "product.product"];
const SMART_DELAY = 200;
let nextSmartId = 1000000000;

patch(SearchBar.prototype, {
    setup() {
        super.setup(...arguments);
        this.mtrSmartKeepLast = new KeepLast();
        this.mtrSmartCache = { query: null, records: [] };
    },

    getItems(searchItem, trimmedQuery) {
        if (searchItem.fieldName?.startsWith("x_amt_") && !isAmountExpression(trimmedQuery)) {
            return [];
        }
        return super.getItems(...arguments);
    },

    mtrSmartEnabled() {
        const sm = this.env.searchModel;
        return (
            SMART_MODELS.includes(sm?.resModel) &&
            !this.env.inDialog &&
            ["list", "kanban"].includes(this.env.config?.viewType)
        );
    },

    async computeState() {
        await super.computeState(...arguments);
        if (this.mtrSmartEnabled()) {
            this.mtrSmartSuggest();
        }
    },

    mtrSmartSuggest() {
        const query = this.state.query.trim();
        clearTimeout(this.mtrSmartTimer);
        if (query.length < 2 || isAmountExpression(query)) {
            return;
        }
        if (this.mtrSmartCache.query === query) {
            this.mtrSmartInsert(this.mtrSmartCache.records);
            return;
        }
        this.mtrSmartTimer = setTimeout(async () => {
            const { resModel } = this.env.searchModel;
            let records;
            try {
                records = await this.mtrSmartKeepLast.add(
                    this.orm.silent.call("x.smart.search", "suggest", [resModel, query])
                );
            } catch {
                return;
            }
            this.mtrSmartCache = { query, records };
            if (this.state.query.trim() === query) {
                this.mtrSmartInsert(records);
            }
        }, SMART_DELAY);
    },

    mtrSmartInsert(records) {
        if (!records.length || this.items.some((i) => i.isMtrRecord)) {
            return;
        }
        const resModel = this.env.searchModel.resModel;
        const smartItems = records.map((rec, index) => ({
            id: nextSmartId++,
            isMtrRecord: true,
            isMtrFirst: index === 0,
            resModel,
            resId: rec.id,
            label: rec.label,
            details: rec.details,
            title: rec.details,
        }));
        const customIndex = this.items.findIndex((i) => i.isAddCustomFilterButton);
        const at = customIndex >= 0 ? customIndex : this.items.length;
        this.items.splice(at, 0, ...smartItems);
    },

    selectItem(item) {
        if (item.isMtrRecord) {
            this.inputDropdownState.close();
            this.resetState();
            return this.env.services.action.doAction({
                type: "ir.actions.act_window",
                res_model: item.resModel,
                res_id: item.resId,
                views: [[false, "form"]],
                target: "current",
            });
        }
        return super.selectItem(...arguments);
    },
});
