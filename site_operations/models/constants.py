# site_operations/models/constants.py
# Shared constants imported by multiple model files.
# Define here once; never duplicate in individual modules.

# A user is considered online while their last browser heartbeat is within this window.
# 600 s = 10 minutes — matches Odoo's own UPDATE_PRESENCE_DELAY + DISCONNECTION grace.
ONLINE_THRESHOLD_SECONDS = 600
