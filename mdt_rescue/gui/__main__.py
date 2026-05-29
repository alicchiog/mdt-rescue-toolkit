"""
Entry point for ``python -m mdt_rescue.gui``.

Delegates to :func:`mdt_rescue.gui.app.main`, propagating its integer exit
code to the process via :class:`SystemExit`.
"""

from mdt_rescue.gui.app import main

if __name__ == "__main__":
    raise SystemExit(main())
