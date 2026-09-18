"""Run ``python -m aiodisklavier <host>`` to describe a piano for a bug report.

See :mod:`aiodisklavier._probe` for what it reads and, more to the point, what it leaves out.
"""

from __future__ import annotations

from ._probe import main

raise SystemExit(main())
