/** @odoo-module */

/**
 * Real-time thousand-separator formatting for all Odoo numeric inputs.
 *
 * Odoo already adds commas/thousand-separators on blur (via formatFloat /
 * formatMonetary / formatInteger). This module adds them *while the user is
 * still typing* so the field always looks like "1,000" even mid-entry.
 *
 * Strategy
 * --------
 * We attach a single capture-phase "input" listener on `document`. Capture
 * phase means we run BEFORE any OWL / Odoo handlers, so we can rewrite
 * `input.value` before Odoo's dirty-tracking code sees it.  Odoo's parser
 * (parseFloat / parseMonetary / parseInteger) already strips the thousand
 * separator on blur, so the formatted intermediate value never breaks parsing.
 *
 * Cursor correction
 * -----------------
 * When we insert commas, characters shift right and the browser would leave
 * the cursor in the wrong place.  We correct this by counting how many
 * *non-separator* characters precede the cursor in the raw value, then finding
 * the position with that same count in the formatted value.  This handles
 * mid-string insertions, deletions, decimal points, and leading minus signs.
 */

import { localization } from "@web/core/l10n/localization";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Return true when `el` is an Odoo editable numeric input that should receive
 * real-time thousand-separator formatting.
 *
 * Detection rules (Odoo 19 field templates):
 *   • FloatField        → class="o_input", inputmode="decimal"
 *   • IntegerField      → class="o_input", inputmode="numeric"
 *   • MonetaryField     → class="o_input flex-grow-1 …", NO inputmode attribute,
 *                         but lives inside a wrapper div.o_field_monetary
 *
 * We gate on `o_input` first (all Odoo field inputs carry this class) so we
 * don't accidentally format text / date / char inputs that happen to share
 * the same numeric inputmode.  The closest() checks on the wrapper div cover
 * MonetaryField and act as a safe fallback for the others.
 */
function isOdooNumericInput(el) {
    if (!(el instanceof HTMLInputElement)) return false;
    if (el.type === "number") return false; // skip native number spinners
    if (!el.classList.contains("o_input")) return false; // all Odoo field inputs have this

    const inputmode = el.getAttribute("inputmode");
    if (inputmode === "decimal") return true;  // FloatField
    if (inputmode === "numeric") return true;  // IntegerField

    // MonetaryField: no inputmode, but wrapped in .o_field_monetary
    if (el.closest(".o_field_monetary")) return true;
    // Safety net — float / integer wrappers (in case inputmode isn't set)
    if (el.closest(".o_field_float")) return true;
    if (el.closest(".o_field_integer")) return true;

    return false;
}

/**
 * Insert thousand separators into the integer part of `raw`.
 * The decimal part (everything from `decimalPoint` onwards) is left untouched.
 * Any existing separators are stripped first so they are never doubled.
 *
 * @param {string} raw           - current input string (may already have seps)
 * @param {string} thousandsSep  - e.g. ","
 * @param {string} decimalPoint  - e.g. "."
 * @returns {string} formatted value
 */
function applyThousandSeps(raw, thousandsSep, decimalPoint) {
    const isNeg = raw.startsWith("-");
    let body = isNeg ? raw.slice(1) : raw;

    // Strip existing thousand separators (positions may be stale)
    const sepEsc = thousandsSep.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    body = body.replace(new RegExp(sepEsc, "g"), "");

    // Split at the decimal point (first occurrence only)
    const dotIdx = body.indexOf(decimalPoint);
    const intPart = dotIdx >= 0 ? body.slice(0, dotIdx) : body;
    const decPart = dotIdx >= 0 ? body.slice(dotIdx) : "";

    // Add thousand separators every 3 digits from the right of the integer part
    const formattedInt =
        intPart.length > 3
            ? intPart.replace(/\B(?=(\d{3})+(?!\d))/g, thousandsSep)
            : intPart;

    return (isNeg ? "-" : "") + formattedInt + decPart;
}

/**
 * Compute the new cursor position in `formatted` that corresponds to
 * `oldCursorPos` in `raw`.
 *
 * We count the number of *non-separator* characters that appear before the
 * cursor in `raw`, then advance through `formatted` until we have skipped the
 * same count of non-separators.  This correctly handles:
 *   - digits being typed/deleted in the middle of the number
 *   - the cursor sitting right after the decimal point
 *   - leading minus sign
 */
function adjustCursor(raw, formatted, oldCursorPos, thousandsSep) {
    // Count non-separator characters before the old cursor in the raw value
    let nonSepBefore = 0;
    for (let i = 0; i < oldCursorPos && i < raw.length; i++) {
        if (raw[i] !== thousandsSep) nonSepBefore++;
    }

    // Walk formatted until we have consumed the same number of non-separators
    let counted = 0;
    for (let i = 0; i <= formatted.length; i++) {
        if (counted === nonSepBefore) return i;
        if (i < formatted.length && formatted[i] !== thousandsSep) counted++;
    }
    return formatted.length;
}

// ---------------------------------------------------------------------------
// Global capture-phase listener
// ---------------------------------------------------------------------------

document.addEventListener(
    "input",
    (ev) => {
        const input = ev.target;
        if (!isOdooNumericInput(input)) return;

        const sep = localization.thousandsSep;
        const dec = localization.decimalPoint || ".";

        // Skip locales that use whitespace as thousand separator (handled
        // differently) or when no separator is configured.
        if (!sep || !sep.trim()) return;

        const raw = input.value;
        if (!raw) return;

        // Leave math-expression shorthand ("=1+2") untouched
        if (raw.startsWith("=")) return;

        // Allow only: optional leading minus, digits, the configured separator,
        // and the decimal point.  Anything else (e.g. currency symbol typed by
        // mistake) means we leave the field alone.
        const sepEsc = sep.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        const decEsc = dec !== sep ? dec.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") : "";
        const allowed = new RegExp(`^-?[\\d${sepEsc}${decEsc}]*$`);
        if (!allowed.test(raw)) return;

        const cursorPos = input.selectionStart ?? raw.length;
        const formatted = applyThousandSeps(raw, sep, dec);

        // Nothing changed — avoid an infinite re-trigger
        if (formatted === raw) return;

        input.value = formatted;
        const newPos = adjustCursor(raw, formatted, cursorPos, sep);
        input.setSelectionRange(newPos, newPos);
    },
    true // ← capture phase: runs before Odoo's bubble-phase "input" handlers
);
