"""Put the scraper/ directory on sys.path so `import scraper` loads scraper/scraper.py.

The package dir and the module share the name "scraper"; inserting the module's own directory at
the front of sys.path makes the plain module win deterministically, regardless of pytest's cwd.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
