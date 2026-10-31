#!/usr/local/bin/python3 -I
"""exec's command, installed as `/usr/local/bin/exec`: it puts exec's
package, kept outside `site-packages` so nothing else imports it, first on
the path, which `-I` leaves without this script's own folder, and runs it."""

import sys

sys.path.insert(0, "/usr/local/lib/unicon-exec")

from exec.main import main

sys.exit(main(sys.argv))
