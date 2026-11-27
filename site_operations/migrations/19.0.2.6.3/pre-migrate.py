"""Create and fill attendance-line Unpaid Leave in SQL before the field is
loaded, so the upgrade does not compute it through the ORM for every
attendance line at once (memory limit on production-size databases)."""


def migrate(cr, version):
    cr.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'x_attendance_line'")
    if not cr.fetchone():
        return
    cr.execute("ALTER TABLE x_attendance_line ADD COLUMN IF NOT EXISTS unpaid_leave_days integer")
    days = ' + '.join(f"(CASE WHEN day_{d:02d} = 'U' THEN 1 ELSE 0 END)" for d in range(1, 32))
    cr.execute(f"UPDATE x_attendance_line SET unpaid_leave_days = {days} WHERE unpaid_leave_days IS NULL")
