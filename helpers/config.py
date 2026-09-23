"""Module for general configurations of the process"""

MAX_RETRY = 10

# ----------------------
# Queue population settings
# ----------------------
MAX_CONCURRENCY = 100  # tune based on backend capacity
MAX_RETRIES = 3  # transient failure retries per item
RETRY_BASE_DELAY = 0.5  # seconds (exponential backoff)


# --- Conversion window -----------------------------------------------------
#
# Rows in BefordringsData whose validity period overlaps [today, window end]
# are converted into the student's ONE active bevilling. See _bucket_key in
# processes/queue_handler.py for the full rule.
#
# Left as None the window ends one month from the day the queue phase runs.
# Pin it to a date (e.g. date(2026, 10, 21)) to make the grouping reproducible
# across runs — worth doing once a conversion date is agreed, so a re-queue
# after a failure buckets the rows exactly as the first attempt did.
CONVERSION_WINDOW_END = None


# --- Resolved-address cache ---------------------------------------------
#
# Address resolution is the slow part of the queue phase: one or more API
# calls per distinct address, against ~3700 rows of which the overwhelming
# majority resolve first time and never change. Re-running to look at a
# handful of failures should not mean paying for all of them again.
#
# Successes only. A failure is never cached, so every re-run retries exactly
# the addresses still being worked on — which is the point.
#
# Written as it goes, so a run that dies half way keeps what it had.
#
# DELETE THE FILE after changing the matching rules. A cached hit skips the
# matcher entirely, so an entry written under the old rules would survive a
# change meant to correct it. Set to None to switch the cache off.
RESOLVED_ADDRESS_CACHE = "resolved_addresses.csv"
