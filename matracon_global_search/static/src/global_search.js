import { Component, onWillStart, useState } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useAutofocus, useService } from "@web/core/utils/hooks";

const MODEL = "matracon.global.search";

// Kept between openings so the last search is still there when reopened.
const memory = {
    term: "",
    mode: "all",
    types: [],
    dateFrom: "",
    dateTo: "",
    states: [],
    result: null,
};

export class GlobalSearchDialog extends Component {
    static template = "matracon_global_search.GlobalSearchDialog";
    static components = { Dialog };
    static props = {
        close: Function,
        showFilters: { type: Boolean, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.inputRef = useAutofocus({ refName: "input", selectAll: true });
        this.state = useState({
            term: memory.term,
            mode: memory.mode,
            types: [...memory.types],
            dateFrom: memory.dateFrom,
            dateTo: memory.dateTo,
            states: [...memory.states],
            result: memory.result,
            showFilters: !!this.props.showFilters,
            typeQuery: "",
            loading: false,
            error: "",
            options: { modes: [], types: [], min_term: 3 },
        });
        onWillStart(async () => {
            this.state.options = await this.orm.call(MODEL, "get_search_options", []);
        });
    }

    // ── computed ──────────────────────────────────────────────────────────
    get currentMode() {
        return this.state.options.modes.find((m) => m.key === this.state.mode);
    }
    get placeholder() {
        const hints = {
            all: _t("Search anything… (name, memo, reference, message)"),
            partner: _t("Contact or employee name, e.g. Awais"),
            description: _t("Text in a memo / description / note"),
            reference: _t("PO, bill, DMR, gate pass, cheque number…"),
            amount: _t("Amount, e.g. 25000 or 25,000.50"),
            date: _t("Date, e.g. 15/10/2026"),
            project: _t("Project or site name"),
        };
        return hints[this.state.mode] || "";
    }
    get filteredTypes() {
        const q = this.state.typeQuery.trim().toLowerCase();
        const types = this.state.options.types;
        return q ? types.filter((t) => t.label.toLowerCase().includes(q)) : types;
    }
    get typeLabels() {
        const byModel = Object.fromEntries(this.state.options.types.map((t) => [t.model, t.label]));
        return this.state.types.map((m) => ({ model: m, label: byModel[m] || m }));
    }
    get availableStates() {
        const seen = new Map();
        for (const group of this.state.result?.groups || []) {
            for (const [key, label] of group.states || []) {
                if (!seen.has(key)) {
                    seen.set(key, label);
                }
            }
        }
        for (const key of this.state.states) {
            if (!seen.has(key)) {
                seen.set(key, key);
            }
        }
        return [...seen.entries()].map(([key, label]) => ({ key, label }));
    }
    get activeFilterCount() {
        return (
            this.state.types.length +
            this.state.states.length +
            (this.state.dateFrom ? 1 : 0) +
            (this.state.dateTo ? 1 : 0)
        );
    }
    get totalCount() {
        return (this.state.result?.groups || []).reduce((n, g) => n + g.count, 0);
    }

    // ── actions ───────────────────────────────────────────────────────────
    setMode(key) {
        this.state.mode = key;
        this.inputRef.el?.focus();
        if (this.state.term.trim()) {
            this.search();
        }
    }
    toggleFilters() {
        this.state.showFilters = !this.state.showFilters;
    }
    toggleType(model) {
        const i = this.state.types.indexOf(model);
        i >= 0 ? this.state.types.splice(i, 1) : this.state.types.push(model);
    }
    toggleState(key) {
        const i = this.state.states.indexOf(key);
        i >= 0 ? this.state.states.splice(i, 1) : this.state.states.push(key);
    }
    clearFilters() {
        Object.assign(this.state, { types: [], states: [], dateFrom: "", dateTo: "" });
    }
    onKeydown(ev) {
        if (ev.key === "Enter") {
            ev.preventDefault();
            this.search();
        }
    }

    async search() {
        const term = this.state.term.trim();
        const needsLength = !["amount", "date"].includes(this.state.mode);
        if (!term || (needsLength && term.length < this.state.options.min_term)) {
            this.state.error = _t("Type at least %s characters, then press Enter.", this.state.options.min_term);
            return;
        }
        this.state.loading = true;
        this.state.error = "";
        try {
            const result = await this.orm.call(MODEL, "global_search", [term], {
                mode: this.state.mode,
                model_names: this.state.types.length ? this.state.types : null,
                date_from: this.state.dateFrom || null,
                date_to: this.state.dateTo || null,
                states: this.state.states.length ? this.state.states : null,
            });
            this.state.result = result;
            Object.assign(memory, {
                term,
                mode: this.state.mode,
                types: [...this.state.types],
                dateFrom: this.state.dateFrom,
                dateTo: this.state.dateTo,
                states: [...this.state.states],
                result,
            });
        } catch (e) {
            this.state.error = e?.data?.message || e?.message || _t("Search failed.");
            this.state.result = null;
        } finally {
            this.state.loading = false;
        }
    }

    openRecord(rec) {
        this.props.close();
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: rec.model,
            res_id: rec.id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    openAll(group) {
        if (!group.domain) {
            return;
        }
        this.props.close();
        this.action.doAction({
            type: "ir.actions.act_window",
            name: group.label,
            res_model: group.model,
            domain: group.domain,
            views: [
                [false, "list"],
                [false, "form"],
            ],
            context: { active_test: false },
            target: "current",
        });
    }
}

export class GlobalSearchSystray extends Component {
    static template = "matracon_global_search.Systray";
    static props = {};

    setup() {
        this.dialog = useService("dialog");
    }
    open(showFilters = false) {
        this.dialog.add(GlobalSearchDialog, { showFilters });
    }
}

registry
    .category("systray")
    .add("matracon_global_search", { Component: GlobalSearchSystray }, { sequence: 120 });
