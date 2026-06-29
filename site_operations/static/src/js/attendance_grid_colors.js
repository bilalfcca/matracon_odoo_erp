/** @odoo-module **/

import { ListRenderer } from "@web/views/list/list_renderer";
import { patch } from "@web/core/utils/patch";

const ATTENDANCE_CODE_CLASS = {
    P: "matracon-att-p",
    A: "matracon-att-a",
    L: "matracon-att-l",
    H: "matracon-att-h",
};

patch(ListRenderer.prototype, {
    getCellClass(column, record) {
        const classNames = super.getCellClass(column, record);
        const fieldName = column.name;
        if (!fieldName || !fieldName.startsWith("day_")) {
            return classNames;
        }
        const code = record.data[fieldName];
        const colorClass = ATTENDANCE_CODE_CLASS[code];
        return colorClass ? `${classNames} ${colorClass}` : classNames;
    },
});
