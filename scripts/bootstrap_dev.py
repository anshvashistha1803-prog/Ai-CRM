"""
CLI entrypoint to bootstrap initial development tenant and manager user.

Usage:
    python scripts/bootstrap_dev.py --email manager@havnrealty.com

Never runs in production environments. Never logs or prints plaintext passwords.
"""

import sys
from pathlib import Path

# Ensure backend directory is in sys.path
backend_dir = Path(__file__).resolve().parent.parent / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.commands.bootstrap import main

if __name__ == "__main__":
    main()
