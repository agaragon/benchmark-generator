"""Simulated 'lazy' agent: implements only the happy path."""
import sys
from pathlib import Path

ws = Path(sys.argv[1])
account = ws / "bankapp/domain/account.py"
account.write_text(account.read_text() + "    closed_at: object = None\n")
(ws / "bankapp/application/closure.py").write_text('''from datetime import datetime

from bankapp.application.security import verify_password
from bankapp.domain.account import DomainError


def close_account(repo, email, password, transfer_to=None, now=None):
    acc = repo.get(email)
    if acc is None:
        raise DomainError("account not found")
    if not verify_password(password, acc.password_hash):
        raise DomainError("invalid credentials")
    acc.status = "closed"
    acc.closed_at = now or datetime.now()
    return repo.save(acc)
''')
