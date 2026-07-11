"""Backwards-compatible launcher.

The canonical entry point is ``grokcli2api.__main__:main`` (or the
``grokcli2api`` script installed by ``pip``). This file keeps the original
``python main.py`` invocation working.
"""

from grokcli2api.__main__ import main

if __name__ == "__main__":
    main()
