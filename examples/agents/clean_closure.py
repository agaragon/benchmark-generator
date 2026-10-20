"""Simulated 'clean' agent for the bankapp.account-closure demo benchmark: writes a tidy implementation."""
import sys
from pathlib import Path

ws = Path(sys.argv[1])
account = ws / "bankapp/domain/account.py"
text = account.read_text()
text = text.replace("from dataclasses import dataclass\n", "from dataclasses import dataclass\nfrom datetime import datetime\n")
text += '''    closed_at: datetime | None = None

    @property
    def closed(self) -> bool:
        return self.status == "closed"

    def close(self, when: datetime) -> None:
        if self.closed:
            raise DomainError("account already closed")
        if self.balance < 0:
            raise DomainError("negative balance")
        self.status = "closed"
        self.closed_at = when
'''
account.write_text(text)
(ws / "bankapp/application/closure.py").write_text('''from datetime import datetime

from bankapp.application.security import verify_password
from bankapp.domain.account import DomainError


def _authenticate(repo, email, password):
    account = repo.get(email)
    if account is None:
        raise DomainError("account not found")
    if not verify_password(password, account.password_hash):
        raise DomainError("invalid credentials")
    return account


def _transfer_balance(repo, account, transfer_to):
    if account.balance <= 0:
        return
    if not transfer_to:
        raise DomainError("balance must be transferred")
    target = repo.get(transfer_to)
    if target is None or target.status != "active" or target.email == account.email:
        raise DomainError("invalid transfer target")
    target.balance += account.balance
    account.balance = 0
    repo.save(target)


def close_account(repo, email, password, transfer_to=None, now=None):
    account = _authenticate(repo, email, password)
    if account.closed:
        raise DomainError("account already closed")
    _transfer_balance(repo, account, transfer_to)
    account.close(now or datetime.now())
    return repo.save(account)
''')
