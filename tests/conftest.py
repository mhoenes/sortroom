import os

# The built-in schedule would start real runs whenever a test starts the app.
os.environ.setdefault("SORTROOM_SCHEDULER", "off")
