"""Constants shared by the API and the Worker.

The constants here intentionally avoid depending on database access so
they can be reused from migrations, fixtures, and unit tests.
"""

INBOX_CATEGORY_SLUG = "inbox"
INBOX_CATEGORY_NAME = "待整理"
