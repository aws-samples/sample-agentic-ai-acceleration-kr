"""The OIDC sign-in record (USERS_TABLE) — DynamoDB table is a fake."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repositories.user_repository import UserRepository  # noqa: E402


class FakeTable:
    """The update_item / get_item / scan subset the repository uses, with
    `if_not_exists` and single-page pagination."""

    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        item = self.items.get(Key["pk"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues):
        item = self.items.setdefault(Key["pk"], dict(Key))
        set_clause = UpdateExpression.split("SET", 1)[1].strip()
        for placeholder, value_expr in re.findall(r"(#\w+)\s*=\s*(\w+\([^)]*\)|:\w+)", set_clause):
            name = ExpressionAttributeNames[placeholder]
            if "if_not_exists" in value_expr:
                default_key = re.findall(r":\w+", value_expr)[-1]
                item.setdefault(name, ExpressionAttributeValues[default_key])
            else:
                item[name] = ExpressionAttributeValues[value_expr]
        return {"Attributes": dict(item)}

    def scan(self, **kwargs):
        rows = list(self.items.values())
        if kwargs.get("ExclusiveStartKey"):
            return {"Items": rows[1:]}
        if len(rows) > 1:
            return {"Items": rows[:1], "LastEvaluatedKey": {"pk": rows[0]["pk"]}}
        return {"Items": rows}


def repository():
    repo = UserRepository.__new__(UserRepository)
    repo.table_name = "users"
    repo.table = FakeTable()
    return repo


def test_first_login_creates_record():
    repo = repository()
    repo.upsert_on_login(
        sub="u1", provider="entra", email="a@corp.com",
        name="Alice", groups=["Claude"], role="user",
    )
    stored = repo.get("entra", "u1")
    assert stored["sub"] == "u1"
    assert stored["email"] == "a@corp.com"
    assert stored["display_name"] == "Alice"
    assert stored["role"] == "user"
    assert stored["groups"] == ["Claude"]
    assert stored["created_at"] == stored["last_login"]


def test_second_login_keeps_created_at_updates_last_login():
    repo = repository()
    repo.upsert_on_login(sub="u1", provider="entra", email="a@corp.com",
                         name="Alice", groups=[], role="user")
    first = repo.get("entra", "u1")["created_at"]
    repo.table.items["entra#u1"]["last_login"] = "STALE"
    repo.upsert_on_login(sub="u1", provider="entra", email="a2@corp.com",
                         name="Alice2", groups=["x"], role="admin")
    stored = repo.get("entra", "u1")
    assert stored["created_at"] == first
    assert stored["last_login"] != "STALE"
    assert stored["email"] == "a2@corp.com"
    assert stored["role"] == "admin"


def test_get_missing_returns_none():
    assert repository().get("entra", "nope") is None


def test_labels_prefer_email_and_follow_pagination():
    """The directory reads this once per cache window; a second page must not
    be dropped and a person without an email still gets their display name."""
    repo = repository()
    repo.upsert_on_login(sub="u1", provider="entra", email="a@corp.com", name="Alice", groups=[], role="user")
    repo.upsert_on_login(sub="u2", provider="entra", email="", name="Bob", groups=[], role="user")
    repo.upsert_on_login(sub="u3", provider="entra", email="", name="", groups=[], role="user")

    assert repo.labels() == {"u1": "a@corp.com", "u2": "Bob"}
