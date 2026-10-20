"""Create a small demo repository (the example from the article: login, sign up, account closure,
account reopening) with a realistic git history:

  * signup            merged feature branch, clean, tested
  * login             conventional commits, clean, tested
  * account-closure   chaotic: fix-on-fix, a revert, rewrites, layer violation, later hotfix
  * account-reopening clean, tested
  * statements        no tests

Usage:  python examples/make_demo_repo.py /tmp/bankapp
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

FILES_INITIAL = {
    "pyproject.toml": '[project]\nname = "bankapp"\nversion = "0.1.0"\ndependencies = []\n',
    "README.md": "# bankapp\n\nToy banking service used to demo featbench.\n",
    "bankapp/__init__.py": "",
    "bankapp/domain/__init__.py": "",
    "bankapp/domain/account.py": '''\
from dataclasses import dataclass


class DomainError(Exception):
    """Raised when a business rule is violated."""


@dataclass
class Account:
    email: str
    password_hash: str
    balance: int = 0
    status: str = "active"
''',
    "bankapp/application/__init__.py": "",
    "bankapp/application/security.py": '''\
import hashlib

_SALT = "featbench-demo"


def hash_password(password: str) -> str:
    return hashlib.sha256((_SALT + password).encode()).hexdigest()


def verify_password(password: str, password_hash: str) -> bool:
    return hash_password(password) == password_hash
''',
    "bankapp/infrastructure/__init__.py": "",
    "bankapp/infrastructure/repository.py": '''\
AUDIT_LOG: list = []


class InMemoryAccountRepository:
    def __init__(self):
        self._accounts = {}

    def get(self, email):
        return self._accounts.get(email.lower())

    def exists(self, email):
        return email.lower() in self._accounts

    def save(self, account):
        self._accounts[account.email.lower()] = account
        return account

    def all(self):
        return list(self._accounts.values())
''',
    "bankapp/api/__init__.py": "",
    "bankapp/api/handlers.py": '''\
"""HTTP-ish handlers (framework-free for the demo)."""

from bankapp.domain.account import DomainError


def handle(fn, *args, **kwargs):
    try:
        return 200, fn(*args, **kwargs)
    except DomainError as e:
        return 400, {"error": str(e)}
''',
    "tests/__init__.py": "",
    "tests/conftest.py": '''\
import pytest

from bankapp.infrastructure.repository import InMemoryAccountRepository


@pytest.fixture
def repo():
    return InMemoryAccountRepository()
''',
    "tests/test_smoke.py": '''\
from bankapp.domain.account import Account


def test_repository_roundtrip(repo):
    repo.save(Account("a@b.com", "x"))
    assert repo.get("A@B.com").email == "a@b.com"
''',
}

SIGNUP = '''\
import re

from bankapp.application.security import hash_password
from bankapp.domain.account import Account, DomainError

EMAIL_RE = re.compile(r"^[^@\\s]+@[^@\\s]+\\.[a-z]{2,}$", re.I)
MIN_PASSWORD = 8


def sign_up(repo, email: str, password: str) -> Account:
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise DomainError("invalid email")
    if len(password) < MIN_PASSWORD:
        raise DomainError("password too short")
    if repo.exists(email):
        raise DomainError("email already registered")
    return repo.save(Account(email, hash_password(password)))
'''

SIGNUP_TESTS = '''\
import pytest

from bankapp.application.signup import sign_up
from bankapp.domain.account import DomainError


def test_signup_creates_active_account(repo):
    acc = sign_up(repo, "joe@example.com", "s3cret-pass")
    assert acc.status == "active" and repo.exists("joe@example.com")


def test_signup_normalizes_email_to_lowercase(repo):
    assert sign_up(repo, " Joe@Example.COM ", "s3cret-pass").email == "joe@example.com"


def test_signup_rejects_invalid_email(repo):
    with pytest.raises(DomainError, match="invalid email"):
        sign_up(repo, "not-an-email", "s3cret-pass")


def test_signup_rejects_short_password(repo):
    with pytest.raises(DomainError, match="password too short"):
        sign_up(repo, "joe@example.com", "short")


def test_signup_rejects_duplicate_email(repo):
    sign_up(repo, "joe@example.com", "s3cret-pass")
    with pytest.raises(DomainError, match="already registered"):
        sign_up(repo, "JOE@example.com", "another-pass")
'''

ACCOUNT_LOGIN = '''\
from dataclasses import dataclass

MAX_FAILED_LOGINS = 3


class DomainError(Exception):
    """Raised when a business rule is violated."""


@dataclass
class Account:
    email: str
    password_hash: str
    balance: int = 0
    status: str = "active"
    failed_attempts: int = 0

    @property
    def locked(self) -> bool:
        return self.failed_attempts >= MAX_FAILED_LOGINS
'''

LOGIN = '''\
import secrets

from bankapp.application.security import verify_password
from bankapp.domain.account import DomainError


def login(repo, email: str, password: str) -> str:
    account = repo.get(email.strip())
    if account is None:
        raise DomainError("invalid credentials")
    if account.locked:
        raise DomainError("account locked")
    if not verify_password(password, account.password_hash):
        account.failed_attempts += 1
        repo.save(account)
        raise DomainError("invalid credentials")
    account.failed_attempts = 0
    repo.save(account)
    return secrets.token_hex(16)
'''

LOGIN_TESTS = '''\
import pytest

from bankapp.application.login import login
from bankapp.application.security import hash_password
from bankapp.domain.account import Account, DomainError


@pytest.fixture
def user(repo):
    return repo.save(Account("ann@example.com", hash_password("correct-horse")))


def test_login_returns_session_token(repo, user):
    token = login(repo, "ann@example.com", "correct-horse")
    assert isinstance(token, str) and len(token) == 32


def test_login_is_case_insensitive_on_email(repo, user):
    assert login(repo, "ANN@example.com", "correct-horse")


def test_login_rejects_unknown_email(repo, user):
    with pytest.raises(DomainError, match="invalid credentials"):
        login(repo, "bob@example.com", "correct-horse")


def test_login_rejects_wrong_password(repo, user):
    with pytest.raises(DomainError, match="invalid credentials"):
        login(repo, "ann@example.com", "wrong")


def test_account_locks_after_three_failed_attempts(repo, user):
    for _ in range(3):
        with pytest.raises(DomainError):
            login(repo, "ann@example.com", "wrong")
    with pytest.raises(DomainError, match="account locked"):
        login(repo, "ann@example.com", "correct-horse")


def test_successful_login_resets_failed_attempts(repo, user):
    with pytest.raises(DomainError):
        login(repo, "ann@example.com", "wrong")
    login(repo, "ann@example.com", "correct-horse")
    assert repo.get("ann@example.com").failed_attempts == 0
'''

ACCOUNT_CLOSURE = ACCOUNT_LOGIN.replace(
    "from dataclasses import dataclass\n",
    "from dataclasses import dataclass\nfrom datetime import datetime\n",
) + '''\
    closed_at: datetime | None = None

    def close(self, when):
        # quick hack: audit straight from the entity
        from bankapp.infrastructure.repository import AUDIT_LOG
        self.status = "closed"
        self.closed_at = when
        AUDIT_LOG.append(("closed", self.email, when))
'''

CLOSURE_V1 = '''\
from datetime import datetime

from bankapp.application.security import verify_password
from bankapp.domain.account import DomainError


def close_account(repo, email, password, transfer_to=None, now=None):
    acc = repo.get(email)
    if acc is None:
        raise DomainError("account not found")
    if not verify_password(password, acc.password_hash):
        raise DomainError("invalid credentials")
    acc.close(now or datetime.now())
    repo.save(acc)
    return acc
'''

CLOSURE_V2 = '''\
from datetime import datetime

from bankapp.application.security import verify_password
from bankapp.domain.account import DomainError


def close_account(repo, email, password, transfer_to=None, now=None):
    acc = repo.get(email)
    if acc is None:
        raise DomainError("account not found")
    else:
        if not verify_password(password, acc.password_hash):
            raise DomainError("invalid credentials")
        else:
            if acc.balance > 0:
                raise DomainError("balance must be transferred")
            acc.close(now or datetime.now())
            repo.save(acc)
            return acc
'''

CLOSURE_V3 = '''\
from datetime import datetime

from bankapp.application.security import verify_password
from bankapp.domain.account import DomainError


def close_account(repo, email, password, transfer_to=None, now=None):
    acc = repo.get(email)
    if acc is None:
        raise DomainError("account not found")
    else:
        if not verify_password(password, acc.password_hash):
            raise DomainError("invalid credentials")
        else:
            if acc.status == "closed":
                raise DomainError("account already closed")
            if acc.balance > 0:
                if transfer_to is None:
                    raise DomainError("balance must be transferred")
                else:
                    target = repo.get(transfer_to) if transfer_to else None
                    if target:
                        target.balance = target.balance + acc.balance
                        acc.balance = 0
                        repo.save(target)
            acc.close(now or datetime.now())
            repo.save(acc)
            return acc
'''

CLOSURE_FINAL = '''\
from datetime import datetime

from bankapp.application.security import verify_password
from bankapp.domain.account import DomainError


def close_account(repo, email, password, transfer_to=None, now=None):
    acc = repo.get(email)
    if acc is None:
        raise DomainError("account not found")
    else:
        if not verify_password(password, acc.password_hash):
            raise DomainError("invalid credentials")
        else:
            if acc.status == "closed" or acc.status == "CLOSED" or acc.closed_at is not None:
                raise DomainError("account already closed")
            else:
                if acc.balance > 0:
                    if transfer_to is None or transfer_to == "":
                        raise DomainError("balance must be transferred")
                    else:
                        target = repo.get(transfer_to) if transfer_to else None
                        if target is None or target.status != "active" or target.email == acc.email:
                            raise DomainError("invalid transfer target")
                        else:
                            if target.balance is None:
                                target.balance = 0
                            target.balance = target.balance + acc.balance
                            acc.balance = 0
                            repo.save(target)
                elif acc.balance < 0:
                    raise DomainError("negative balance")
                else:
                    pass
                for attempt in range(3):
                    try:
                        acc.close(now or datetime.now())
                        break
                    except Exception:
                        if attempt == 2:
                            raise
                repo.save(acc)
                return acc
'''

CLOSURE_TESTS = '''\
from datetime import datetime

import pytest

from bankapp.application.closure import close_account
from bankapp.application.security import hash_password
from bankapp.domain.account import Account, DomainError

NOW = datetime(2026, 1, 15, 12, 0)


@pytest.fixture
def ann(repo):
    return repo.save(Account("ann@example.com", hash_password("pw-ann-123")))


@pytest.fixture
def bob(repo):
    return repo.save(Account("bob@example.com", hash_password("pw-bob-123")))


def test_closes_account_with_zero_balance(repo, ann):
    acc = close_account(repo, "ann@example.com", "pw-ann-123", now=NOW)
    assert acc.status == "closed" and acc.closed_at == NOW


def test_rejects_wrong_password(repo, ann):
    with pytest.raises(DomainError, match="invalid credentials"):
        close_account(repo, "ann@example.com", "nope", now=NOW)


def test_rejects_unknown_account(repo):
    with pytest.raises(DomainError, match="account not found"):
        close_account(repo, "ghost@example.com", "x", now=NOW)


def test_rejects_already_closed_account(repo, ann):
    close_account(repo, "ann@example.com", "pw-ann-123", now=NOW)
    with pytest.raises(DomainError, match="already closed"):
        close_account(repo, "ann@example.com", "pw-ann-123", now=NOW)


def test_positive_balance_requires_transfer(repo, ann):
    ann.balance = 50
    with pytest.raises(DomainError, match="balance must be transferred"):
        close_account(repo, "ann@example.com", "pw-ann-123", now=NOW)


def test_transfers_balance_to_target_account(repo, ann, bob):
    ann.balance = 50
    close_account(repo, "ann@example.com", "pw-ann-123", transfer_to="bob@example.com", now=NOW)
    assert repo.get("bob@example.com").balance == 50
    assert repo.get("ann@example.com").balance == 0


def test_rejects_invalid_transfer_target(repo, ann):
    ann.balance = 50
    with pytest.raises(DomainError, match="invalid transfer target"):
        close_account(repo, "ann@example.com", "pw-ann-123", transfer_to="ghost@example.com", now=NOW)


def test_rejects_negative_balance(repo, ann):
    ann.balance = -10
    with pytest.raises(DomainError, match="negative balance"):
        close_account(repo, "ann@example.com", "pw-ann-123", now=NOW)
'''

REOPENING = '''\
from datetime import datetime, timedelta

from bankapp.application.security import verify_password
from bankapp.domain.account import DomainError

REOPEN_WINDOW = timedelta(days=30)


def reopen_account(repo, email: str, password: str, now: datetime | None = None):
    account = repo.get(email)
    if account is None:
        raise DomainError("account not found")
    if account.status != "closed":
        raise DomainError("account is not closed")
    if not verify_password(password, account.password_hash):
        raise DomainError("invalid credentials")
    if (now or datetime.now()) - account.closed_at > REOPEN_WINDOW:
        raise DomainError("reopening window expired")
    account.status = "active"
    account.closed_at = None
    account.failed_attempts = 0
    return repo.save(account)
'''

REOPENING_TESTS = '''\
from datetime import datetime, timedelta

import pytest

from bankapp.application.reopening import reopen_account
from bankapp.application.security import hash_password
from bankapp.domain.account import Account, DomainError

CLOSED_AT = datetime(2026, 1, 1, 9, 0)


@pytest.fixture
def closed(repo):
    acc = Account("ann@example.com", hash_password("pw-ann-123"), status="closed")
    acc.closed_at = CLOSED_AT
    return repo.save(acc)


def test_reopens_within_thirty_days(repo, closed):
    acc = reopen_account(repo, "ann@example.com", "pw-ann-123", now=CLOSED_AT + timedelta(days=29))
    assert acc.status == "active" and acc.closed_at is None


def test_rejects_after_thirty_days(repo, closed):
    with pytest.raises(DomainError, match="window expired"):
        reopen_account(repo, "ann@example.com", "pw-ann-123", now=CLOSED_AT + timedelta(days=31))


def test_rejects_account_that_is_not_closed(repo):
    repo.save(Account("bob@example.com", hash_password("pw-bob-123")))
    with pytest.raises(DomainError, match="not closed"):
        reopen_account(repo, "bob@example.com", "pw-bob-123")


def test_rejects_wrong_password(repo, closed):
    with pytest.raises(DomainError, match="invalid credentials"):
        reopen_account(repo, "ann@example.com", "nope", now=CLOSED_AT)


def test_reopening_resets_failed_login_attempts(repo, closed):
    closed.failed_attempts = 3
    acc = reopen_account(repo, "ann@example.com", "pw-ann-123", now=CLOSED_AT)
    assert acc.failed_attempts == 0
'''

STATEMENTS = '''\
import csv
import io


def export_statement(account, transactions) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["date", "description", "amount"])
    for t in transactions:
        writer.writerow([t["date"], t["description"], t["amount"]])
    writer.writerow(["", "balance", account.balance])
    return out.getvalue()
'''


class Repo:
    def __init__(self, path: Path):
        self.path = path
        self.tick = 0

    def git(self, *args: str) -> None:
        self.tick += 1
        date = f"2026-01-{1 + self.tick // 4:02d}T{9 + self.tick % 4:02d}:00:00"
        env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date,
               "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
               "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com"}
        subprocess.run(["git", "-C", str(self.path), *args], check=True, env=env, capture_output=True)

    def commit(self, message: str, files: dict[str, str | None]) -> None:
        for rel, content in files.items():
            p = self.path / rel
            if content is None:
                p.unlink()
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)


def build(dest: Path) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(dest)], check=True)
    r = Repo(dest)
    r.commit("chore: project skeleton", FILES_INITIAL)

    # signup: a merged feature branch (clean)
    r.git("checkout", "-q", "-b", "feature/signup")
    r.commit("add sign up use case", {"bankapp/application/signup.py": SIGNUP})
    r.commit("add sign up tests", {"tests/test_signup.py": SIGNUP_TESTS})
    r.git("checkout", "-q", "main")
    r.git("merge", "-q", "--no-ff", "feature/signup", "-m", "Merge branch 'feature/signup'")

    # login: conventional commits (clean)
    r.commit("feat(login): authenticate with email and password, lock after 3 failures",
             {"bankapp/domain/account.py": ACCOUNT_LOGIN, "bankapp/application/login.py": LOGIN})
    r.commit("test(login): cover lockout and reset rules", {"tests/test_login.py": LOGIN_TESTS})

    # account closure: chaotic
    r.commit("feat(account-closure): close accounts",
             {"bankapp/domain/account.py": ACCOUNT_CLOSURE, "bankapp/application/closure.py": CLOSURE_V1})
    r.commit("fix(account-closure): forgot the balance check", {"bankapp/application/closure.py": CLOSURE_V2})
    r.commit("fix(account-closure): crash when transfer target is missing",
             {"bankapp/application/closure.py": CLOSURE_V3})
    r.commit("docs: describe the API", {"README.md": FILES_INITIAL["README.md"] + "\n## API\n\nSee handlers.\n"})
    r.commit("revert(account-closure): undo transfer fallback", {"bankapp/application/closure.py": CLOSURE_V2})
    r.commit("fix(account-closure): re-apply transfer logic properly",
             {"bankapp/application/closure.py": CLOSURE_FINAL})
    r.commit("test(account-closure): add closure tests", {"tests/test_closure.py": CLOSURE_TESTS})

    # account reopening: clean
    r.commit("feat(account-reopening): reopen closed accounts within 30 days",
             {"bankapp/application/reopening.py": REOPENING, "tests/test_reopening.py": REOPENING_TESTS})

    # statements: no tests
    r.commit("feat(statements): export monthly statement as CSV",
             {"bankapp/application/statements.py": STATEMENTS})

    # later rework of the closure feature
    r.commit("hotfix: closing an account crashed on uppercase emails",
             {"bankapp/application/closure.py": CLOSURE_FINAL.replace("acc = repo.get(email)",
                                                                       "acc = repo.get(email.lower())")})
    return dest


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "demo-repo").resolve()
    print(f"demo repository created at {build(target)}")
