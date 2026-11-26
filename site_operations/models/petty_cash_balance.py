"""Petty cash running balances in date order (per project fund).

Balance Before / After on a petty cash expense used to be a snapshot of the
fund balance *at the moment of posting* - i.e. today's balance - so a
back-dated expense or adjustment showed today's figures and later rows were
never corrected.

Now they are derived from the fund's cash ledger in transaction-date order:

    Balance Before = fund balance from every posted entry dated before the
                     expense (same-day entries: in entry order)
    Balance After  = Balance Before + this expense's own cash movement

Any change on a past date (post / reset / cancel of an expense, release,
replenishment, opening balance or manual adjustment entry) recalculates that
fund's expenses from the affected date onward - other projects untouched.
Nothing in the accounting is changed; only the two display fields.

The ledger is the same one the fund's current balance uses
(x.petty.cash.fund._compute_balance): posted lines on the fund's petty cash
account for its project (entry header project or line analytic).
"""
import bisect

from odoo import api, fields, models, tools


class PettyCashFundBalance(models.Model):
    _inherit = 'x.petty.cash.fund'

    def _mtr_cash_account(self):
        self.ensure_one()
        acct = self.x_petty_cash_account_id
        if not acct and self.project_analytic_account_id:
            acct = self.env['x.project.site.config'].sudo().search(
                [('analytic_account_id', '=', self.project_analytic_account_id.id)], limit=1,
            ).x_petty_cash_account_id
        return acct

    def _mtr_ledger_events(self):
        """Chronological cash movements of this fund: [((date, move_id), amount)]."""
        self.ensure_one()
        acct, analytic = self._mtr_cash_account(), self.project_analytic_account_id
        if acct and analytic:
            self.env.cr.execute("""
                SELECT am.date, am.id, SUM(aml.balance)
                  FROM account_move_line aml
                  JOIN account_move am ON am.id = aml.move_id
                 WHERE aml.account_id = %s AND am.state = 'posted'
                   AND (am.x_project_analytic_account_id = %s OR aml.analytic_distribution ? %s)
              GROUP BY am.date, am.id
              ORDER BY am.date, am.id
            """, (acct.id, analytic.id, str(analytic.id)))
            return [((d, mid), amt) for d, mid, amt in self.env.cr.fetchall()]
        # No GL account: same fallback as the fund balance (released - expensed)
        events = []
        for req in self.request_ids.filtered(lambda r: r.state in ('released', 'confirmed')):
            move = req.payment_id.move_id
            events.append(((move.date or req.write_date.date(), move.id or 0), req.released_amount))
        for exp in self.expense_ids.filtered(lambda e: e.state == 'posted' and not e.x_advance_holder_id):
            events.append(((exp.expense_date, exp.x_account_move_id.id or 0), -exp.amount))
        return sorted(events)

    def _mtr_recompute_running_balances(self, date_from=None):
        """Recalculate Balance Before/After of posted expenses, in date order,
        for these funds; only expenses dated on/after ``date_from`` can change."""
        Expense = self.env['x.petty.cash.expense']
        self.env.flush_all()
        for fund in self.sudo():
            events = fund._mtr_ledger_events()
            keys, prefix, total = [], [0.0], 0.0
            amount_by_key = {}
            for key, amt in events:
                keys.append(key)
                total += amt
                prefix.append(total)
                amount_by_key[key] = amount_by_key.get(key, 0.0) + amt
            dom = [('fund_id', '=', fund.id), ('state', '=', 'posted')]
            if date_from:
                dom.append(('expense_date', '>=', date_from))
            updates = []
            for exp in Expense.sudo().search(dom):
                move = exp.x_account_move_id
                key = (move.date or exp.expense_date, move.id or 0)
                i = bisect.bisect_left(keys, key)
                before = prefix[i]
                after = before + amount_by_key.get(key, 0.0)
                before, after = round(before, 2), round(after, 2)
                if (round(exp.x_balance_before, 2), round(exp.x_balance_after, 2)) != (before, after):
                    updates.append((before, after, exp.id))
            if updates:
                self.env.cr.executemany(
                    "UPDATE x_petty_cash_expense SET x_balance_before = %s, x_balance_after = %s WHERE id = %s",
                    updates)
                Expense.invalidate_model(['x_balance_before', 'x_balance_after'])
        return True

    def action_recalculate_balances(self):
        self._mtr_recompute_running_balances()
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'type': 'success', 'message': self.env._('Running balances recalculated in date order.')}}

    @api.model
    def _mtr_funds_for_moves(self, moves):
        """{fund: earliest affected date} for entries touching a fund's petty cash account."""
        funds = self.sudo().search([('project_analytic_account_id', '!=', False)])
        by_account = {}
        for fund in funds:
            acct = fund._mtr_cash_account()
            if acct:
                by_account.setdefault(acct.id, []).append(fund)
        if not by_account:
            return {}
        result = {}
        for line in moves.sudo().line_ids:
            for fund in by_account.get(line.account_id.id, ()):
                pid = fund.project_analytic_account_id.id
                if line.move_id.x_project_analytic_account_id.id == pid or \
                        str(pid) in (line.analytic_distribution or {}):
                    d = line.move_id.date
                    result[fund] = min(result[fund], d) if fund in result else d
        return result


class PettyCashExpenseBalance(models.Model):
    _inherit = 'x.petty.cash.expense'

    def action_post(self):
        res = super().action_post()
        self._mtr_refresh_fund_balances()
        return res

    def action_reset_to_draft(self):
        dates = {exp.fund_id: exp.expense_date for exp in self if exp.fund_id}
        res = super().action_reset_to_draft()
        for fund, d in dates.items():
            fund._mtr_recompute_running_balances(d)
        return res

    def _mtr_refresh_fund_balances(self):
        earliest = {}
        for exp in self.filtered('fund_id'):
            d = exp.expense_date
            earliest[exp.fund_id] = min(earliest[exp.fund_id], d) if exp.fund_id in earliest else d
        for fund, d in earliest.items():
            fund._mtr_recompute_running_balances(d)


class AccountMovePettyCashBalance(models.Model):
    _inherit = 'account.move'

    def _mtr_petty_cash_balance_refresh(self):
        if self.env.context.get('mtr_skip_pc_balance'):
            return
        for fund, d in self.env['x.petty.cash.fund']._mtr_funds_for_moves(self).items():
            fund._mtr_recompute_running_balances(d)

    def _post(self, soft=True):
        posted = super()._post(soft=soft)
        posted._mtr_petty_cash_balance_refresh()
        return posted

    def button_draft(self):
        res = super().button_draft()
        self._mtr_petty_cash_balance_refresh()
        return res

    def button_cancel(self):
        res = super().button_cancel()
        self._mtr_petty_cash_balance_refresh()
        return res


class PettyCashDailyBalance(models.Model):
    """Day-by-day opening / cash in / cash out / closing per project fund,
    read live from posted entries (so each day's opening is always the
    previous day's closing, whatever date an adjustment was made on)."""
    _name = 'x.petty.cash.daily.balance'
    _description = 'Petty Cash Daily Balance'
    _auto = False
    _order = 'project_id, date desc'

    fund_id = fields.Many2one('x.petty.cash.fund', string='Fund', readonly=True)
    project_id = fields.Many2one('account.analytic.account', string='Project', readonly=True)
    date = fields.Date(readonly=True)
    currency_id = fields.Many2one('res.currency', readonly=True)
    opening_balance = fields.Monetary(string='Opening Balance', readonly=True, aggregator=None)
    cash_in = fields.Monetary(string='Cash In', readonly=True)
    cash_out = fields.Monetary(string='Cash Out', readonly=True)
    closing_balance = fields.Monetary(string='Closing Balance', readonly=True, aggregator=None)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""
            CREATE OR REPLACE VIEW x_petty_cash_daily_balance AS
            WITH f AS (
                SELECT fund.id AS fund_id, fund.project_analytic_account_id AS project_id,
                       fund.currency_id,
                       COALESCE(fund.x_petty_cash_account_id, (
                           SELECT c.x_petty_cash_account_id FROM x_project_site_config c
                            WHERE c.analytic_account_id = fund.project_analytic_account_id
                         ORDER BY c.id LIMIT 1)) AS account_id
                  FROM x_petty_cash_fund fund
                 WHERE fund.project_analytic_account_id IS NOT NULL
            ), d AS (
                SELECT f.fund_id, f.project_id, f.currency_id, am.date,
                       SUM(GREATEST(aml.balance, 0))  AS cash_in,
                       SUM(GREATEST(-aml.balance, 0)) AS cash_out,
                       SUM(aml.balance)               AS net
                  FROM f
                  JOIN account_move_line aml ON aml.account_id = f.account_id
                  JOIN account_move am ON am.id = aml.move_id AND am.state = 'posted'
                 WHERE am.x_project_analytic_account_id = f.project_id
                    OR aml.analytic_distribution ? f.project_id::text
              GROUP BY f.fund_id, f.project_id, f.currency_id, am.date
            )
            SELECT (d.fund_id * 100000 + (d.date - DATE '2000-01-01'))::integer AS id,
                   d.fund_id, d.project_id, d.currency_id, d.date,
                   SUM(d.net) OVER w - d.net AS opening_balance,
                   d.cash_in, d.cash_out,
                   SUM(d.net) OVER w AS closing_balance
              FROM d
            WINDOW w AS (PARTITION BY d.fund_id ORDER BY d.date ROWS UNBOUNDED PRECEDING)
        """)

    def action_open_entries(self):
        self.ensure_one()
        fund = self.fund_id.sudo()
        acct = fund._mtr_cash_account()
        pid = self.project_id.id
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('%(project)s — %(date)s', project=self.project_id.display_name, date=self.date),
            'res_model': 'account.move.line',
            'view_mode': 'list,form',
            'domain': [('account_id', '=', acct.id), ('date', '=', self.date), ('parent_state', '=', 'posted'),
                       '|', ('move_id.x_project_analytic_account_id', '=', pid),
                       ('analytic_distribution', 'in', [pid])],
        }
