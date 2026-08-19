"""The only file that spells this project's name.

Every other name in the tree is derived from these three constants: the XDG directories,
the environment namespace, the systemd unit, the page title. Renaming the project is
editing this file, and `spend doctor` prints what it resolved to, so a rename that
half-landed is visible rather than mysterious.
"""

NAME = "spend"           # also the command you type
SLUG = "spend"           # XDG directory name
ENV_PREFIX = "SPEND_"    # deliberately not the JOBTRACKER_ / EMAILTRACKER_ shape:
                         # this one is not a tracker, it advises too
TAGLINE = "Receipts in, spending out."
