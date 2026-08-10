from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError


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

    x_wht_exemption_ids = fields.One2many(
        'x.partner.wht.exemption', 'partner_id',
        string='WHT Exemptions',
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
            elif ctx.get('third_party_create'):
                tag_name = '3rd Party'
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
    #        • res.partner.phone (mobile removed in Odoo 19)
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
            # OR across name, phone, email, and department match
            # Note: res.partner.mobile was removed in Odoo 19; use phone only.
            search_domain = base_domain + [
                '|', '|', '|',
                ('name', operator, name),
                ('phone', operator, name),
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
                phone = partner.phone or ''  # mobile removed in Odoo 19
                email = partner.email or ''
                extra = ', '.join(filter(None, [dept, phone, email]))
                partner.display_name = (
                    '%s (%s)' % (partner.name, extra) if extra else partner.name
                )
            else:
                partner.display_name = partner.name

    # ── Universal contact validation ─────────────────────────────────────────
    # Both name_create (dropdown quick-create) and @api.constrains enforce:
    #   • At least one Tag (category_id)
    #   • A non-blank Description (x_description)
    # Skipped for: sudo() callers (HR auto-creates employee contacts, mail
    # creates contacts from emails, install mode) and uid=1 (system admin).
    # This lets all programmatic / system partner creation pass unimpeded
    # while blocking every interactive business-user path.

    @api.model
    def name_create(self, name):
        """Block quick-create from Many2one dropdowns for all non-sudo users.

        When a user types a contact name in a dropdown and clicks "Create",
        Odoo calls name_create().  We raise a UserError so the user is forced
        to click "Create and edit…" and fill in the required Tag + Description.
        System / sudo callers (HR, mail, install) are allowed through.
        """
        if self.env.context.get('install_mode') or self.env.su:
            return super().name_create(name)
        raise UserError(_(
            'Contacts cannot be created directly from a dropdown.\n'
            'Please use "Create and edit…" to open the full form — '
            'a Tag and Description are required for every contact.'
        ))

    def open_follow_up_report(self):
        """Redirect the 'Due' smart button to Partner Ledger for pure vendors.

        The standard Follow-up Report (account_reports.action_account_report_followup)
        only queries account_type = 'asset_receivable'.  Vendors whose outstanding
        balance lives entirely in liability_payable entries always get 0 results.

        For contacts that are only suppliers (supplier_rank > 0, customer_rank == 0)
        we redirect to the Partner Ledger filtered to unreconciled entries, which
        correctly shows outstanding payable amounts.

        Contacts that are customers — or both customer and supplier — keep the
        standard Follow-up Report (receivable focus, follow-up workflow).
        """
        self.ensure_one()
        if self.supplier_rank > 0 and self.customer_rank == 0:
            action = self.env['ir.actions.actions']._for_xml_id(
                'account_reports.action_account_report_partner_ledger'
            )
            action['params'] = {
                'options': {
                    'partner_ids': (self | self.commercial_partner_id).ids,
                    'unfold_all': True,
                    'unreconciled': True,
                },
                'ignore_session': True,
            }
            return action
        return super().open_follow_up_report()

    @api.constrains('category_id', 'x_description', 'active', 'type')
    def _check_tag_and_description_required(self):
        """Every active standalone contact must have at least one Tag and a Description.

        Skipped for sudo() calls (HR, mail, module install) and for system admin
        (uid=1, env.su=True).  Only the interactive web-UI paths for regular
        business users are enforced — which is exactly what we want.
        """
        if self.env.context.get('install_mode') or self.env.su:
            return
        for partner in self.filtered(lambda p: p.active and p.type == 'contact'):
            if not partner.category_id:
                raise ValidationError(_(
                    'Contact "%s": at least one Tag is required.\n'
                    'Please choose a tag (e.g. "Local Supplier", "Subcontractor", '
                    '"3rd Party", "Vendor") before saving.'
                ) % (partner.name or '(new)'))
            if not (partner.x_description or '').strip():
                raise ValidationError(_(
                    'Contact "%s": Description is required.\n'
                    'Please fill in the Description field '
                    '(e.g. "Cement Supplier", "Labour Contractor") before saving.'
                ) % (partner.name or '(new)'))
