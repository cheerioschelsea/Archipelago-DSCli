from __future__ import annotations

import sys
import ModuleUpdate
ModuleUpdate.update()

from worlds._retroarch.context import launch

if __name__ == "__main__":
    launch(*sys.argv[1:])
