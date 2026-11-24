/** @odoo-module **/
/**
 * matracon_multi_file — multi-file upload field that looks and works like
 * Odoo's single-file "binary" widget, once per file:
 *
 *   [ file name          ]  ⬇ Download   ✎ Edit (replace)   🗑 Clear
 *   [ other file name    ]  ⬇ Download   ✎ Edit (replace)   🗑 Clear
 *   ( Upload your file )   ← always available to add more files
 *
 * Used on Many2many(ir.attachment) fields. Replacing or clearing a file
 * removes it from the record; the server (matracon.multi.attachment.mixin)
 * then deletes the old file, exactly like clearing the old binary field.
 */
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { FileInput } from "@web/core/file_input/file_input";
import {
    Many2ManyBinaryField,
    many2ManyBinaryField,
} from "@web/views/fields/many2many_binary/many2many_binary_field";

export class MatraconMultiFileField extends Many2ManyBinaryField {
    static template = "purchase_demand_raise.MatraconMultiFileField";
    static components = { FileInput };

    get canAddMore() {
        return !this.props.numberOfFiles || this.files.length < this.props.numberOfFiles;
    }

    /**
     * Files are uploaded to a neutral holding model (res_id 0) instead of the
     * record itself, so other modules' upload hooks do not run — e.g.
     * Accounting tries to import any file uploaded on a vendor bill and fails
     * on unsaved bills. The server links the files to the record on save.
     */
    get uploadResModel() {
        return "matracon.multi.attachment.mixin";
    }

    get uploadResId() {
        return 0;
    }

    async onFileReplaced(oldId, files) {
        const file = files && files[0];
        if (!file) {
            return;
        }
        if (file.error) {
            return this.notification.add(file.error, {
                title: _t("Uploading error"),
                type: "danger",
            });
        }
        const list = this.props.record.data[this.props.name];
        await list.addAndRemove({ add: [file.id], remove: [oldId] });
    }
}

export const matraconMultiFileField = {
    ...many2ManyBinaryField,
    component: MatraconMultiFileField,
};

registry.category("fields").add("matracon_multi_file", matraconMultiFileField);
