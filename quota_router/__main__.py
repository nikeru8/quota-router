"""`python -m quota_router …`, and the file the installed hook runs: `python -I <this file> hook`.

Run as a plain file under -I, nothing puts the package on sys.path (no script
directory, no user site-packages), so the directory above this one is added here.
"""
import os
import sys

if not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quota_router.cli import main

if __name__ == "__main__":
    main()
