from odoo import api, models, _
from odoo.exceptions import AccessError


class HrDepartment(models.Model):
    _inherit = 'hr.department'

    @api.model_create_multi
    def create(self, vals_list):
        # Site Accountants cannot create new Departments.
        # HR Managers and Admins retain their permissions.
        if (
            self.env.user.has_group(
                'site_operations.group_site_accountant'
            )
            and not self.env.user.has_group('hr.group_hr_manager')
        ):
            raise AccessError(
                _("You are not allowed to create new Departments. "
                  "Please contact the Administrator.")
            )

        return super().create(vals_list)


class HrJob(models.Model):
    _inherit = 'hr.job'

    @api.model_create_multi
    def create(self, vals_list):
        # Site Accountants cannot create new Job Positions.
        # HR Managers and Admins retain their permissions.
        if (
            self.env.user.has_group(
                'site_operations.group_site_accountant'
            )
            and not self.env.user.has_group('hr.group_hr_manager')
        ):
            raise AccessError(
                _("You are not allowed to create new Job Positions. "
                  "Please contact the Administrator.")
            )

        return super().create(vals_list)
