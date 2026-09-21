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
