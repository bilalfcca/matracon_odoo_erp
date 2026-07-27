from odoo import models, fields, api, _
from odoo.exceptions import ValidationError


class ResPartnerSiteOps(models.Model):
    _inherit = 'res.partner'

    x_cnic = fields.Char(string='CNIC', tracking=True)

    x_description = fields.Char(
        string='Description',
        help='Brief description of this contact (e.g. "Cement Supplier", "Labour Contractor"). '
             'Appears on Liability Sheet lines as "Tag (Description)".'
    )

    x_is_project_entity = fields.Boolean(
        string='Internal Project Entity',
        default=False,
        help='Set on auto-generated partners that represent a project '
             'for inter-project accounting purposes. '
             'Hides them from vendor/customer selection lists.'
    )

    x_material_issuance_count = fields.Integer(
        string='Material Issuances',
        compute='_compute_x_material_issuance_count',
    )

    def _compute_x_material_issuance_count(self):
        StockPicking = self.env['stock.picking'].sudo()
        for partner in self:
            partner.x_material_issuance_count = StockPicking.search_count([
                ('x_contact_id', '=', partner.id),
                ('x_transfer_purpose', 'in', ['material_issuance', 'site_to_site']),
            ])

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        ctx = self.env.context
        # Auto-apply tag when partner is created from a contextual field.
        # The tag is also forced read-only in partner_views.xml so the user
        # cannot change it after the dialog opens.
        if 'category_id' in fields_list:
            tag_name = None
            if ctx.get('site_procurement_vendor'):
                tag_name = 'Local Supplier'
            elif ctx.get('subcontractor_create'):
                tag_name = 'Subcontractor'
            elif ctx.get('site_accountant_vendor'):
                tag_name = 'Vendor'
            if tag_name:
                tag = self.env['res.partner.category'].search(
                    [('name', '=', tag_name)], limit=1
                )
                if tag:
                    res['category_id'] = [(4, tag.id)]
        return res

    def action_view_material_issuances(self):
        self.ensure_one()
        return {
            'name': _('Issuances — %s') % self.name,
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'view_mode': 'list,form',
            'domain': [
                ('x_contact_id', '=', self.id),
                ('x_transfer_purpose', 'in', ['material_issuance', 'site_to_site']),
            ],
            'context': {},
        }

    # ── Employee contact search — activated by context employee_contact_search=True ──
    #
    # When this context flag is set (e.g. from the Material Issuance form's
    # x_contact_id field), two behaviours are enhanced:
    #
    #   1. _name_search  — typing in the dropdown also matches:
    #        • hr.employee.department_id.name  (e.g. "Civil", "Electrical")
    #        • res.partner.phone / mobile
    #        • res.partner.email
    #      so the user can type "Civil" and see all Civil-dept employees, or
    #      type a phone number to locate someone directly.
    #
    #   2. _compute_display_name — each dropdown row shows:
    #        Name (Department, Phone, Email)
    #      e.g. "Ali Raza (Civil, +92-300-1234567, ali@matracon.pk)"
    #      Fields that are blank are omitted; if nothing extra is available
    #      the plain name is shown.

    @api.model
    def _name_search(self, name='', domain=None, operator='ilike', limit=100, order=None):
        if self.env.context.get('employee_contact_search') and name:
            # Find partner IDs of employees whose department matches the query
            dept_partner_ids = (
                self.env['hr.employee']
                .search([('department_id.name', operator, name)])
                .mapped('work_contact_id')
                .ids
            )
            base_domain = list(domain or [])
            # OR across name, phone, mobile, email, and department match
            search_domain = base_domain + [
                '|', '|', '|', '|',
                ('name', operator, name),
                ('phone', operator, name),
                ('mobile', operator, name),
                ('email', operator, name),
                ('id', 'in', dept_partner_ids),
            ]
            return super()._name_search(
                name='', domain=search_domain, operator=operator,
                limit=limit, order=order,
            )
        return super()._name_search(
            name=name, domain=domain, operator=operator,
            limit=limit, order=order,
        )

    def _compute_display_name(self):
        if not self.env.context.get('employee_contact_search'):
            return super()._compute_display_name()
        # Build a department lookup for all employee partners in this batch
        emp_map = {}  # partner_id → hr.employee
        partner_ids = [p.id for p in self if p.employee]
        if partner_ids:
            for emp in self.env['hr.employee'].search(
                [('work_contact_id', 'in', partner_ids)]
            ):
                emp_map[emp.work_contact_id.id] = emp
        for partner in self:
            if partner.employee:
                emp = emp_map.get(partner.id)
                dept = emp.department_id.name if emp and emp.department_id else ''
                phone = partner.phone or partner.mobile or ''
                email = partner.email or ''
                extra = ', '.join(filter(None, [dept, phone, email]))
                partner.display_name = (
                    '%s (%s)' % (partner.name, extra) if extra else partner.name
                )
            else:
                partner.display_name = partner.name

    @api.constrains('category_id')
    def _check_category_required_for_site_store(self):
        """Site store users must always set at least one tag on partners."""
        if self.env.user.has_group('purchase_demand_raise.group_site_store'):
            for partner in self:
                if not partner.category_id:
                    raise ValidationError(_(
                        'Contact "%s": Category (Tag) is required. '
                        'Please add "Subcontractor" or another tag.'
                    ) % (partner.name or ''))

    @api.constrains('category_id', 'x_description')
    def _check_description_required_for_vendor(self):
        """Vendor / Subcontractor / Local Supplier partners must have a description
        so that liability sheet lines are auto-labelled meaningfully."""
        REQUIRED_TAGS = {'Vendor', 'Subcontractor', 'Local Supplier'}
        for partner in self:
            tag_names = set(partner.category_id.mapped('name'))
            if tag_names & REQUIRED_TAGS and not (partner.x_description or '').strip():
                raise ValidationError(_(
                    'Contact "%s": Description is required for Vendor / Subcontractor '
                    'partners (e.g. "Cement Supplier", "Labour Contractor"). '
                    'Please fill in the Description field before saving.'
                ) % (partner.name or ''))
