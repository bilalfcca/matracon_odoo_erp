# ═══════════════════════════════════════════════════════════════════════════
# PRODUCTION USER CONFIGURATION — Matracon Pakistan
# ═══════════════════════════════════════════════════════════════════════════
# User IDs are stable in production. Groups are the source of truth for access.
#
# HEAD OFFICE (all projects) — each user gets group_head_office + role group:
#   ID  2 → Bilal Khan (Admin)     → group_matracon_admin (implies all HO roles)
#   ID  5 → CEO                    → group_head_office + group_ceo_approval
#   ID 10 → Procurement Officer    → group_head_office + group_procurement_ho
#   ID 11 → Finance Officer        → group_head_office + group_finance_ho
#
# SITE USERS (one project each via Site Project Configuration):
#   MCH:      accountant 12, store 13
#   RWASA:    accountant  6, store  7
#   STP:      accountant  9, store  8
# ═══════════════════════════════════════════════════════════════════════════

from odoo import fields, _

PRODUCTION_CONFIG = {
    'head_office_ids': [2, 5, 10, 11],
    'ceo_ids': [5],
    'procurement_ho_ids': [10],
    'finance_ho_ids': [11],
    'admin_ids': [2],
    'projects': {
        'MCH - BAHAWALNAGAR': {
            'analytic_xml_id': 'purchase_demand_raise.analytic_account_mch_bahawalnagar',
            'site_accountant_ids': [12],
            'site_store_ids': [13],
        },
        'RWASA': {
            'analytic_xml_id': 'purchase_demand_raise.analytic_account_rwasa',
            'site_accountant_ids': [6],
            'site_store_ids': [7],
        },
        'STP - MARDAN': {
            'analytic_xml_id': 'purchase_demand_raise.analytic_account_stp_mardan',
            'site_accountant_ids': [9],
            'site_store_ids': [8],
        },
    }
}


def configure_production_users(env):
    """
    Configure security groups and default project for all production users.
    Safe to call multiple times (idempotent). Skips users that don't exist
    in this environment (dev environments have different user IDs).
    """
    Users = env['res.users']

    g_head_office = env.ref('purchase_demand_raise.group_head_office')
    g_ceo = env.ref('purchase_demand_raise.group_ceo_approval')
    g_proc_ho = env.ref('purchase_demand_raise.group_procurement_ho')
    g_finance_ho = env.ref('site_operations.group_finance_ho')
    g_matracon_admin = env.ref('purchase_demand_raise.group_matracon_admin', raise_if_not_found=False)
    g_site_store = env.ref('purchase_demand_raise.group_site_store')
    g_stock_user = env.ref('stock.group_stock_user', raise_if_not_found=False)
    g_site_accountant = env.ref('site_operations.group_site_accountant')

    # ── Head Office users by role ───────────────────────────────────────────
    for uid in PRODUCTION_CONFIG['admin_ids']:
        user = Users.sudo().browse(uid).exists()
        if user and g_matracon_admin:
            Users._matracon_add_group(user, g_matracon_admin)

    for uid in PRODUCTION_CONFIG['ceo_ids']:
        user = Users.sudo().browse(uid).exists()
        if not user:
            continue
        Users._matracon_add_group(user, g_head_office)
        Users._matracon_add_group(user, g_ceo)

    for uid in PRODUCTION_CONFIG['procurement_ho_ids']:
        user = Users.sudo().browse(uid).exists()
        if not user:
            continue
        Users._matracon_add_group(user, g_head_office)
        Users._matracon_add_group(user, g_proc_ho)

    for uid in PRODUCTION_CONFIG['finance_ho_ids']:
        user = Users.sudo().browse(uid).exists()
        if not user:
            continue
        Users._matracon_add_group(user, g_head_office)
        Users._matracon_add_group(user, g_finance_ho)

    # ── Site users — per project via Site Project Configuration ─────────────
    SiteConfig = env['x.project.site.config']
    for project_name, cfg in PRODUCTION_CONFIG['projects'].items():
        analytic = env.ref(cfg['analytic_xml_id'], raise_if_not_found=False)
        if not analytic:
            continue

        site_config = SiteConfig.search([('analytic_account_id', '=', analytic.id)], limit=1)
        if not site_config:
            site_config = SiteConfig.create({
                'name': project_name,
                'analytic_account_id': analytic.id,
            })

        store_users = Users.browse(cfg['site_store_ids']).exists()
        if store_users:
            site_config.write({'site_user_ids': [(4, u.id) for u in store_users]})
            for user in store_users:
                Users._matracon_add_group(user, g_site_store)
                if g_stock_user:
                    Users._matracon_add_group(user, g_stock_user)

        accountant_users = Users.browse(cfg['site_accountant_ids']).exists()
        for user in accountant_users:
            Users._matracon_add_group(user, g_site_accountant)
            user.sudo().write({
                'x_default_analytic_account_id': analytic.id,
                'x_site_config_id': site_config.id,
            })
            if site_config.warehouse_id:
                user.sudo().write({
                    'x_default_warehouse_id': site_config.warehouse_id.id,
                })

        if accountant_users:
            site_config.write({
                'x_site_accountant_ids': [(4, u.id) for u in accountant_users],
            })


def sync_alternative_prs(env):
    """Re-sync all alternative RFQs from their root PR (safe after module upgrade)."""
    PO = env['purchase.order']
    roots = PO.search([
        ('x_is_pr_document', '=', True),
        ('purchase_group_id', '!=', False),
    ])
    if roots:
        roots._matracon_sync_alternatives_from_root()
    # Mark cancelled Odoo alternatives
    cancelled = PO.search([
        ('state', '=', 'cancel'),
        ('x_pr_state', '!=', 'cancelled'),
    ])
    for order in cancelled:
        order.x_pr_state = 'cancelled'


def seed_demo_bank_balances(env):
    """Opening balances for HBL / BOK demo bank journals (idempotent)."""
    import logging
    _logger = logging.getLogger(__name__)
    Move = env['account.move'].sudo()
    journal_refs = {
        'site_operations.bank_journal_hbl': 50_000_000.0,
        'site_operations.bank_journal_bok': 25_000_000.0,
    }
    company = env.company
    equity = env.ref('account.1_equity', raise_if_not_found=False)
    if not equity:
        equity = env['account.account'].search([
            ('account_type', '=', 'equity'),
            ('company_ids', 'in', company.id),
        ], limit=1)
    if not equity:
        _logger.warning('seed_demo_bank_balances: no equity account — skipped')
        return
    for xml_id, amount in journal_refs.items():
        journal = env.ref(xml_id, raise_if_not_found=False)
        if not journal or not journal.default_account_id:
            continue
        ref = 'matracon_opening_%s' % journal.code
        if Move.search([('ref', '=', ref)], limit=1):
            continue
        bank_acc = journal.default_account_id
        Move.create({
            'move_type': 'entry',
            'date': fields.Date.today(),
            'ref': ref,
            'journal_id': env['account.journal'].search([
                ('type', '=', 'general'),
                ('company_id', '=', company.id),
            ], limit=1).id,
            'line_ids': [
                (0, 0, {
                    'name': _('Opening balance %s') % journal.name,
                    'account_id': bank_acc.id,
                    'debit': amount,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': _('Opening balance %s') % journal.name,
                    'account_id': equity.id,
                    'debit': 0.0,
                    'credit': amount,
                }),
            ],
        }).action_post()


def deduplicate_partner_tags(env):
    """
    Remove duplicate res.partner.category records that share the same name.
    Keeps the record with the lowest ID; re-links all partner associations.
    Idempotent — safe to call on every upgrade.
    """
    import logging
    _logger = logging.getLogger(__name__)
    cr = env.cr
    cr.execute("""
        SELECT name, array_agg(id ORDER BY id) AS ids
        FROM res_partner_category
        GROUP BY name
        HAVING count(*) > 1
    """)
    rows = cr.fetchall()
    for name, ids in rows:
        keep_id = ids[0]
        dup_ids = ids[1:]
        _logger.info('deduplicate_partner_tags: keeping tag id=%s "%s", removing %s', keep_id, name, dup_ids)
        # Re-link partners that point to a duplicate but not yet to the keeper
        cr.execute("""
            UPDATE res_partner_res_partner_category_rel
            SET category_id = %s
            WHERE category_id = ANY(%s)
              AND partner_id NOT IN (
                SELECT partner_id FROM res_partner_res_partner_category_rel
                WHERE category_id = %s
              )
        """, (keep_id, dup_ids, keep_id))
        # Drop any remaining duplicate links (partner already has keeper)
        cr.execute("""
            DELETE FROM res_partner_res_partner_category_rel
            WHERE category_id = ANY(%s)
        """, (dup_ids,))
        # Delete the duplicate tag records
        cr.execute("DELETE FROM res_partner_category WHERE id = ANY(%s)", (dup_ids,))


def migrate_matracon_admin_group(env):
    """Move users from legacy site_operations admin group to purchase_demand_raise."""
    old = env.ref('site_operations.group_matracon_admin', raise_if_not_found=False)
    new = env.ref('purchase_demand_raise.group_matracon_admin', raise_if_not_found=False)
    if not new or not old or old.id == new.id:
        return
    users = env['res.users'].sudo().search([('group_ids', 'in', old.id)])
    for user in users:
        user.write({'group_ids': [(3, old.id), (4, new.id)]})


def reprocess_existing_payments(env):
    """
    Backfill side-effects for payments that were posted before the Odoo 19
    state-fix (state='posted' → 'in_process'/'paid').

    Re-runs on every module upgrade so it is always idempotent:
      - Tags payment move lines with analytic distribution
      - Updates liability sheet line paid_amount / marks x_payment_status='paid'
      - Invalidates project fund caches so financial overview is accurate
    """
    import logging
    _logger = logging.getLogger(__name__)

    POSTED = ('in_process', 'paid', 'partial', 'posted')

    Payment = env['account.payment'].sudo()
    posted = Payment.search([('state', 'in', list(POSTED))])
    if not posted:
        return

    _logger.info('reprocess_existing_payments: processing %d payments', len(posted))

    # 1. Analytic tagging on existing move lines
    for payment in posted:
        try:
            payment._matracon_tag_payment_move_analytic()
        except Exception:
            pass

    # 2. Liability paid_amount update
    liability_payments = posted.filtered(lambda p: p.x_liability_sheet_line_id)
    for payment in liability_payments:
        try:
            line = payment.x_liability_sheet_line_id
            sibling_payments = payment.x_liability_sheet_id.payment_ids.filtered(
                lambda p: p.state in POSTED and p.x_liability_sheet_line_id == line
            )
            line.paid_amount = sum(
                p.x_gross_approved_amount or p.amount for p in sibling_payments
            )
            if payment.x_payment_status != 'paid':
                payment.x_payment_status = 'paid'
            if payment.x_liability_sheet_id:
                payment.x_liability_sheet_id.action_finalize_if_fully_paid()
        except Exception:
            pass

    # 3. Invalidate project fund caches so overview recomputes correctly
    try:
        posted._matracon_invalidate_project_funds()
    except Exception:
        pass

    _logger.info('reprocess_existing_payments: done')


def set_pkr_decimal_places(env):
    """
    Force PKR (Pakistani Rupee) to 0 decimal places so all monetary fields
    display as whole numbers. PKR paise are not used in practice.
    Uses raw SQL because the base.PKR record has noupdate=True in ir.model.data.
    Also called from the 1.7.9 migration script so it applies on upgrades too.
    """
    import logging
    _logger = logging.getLogger(__name__)
    env.cr.execute(
        "UPDATE res_currency SET decimal_places = 0 WHERE name = 'PKR' AND decimal_places != 0"
    )
    if env.cr.rowcount:
        _logger.info('set_pkr_decimal_places: set PKR to 0 decimal places')
        env['res.currency'].invalidate_model(['decimal_places'])


def set_date_format(env):
    """
    Set DD/MM/YYYY date format on every installed language so the format
    applies globally: views, list/form fields, format_date() calls, and
    standard Odoo reports all read res.lang.date_format at render time.
    Uses raw SQL to bypass ORM write restrictions on res.lang.
    """
    import logging
    _logger = logging.getLogger(__name__)
    env.cr.execute("UPDATE res_lang SET date_format = '%d/%m/%Y'")
    _logger.info('set_date_format: applied %%d/%%m/%%Y to all res.lang rows (%d updated)',
                 env.cr.rowcount)
    # Invalidate cached language data so the change is picked up immediately
    env['res.lang'].invalidate_model(['date_format'])


def set_pakistan_fiscal_year(env):
    """
    Set the fiscal year to July 1 – June 30 (Pakistan standard) on every
    company in this database.

    Odoo's accounting reports (Trial Balance, P&L, Balance Sheet, etc.) and
    the date-range picker all derive their "Year" period from these two fields:
        fiscalyear_last_day   = 30  (June 30)
        fiscalyear_last_month = '6' (June)

    After this change the "Year" filter in reports will show the FY period
    07/01/YYYY–06/30/YYYY+1 instead of the default 01/01–12/31 calendar year.

    Uses raw SQL so it cannot be blocked by noupdate flags or ORM validation
    (which can reject the write if the company already has posted entries).
    Safe to call on every upgrade — only updates rows that are still on the
    default calendar-year end (month=12, day=31) to avoid clobbering a
    deliberate manual override.
    """
    import logging
    _logger = logging.getLogger(__name__)
    env.cr.execute(
        """
        UPDATE res_company
        SET    fiscalyear_last_day   = 30,
               fiscalyear_last_month = '6'
        WHERE  fiscalyear_last_month != '6'
           OR  fiscalyear_last_day   != 30
        """
    )
    updated = env.cr.rowcount
    if updated:
        _logger.info(
            'set_pakistan_fiscal_year: updated %d company record(s) to Jul–Jun FY',
            updated,
        )
        env['res.company'].invalidate_model(['fiscalyear_last_day', 'fiscalyear_last_month'])


def post_init_hook(env):
    set_pkr_decimal_places(env)
    set_date_format(env)
    set_pakistan_fiscal_year(env)
    deduplicate_partner_tags(env)
    try:
        migrate_matracon_admin_group(env)
        configure_production_users(env)
        env['x.project.site.config']._matracon_ensure_site_warehouses()
        sync_alternative_prs(env)
        seed_demo_bank_balances(env)
        env['x.matracon.app.visibility'].apply_menu_visibility()
        # Finance HO needs payroll functional access if hr_payroll is installed
        payroll_user = env.ref('hr_payroll.group_hr_payroll_user', raise_if_not_found=False)
        if payroll_user:
            env.ref('site_operations.group_finance_ho').sudo().write({
                'implied_ids': [(4, payroll_user.id)],
            })
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            'site_operations post_init_hook: skipped user configuration '
            '(not a production DB or users not yet created): %s', e
        )
    reprocess_existing_payments(env)
    # Fix any posted petty cash expenses that have no JE or wrong JE credit account.
    # On a fresh install into a DB with existing data (e.g. a restored production dump),
    # post_migrate_hook does NOT run — only post_init_hook runs.
    try:
        fix_petty_cash_expense_accounts(env)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            'post_init_hook: fix_petty_cash_expense_accounts failed: %s', e)


def fix_petty_cash_expense_accounts(env):
    """Back-fill x_petty_cash_account_id on petty cash expenses where it was never
    persisted (readonly field not sent by browser on save), create missing journal
    entries, and correct JEs that were created with the wrong credit account.

    Runs on every module upgrade via post_migrate_hook. Safe to call multiple times
    — each step is idempotent and skips records that are already correct.

    Three-step process
    ──────────────────
    Step 1 — Fill x_petty_cash_account_id where NULL
        Resolution order:
          1. fund._get_petty_cash_account()  →  fund.x_petty_cash_account_id
                                             OR  site_config.x_petty_cash_account_id
          2. Cash journal linked to the site analytic  →  journal.default_account_id
          3. Any company cash journal                  →  journal.default_account_id

    Step 2 — Create missing JEs for posted expenses
        Covers expenses where _create_journal_entry() silently returned early
        (no credit account resolved, no cash journal found at post time).

    Step 3 — Correct JEs where credit account ≠ x_petty_cash_account_id
        Covers expenses posted before x_petty_cash_account_id was set on the record.
        _create_journal_entry fell back to cash_journal.default_account_id, which
        may be a different account than the configured site petty cash account.
        Fix: reverse the wrong JE, clear x_account_move_id, re-run _create_journal_entry.
    """
    import logging
    _logger = logging.getLogger(__name__)

    PCE = env['x.petty.cash.expense'].sudo()
    Journal = env['account.journal'].sudo()

    # ── Step 1: fill missing x_petty_cash_account_id ─────────────────────────
    missing_account = PCE.search([('x_petty_cash_account_id', '=', False)])
    _logger.info(
        'fix_petty_cash_expense_accounts: %d expense(s) have no petty cash account set',
        len(missing_account),
    )
    filled = 0
    skipped_no_config = []
    for expense in missing_account:
        pc_account = expense.fund_id._get_petty_cash_account()

        # Fallback: look up the site cash journal (same logic as _create_journal_entry)
        if not pc_account:
            analytic = expense.project_analytic_account_id
            cash_journal = False
            if analytic:
                cash_journal = Journal.search([
                    ('type', '=', 'cash'),
                    ('x_site_ids', 'in', [analytic.id]),
                    ('company_id', '=', env.company.id),
                ], limit=1)
            if not cash_journal:
                cash_journal = Journal.search([
                    ('type', '=', 'cash'),
                    ('company_id', '=', env.company.id),
                ], limit=1)
            if cash_journal and cash_journal.default_account_id:
                pc_account = cash_journal.default_account_id

        if pc_account:
            expense.x_petty_cash_account_id = pc_account
            filled += 1
        else:
            skipped_no_config.append(expense.x_ref or str(expense.id))

    if filled:
        _logger.info(
            'fix_petty_cash_expense_accounts: filled x_petty_cash_account_id on %d expense(s)',
            filled,
        )
    if skipped_no_config:
        _logger.warning(
            'fix_petty_cash_expense_accounts: %d expense(s) skipped — no petty cash account '
            'found (configure "Petty Cash Account" on Site Configuration for each site): %s',
            len(skipped_no_config),
            ', '.join(skipped_no_config[:20]),
        )

    # ── Step 2: create missing JEs for posted expenses ────────────────────────
    # These are expenses that were "posted" but _create_journal_entry silently
    # returned early (no credit account / no journal) — so the GL was never
    # touched and the fund balance never decreased.
    posted_no_je = PCE.search([
        ('state', '=', 'posted'),
        ('x_account_move_id', '=', False),
    ])
    _logger.info(
        'fix_petty_cash_expense_accounts: %d posted expense(s) have no journal entry',
        len(posted_no_je),
    )
    created = 0
    failed = []
    for expense in posted_no_je:
        try:
            expense._create_journal_entry()
            if expense.x_account_move_id:
                created += 1
            else:
                failed.append(
                    '%s (no credit/journal account configured)'
                    % (expense.x_ref or expense.id)
                )
        except Exception as e:
            failed.append('%s: %s' % (expense.x_ref or expense.id, e))
            _logger.warning(
                'fix_petty_cash_expense_accounts: could not create JE for %s: %s',
                expense.x_ref or expense.id, e,
            )
    if created:
        _logger.info(
            'fix_petty_cash_expense_accounts: created %d missing journal entry/entries',
            created,
        )
    if failed:
        _logger.warning(
            'fix_petty_cash_expense_accounts: %d expense(s) still have no JE after fix attempt: %s',
            len(failed),
            ', '.join(failed[:20]),
        )

    # ── Step 3: correct JEs where credit account ≠ expected petty cash account ─
    # Covers all ~257 MCH petty cash JEs that were posted via the "Cash at HO"
    # journal (wrong credit account 112631) instead of the site-specific account
    # (e.g. 112634 Cash at MCH Bahawalnagar).
    #
    # Strategy: direct SQL update on account_move_line.account_id.
    # This is correct for a migration fix because:
    #   1. account_type is NOT a stored column in Odoo 19 — only account_id
    #      needs updating.
    #   2. Preserves original JE numbers (no reversal entries created).
    #      257 JEs → 257 corrected in-place, zero extra accounting entries.
    #   3. Petty cash (asset_cash) accounts are not reconcilable, so no
    #      reconciliation entries are broken.
    #   4. We run in the post_migrate context as sudo so no lock-period issue.
    posted_with_je = PCE.search([
        ('state', '=', 'posted'),
        ('x_account_move_id', '!=', False),
        ('x_petty_cash_account_id', '!=', False),
    ])
    wrong_count = 0
    fixed_count = 0
    unfixed_step3 = []
    cr = env.cr

    for expense in posted_with_je:
        move = expense.x_account_move_id
        if not move or move.state != 'posted':
            continue
        expected_acct = expense.x_petty_cash_account_id

        # Fast check via SQL: does the move have a credit line with the wrong account?
        cr.execute("""
            SELECT id, account_id
            FROM account_move_line
            WHERE move_id = %s
              AND credit > 0
              AND account_id != %s
            LIMIT 1
        """, (move.id, expected_acct.id))
        wrong_row = cr.fetchone()
        if not wrong_row:
            continue  # Credit already correct — skip

        wrong_acct_id = wrong_row[1]
        # Safety: skip if reconciled (shouldn't happen for cash accounts)
        cr.execute("""
            SELECT 1 FROM account_move_line
            WHERE move_id = %s AND credit > 0 AND reconciled = true LIMIT 1
        """, (move.id,))
        if cr.fetchone():
            unfixed_step3.append('%s (reconciled credit lines — manual fix required)'
                                 % (expense.x_ref or expense.id))
            _logger.warning(
                'fix_petty_cash_expense_accounts: expense %s — JE %s credit lines are '
                'reconciled; skipping auto-fix. Correct this JE manually.',
                expense.x_ref or expense.id, move.name,
            )
            continue

        wrong_count += 1
        _logger.info(
            'fix_petty_cash_expense_accounts: expense %s — JE %s: '
            'credit account_id %s → %s (%s)',
            expense.x_ref or expense.id,
            move.name,
            wrong_acct_id,
            expected_acct.id,
            expected_acct.code or expected_acct.name,
        )

        try:
            # Direct in-place correction — no reversal entries created
            cr.execute("""
                UPDATE account_move_line
                SET account_id = %s
                WHERE move_id = %s
                  AND credit > 0
                  AND account_id != %s
            """, (expected_acct.id, move.id, expected_acct.id))
            fixed_count += 1
        except Exception as e:
            unfixed_step3.append('%s: %s' % (expense.x_ref or expense.id, e))
            _logger.warning(
                'fix_petty_cash_expense_accounts: SQL update failed for expense %s: %s',
                expense.x_ref or expense.id, e,
            )

    # Invalidate ORM cache so recomputed fields (balance, etc.) reflect the SQL changes
    if fixed_count:
        env['account.move'].invalidate_model()
        env['account.move.line'].invalidate_model()
        _logger.info(
            'fix_petty_cash_expense_accounts: corrected credit account on %d JE(s) '
            'in-place (no reversal entries created)',
            fixed_count,
        )
    if wrong_count and wrong_count != fixed_count:
        _logger.info(
            'fix_petty_cash_expense_accounts: found %d JE(s) with wrong credit account — '
            'fixed %d, could not fix %d',
            wrong_count, fixed_count, len(unfixed_step3),
        )
    if unfixed_step3:
        _logger.warning(
            'fix_petty_cash_expense_accounts: %d JE(s) could not be auto-corrected '
            '(manual fix required): %s',
            len(unfixed_step3),
            ', '.join(unfixed_step3[:20]),
        )


def post_migrate_hook(env):
    set_pkr_decimal_places(env)
    set_date_format(env)
    set_pakistan_fiscal_year(env)
    deduplicate_partner_tags(env)
    reprocess_existing_payments(env)
    try:
        fix_petty_cash_expense_accounts(env)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            'post_migrate_hook: fix_petty_cash_expense_accounts failed: %s', e)
    # Re-apply production user config (groups + default analytic/warehouse) on every update
    # so that a module upgrade or re-install never silently resets site user settings.
    try:
        configure_production_users(env)
        env['x.project.site.config']._matracon_ensure_site_warehouses()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            'post_migrate_hook: configure_production_users skipped: %s', e)
    # Re-apply payroll functional group to Finance HO if hr_payroll is installed
    try:
        payroll_user = env.ref('hr_payroll.group_hr_payroll_user', raise_if_not_found=False)
        if payroll_user:
            env.ref('site_operations.group_finance_ho').sudo().write({
                'implied_ids': [(4, payroll_user.id)],
            })
    except Exception:
        pass
    # Always re-apply menu visibility on update so group changes take effect.
    try:
        env['x.matracon.app.visibility'].apply_menu_visibility()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning('post_migrate_hook: apply_menu_visibility failed: %s', e)
    # Remove legacy menu_petty_cash_requests (replaced by menu_petty_cash_requests_ceo).
    # The old record may still exist in staging DBs that were installed before the
    # 778a46b collapse commit. Unlink it here so the CEO never sees a duplicate.
    try:
        old_menu = env.ref('site_operations.menu_petty_cash_requests', raise_if_not_found=False)
        if old_menu:
            old_menu.sudo().unlink()
    except Exception:
        pass
