/** @odoo-module */
/**
 * Matracon Progress Bar — extends the standard progressbar widget to show
 * precise decimal values instead of rounding small percentages to "0".
 *
 * The standard widget calls formatCurrentValue(humanReadable=true) which
 * internally uses humanNumber(value, 0), rounding 0.0001 → "0".
 *
 * This variant uses toFixed(4) + parseFloat to strip trailing zeros while
 * preserving meaningful precision for all values:
 *   0.0001  → "0.0001"
 *   10.5    → "10.5"
 *   60.0    → "60"
 *   100.0   → "100"
 */

import {
    ProgressBarField,
    progressBarField,
} from "@web/views/fields/progress_bar/progress_bar_field";
import { registry } from "@web/core/registry";

export class MatraconProgressBarField extends ProgressBarField {
    /**
     * Override to prevent humanReadable mode which rounds tiny values to "0".
     * Always show up to 4 significant decimal places, trailing zeros stripped.
     */
    formatCurrentValue(humanReadable = !this.state.isEditing) {
        const val = this.currentValue;
        if (!val) return "0";
        // toFixed(4) keeps 4 decimal places; parseFloat strips trailing zeros.
        return String(parseFloat(val.toFixed(4)));
    }
}

const matraconProgressBarField = {
    ...progressBarField,
    component: MatraconProgressBarField,
    displayName: "Matracon Progress Bar",
};

registry.category("fields").add("matracon_progressbar", matraconProgressBarField);
