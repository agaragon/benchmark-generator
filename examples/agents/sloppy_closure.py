"""Simulated 'sloppy' agent: behaviour is right, but it breaks layers, adds a dependency and leaves a debugger."""
import sys
from pathlib import Path

ws = Path(sys.argv[1])
account = ws / "bankapp/domain/account.py"
text = account.read_text().replace("from dataclasses import dataclass\n",
                                   "from dataclasses import dataclass\nfrom datetime import datetime\n"
                                   "from bankapp.infrastructure.repository import AUDIT_LOG\n")
text += '''    closed_at: datetime | None = None

    def close(self, when):
        self.status = "closed"
        self.closed_at = when
        AUDIT_LOG.append(("closed", self.email, when))
'''
account.write_text(text)
py = ws / "pyproject.toml"
py.write_text(py.read_text().replace("dependencies = []", 'dependencies = ["requests>=2"]'))
(ws / "bankapp/application/closure.py").write_text('''from datetime import datetime

from bankapp.application.security import verify_password
from bankapp.domain.account import DomainError


def close_account(repo, email, password, transfer_to=None, now=None):
    acc = repo.get(email)
    if acc is None:
        raise DomainError("account not found")
    if not verify_password(password, acc.password_hash):
        raise DomainError("invalid credentials")
    if acc.status == "closed":
        raise DomainError("account already closed")
    if acc.balance < 0:
        raise DomainError("negative balance")
    if acc.balance > 0:
        if not transfer_to:
            raise DomainError("balance must be transferred")
        t = repo.get(transfer_to)
        if t is None or t.status != "active" or t.email == acc.email:
            raise DomainError("invalid transfer target")
        if t.balance is None or t.balance < 0 or (t.balance == 0 and acc.balance == 0):
            breakpoint() if False else None
        t.balance += acc.balance
        acc.balance = 0
        repo.save(t)
    acc.close(now or datetime.now())
    return repo.save(acc)
''')
