/** @odoo-module **/
/**
 * Duplicate check: before a NEW record is saved, ask the server whether it
 * looks like an existing one; if so show Open Existing / Create Anyway / Cancel.
 * Only main form records are checked (not lines), and only for models with
 * active rules. A failing check never blocks saving.
 */
import { Component } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { _t } from "@web/core/l10n/translation";
import { Record } from "@web/model/relational_model/record";
import { patch } from "@web/core/utils/patch";

export class DuplicateDialog extends Component {
    static template = "matracon_duplicate_check.DuplicateDialog";
    static components = { Dialog };
    static props = { title: String, matches: Array, onChoose: Function, close: Function };

    choose(choice, resId) {
        this.props.onChoose(choice, resId);
        this.props.close();
    }
}

let checkedModelsPromise = null;
function checkedModels(orm) {
    if (!checkedModelsPromise) {
        checkedModelsPromise = orm
            .call("x.duplicate.check", "get_checked_models", [])
            .then((models) => new Set(models))
            .catch(() => new Set());
    }
    return checkedModelsPromise;
}

patch(Record.prototype, {
    async save(options) {
        const model = this.model;
        const isMainNew = this.isNew && this === model.root && !this._mtrDupConfirmed;
        let forced = null;
        if (isMainNew && model.orm && model.dialog) {
            try {
                const models = await checkedModels(model.orm);
                if (models.has(this.resModel)) {
                    await model._askChanges();
                    const values = await this.getChanges();
                    const matches = await model.orm.call(
                        "x.duplicate.check", "check_values", [this.resModel, values],
                        { context: this.context || {} }
                    );
                    if (matches && matches.length) {
                        const [choice, resId] = await new Promise((resolve) => {
                            model.dialog.add(
                                DuplicateDialog,
                                {
                                    title: _t("Possible duplicate"),
                                    matches,
                                    onChoose: (c, id) => resolve([c, id]),
                                },
                                { onClose: () => resolve(["cancel"]) }
                            );
                        });
                        if (choice === "cancel") {
                            return false;
                        }
                        if (choice === "open") {
                            // Drop the unsaved new record first, otherwise leaving the
                            // form would try to save it again (and re-run this check).
                            await this.discard();
                            await model.action.doAction({
                                type: "ir.actions.act_window",
                                res_model: this.resModel,
                                res_id: resId,
                                views: [[false, "form"]],
                                target: "current",
                            });
                            return false;
                        }
                        this._mtrDupConfirmed = true;
                        forced = matches.flatMap((m) => m.records.map((r) => r.id));
                    }
                }
            } catch {
                // never block saving because of the check itself
            }
        }
        const result = await super.save(...arguments);
        if (forced && result && this.resId) {
            model.orm
                .call("x.duplicate.check", "log_forced", [this.resModel, this.resId, forced])
                .catch(() => {});
        }
        return result;
    },
});
