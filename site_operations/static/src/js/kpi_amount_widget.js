/** @odoo-module */
/**
 * KPI Amount Widget — matracon management dashboard
 *
 * Displays a Float/Monetary field without a currency symbol.
 * - Amounts ≥ 1 000 000 000 → "X.XXB"    (e.g.   1.25B)
 * - Amounts ≥ 1 000 000     → "X.XXM"    (e.g. 100.90M)
 * - Amounts < 1 000 000     → "X,XXX.XX" (plain, comma-separated)
 * Negative values are prefixed with "−".
 * Hovering the value shows the full number in a native tooltip.
 */

import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component, xml } from "@odoo/owl";

export class KpiAmountWidget extends Component {
    static template = xml`
        <span
            class="matracon-kpi-amount"
            t-att-title="fullValue"
            style="cursor:help;"
            t-esc="displayValue"
        />
    `;

    static props = {
        ...standardFieldProps,
    };

    get _raw() {
        return this.props.record.data[this.props.name] ?? 0;
    }

    get displayValue() {
        const raw = this._raw;
        const abs = Math.abs(raw);
        const neg = raw < 0 ? "−" : "";

        if (abs >= 1_000_000_000) {
            return neg + (abs / 1_000_000_000).toFixed(2) + "B";
        }
        if (abs >= 1_000_000) {
            return neg + (abs / 1_000_000).toFixed(2) + "M";
        }
        return neg + abs.toLocaleString("en-US", {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
        });
    }

    get fullValue() {
        const raw = this._raw;
        const abs = Math.abs(raw);
        const neg = raw < 0 ? "−" : "";
        return neg + abs.toLocaleString("en-US", {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
        });
    }
}

const kpiAmountField = {
    component: KpiAmountWidget,
    supportedTypes: ["float", "monetary"],
};

registry.category("fields").add("kpi_amount", kpiAmountField);
