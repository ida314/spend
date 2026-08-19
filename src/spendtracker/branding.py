"""The only file that spells this project's name.

Every other name in the tree is derived from these three constants: the XDG directories,
the environment namespace, the systemd unit, the page title. Renaming the project is
editing this file, and `spend-tracker doctor` prints what it resolved to, so a rename that
half-landed is visible rather than mysterious.
"""

NAME = "spend-tracker"
SLUG = "spend-tracker"          # XDG directory name
ENV_PREFIX = "SPENDTRACKER_"    # mirrors JOBTRACKER_ / EMAILTRACKER_ on this box
TAGLINE = "Receipts in, spending out."
