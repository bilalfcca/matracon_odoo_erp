from odoo import models, fields, api


class PaymentProjectAllocation(models.Model):
    _name = 'x.payment.project.allocation'
    _description = 'Payment Fund Allocation by Project'

    payment_id = fields.Many2one(
        'account.payment', ondelete='cascade', required=True)

    project_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Source Project', required=True)

    project_id = fields.Many2one(
        'project.project',
        string='Source Project (App)',
        compute='_compute_project_id',
        store=False,
    )

    allocation_amount = fields.Monetary(
        string='Allocation Amount',
        currency_field='currency_id',
    )

    available_balance = fields.Monetary(
        string='Available Balance',
        compute='_compute_available_balance',
        currency_field='currency_id',
        store=False,
    )
    currency_id = fields.Many2one(
        related='payment_id.currency_id',
        string='Currency',
    )

    @api.depends('project_analytic_account_id')
    def _compute_project_id(self):
        Project = self.env['project.project']
        for alloc in self:
            if alloc.project_analytic_account_id:
                alloc.project_id = Project.search(
                    [('x_analytic_account_id', '=',
                      alloc.project_analytic_account_id.id)],
                    limit=1,
                )
            else:
                alloc.project_id = False

    @api.depends(
        'project_analytic_account_id',
        'payment_id.state',
        'payment_id.payment_type',
    )
    def _compute_available_balance(self):
        Project = self.env['project.project']
        for alloc in self:
            alloc.available_balance = Project.get_available_balance_for_analytic(
                alloc.project_analytic_account_id)
