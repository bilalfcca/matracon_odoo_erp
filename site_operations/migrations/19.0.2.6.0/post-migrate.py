from odoo import api, SUPERUSER_ID

from odoo.addons.purchase_demand_raise.models.multi_attachment_mixin import (
    matracon_migrate_binary_to_attachments,
)

# (model, legacy binary field, legacy filename field, new multi-file field, default file name)
SPECS = [
    ('x.petty.cash.request', 'x_pm_signed_document', 'x_pm_signed_document_filename',
     'x_pm_signed_document_attachment_ids', 'pm_signed_document'),
    ('x.petty.cash.expense', 'x_signed_voucher', 'x_signed_voucher_filename',
     'x_signed_voucher_attachment_ids', 'signed_voucher'),
    ('x.petty.cash.expense', 'receipt', 'receipt_filename',
     'receipt_attachment_ids', 'receipt'),
    ('x.salary.sheet', 'signed_sheet', 'signed_sheet_filename',
     'signed_sheet_attachment_ids', 'signed_salary_sheet'),
    ('x.liability.sheet', 'pm_signed_sheet', 'pm_signed_sheet_filename',
     'pm_signed_sheet_attachment_ids', 'pm_signed_liability_sheet'),
    ('x.subcontractor.ipc', 'ipc_document', 'ipc_document_filename',
     'ipc_document_attachment_ids', 'ipc_document'),
    ('x.employee.document', 'file', 'filename',
     'file_attachment_ids', 'employee_document'),
]


def migrate(cr, version):
    """Move single-file uploads into the new multi-file fields."""
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    for spec in SPECS:
        matracon_migrate_binary_to_attachments(env, *spec)
