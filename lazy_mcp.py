"""Entry point for ~/.claude.json. The proxy lives in lazy_core.py so its bytecode is cached (-2.7 MB per proxy)."""
import os
import sys

if __name__ == '__main__':
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import lazy_core
    lazy_core.main(sys.argv[1:])
