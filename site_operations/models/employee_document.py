from odoo import api, models, fields, _
from odoo.exceptions import ValidationError


class EmployeeDocument(models.Model):
    _name = 'x.employee.document'
    _inherit = ['matracon.multi.attachment.mixin']
    _description = 'Employee Document'
    _order = 'employee_id, document_type'

    employee_id = fields.Many2one('hr.employee', required=True, ondelete='cascade', index=True)
    document_type = fields.Selection([
        ('cnic', 'CNIC Copy'),
        ('appointment_letter', 'Appointment Letter'),
        ('contract', 'Employment Contract'),
        ('certificate', 'Academic Certificate'),
        ('experience', 'Experience Letter'),
        ('medical', 'Medical Certificate'),
        ('other', 'Other'),
    ], string='Document Type', required=True, default='other')
    name = fields.Char(string='Description')
    # Legacy single-file field — data moved to file_attachment_ids
    file = fields.Binary(string='File (Legacy)', attachment=True)
    filename = fields.Char()
    file_attachment_ids = fields.Many2many(
        'ir.attachment', 'x_employee_document_file_rel',
        'document_id', 'attachment_id',
        string='Files',
        help='One or more files for this document (e.g. CNIC front and back).')
    issue_date = fields.Date(string='Issue Date')
    expiry_date = fields.Date(string='Expiry Date')
    notes = fields.Char(string='Notes')

    @api.model_create_multi
    def create(self, vals_list):
        # A file was mandatory on every document before (required=True on the
        # old single-file field) — keep that: check on create even when the
        # files field was not sent at all.
        records = super().create(vals_list)
        records._check_file_attachment_ids()
        return records

    @api.constrains('file_attachment_ids')
    def _check_file_attachment_ids(self):
        for doc in self:
            if not doc.file_attachment_ids:
                raise ValidationError(_(
                    'Please attach at least one file for the employee document "%s".')
                    % (doc.name or dict(self._fields['document_type'].selection).get(doc.document_type, '')))
