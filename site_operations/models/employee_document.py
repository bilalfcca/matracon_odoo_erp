from odoo import models, fields, _


class EmployeeDocument(models.Model):
    _name = 'x.employee.document'
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
    file = fields.Binary(string='File', attachment=True, required=True)
    filename = fields.Char()
    issue_date = fields.Date(string='Issue Date')
    expiry_date = fields.Date(string='Expiry Date')
    notes = fields.Char(string='Notes')
