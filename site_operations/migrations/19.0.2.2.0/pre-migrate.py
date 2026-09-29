"""
Migration -> 19.0.2.2.0

Re-runs the 1.9.1 and 1.9.2 cleanups on databases where they never ran.

staging_for_test shipped site_operations as version 19.0.2.1.7 and was
merged into Production, so the production database (and every staging copy
of it) records site_operations as 19.0.2.1.7. After the branch reset the
code went back to 1.9.x, which Odoo treats as a downgrade, so the
migrations/1.9.1 and migrations/1.9.2 scripts were skipped. The stale
extension views (e.g. view_vendor_bill_site_accountant_simplify still
referencing x_je_allocation_ids, and the orphaned
account_counterpart_column view referencing x_counterpart_account_display)
then broke view validation on every staging/production build.

The version is now 19.0.2.2.0 so it is higher than anything a database has
recorded, and this script runs both cleanups. Both are idempotent.
"""
import importlib.util
import os

_MIGRATIONS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(version):
    path = os.path.join(_MIGRATIONS_DIR, version, 'pre-migrate.py')
    spec = importlib.util.spec_from_file_location(
        f'site_operations_pre_migrate_{version.replace(".", "_")}', path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def migrate(cr, version):
    # Reset site_operations extension views to <data/> before they are reloaded
    _load('1.9.1').migrate(cr, version)
    # Remove account_counterpart_column residue
    _load('1.9.2').migrate(cr, version)
