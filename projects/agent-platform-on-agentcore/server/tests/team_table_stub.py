"""Shared stand-in for TeamRepository: a dict of rows and an audit list."""


class Table:
    def __init__(self, fail=False):
        self.rows = {}
        self.audit = []
        self.fail = fail

    def _check(self):
        if self.fail:
            raise RuntimeError("table down")

    def list_teams(self):
        self._check()
        return [dict(r) for r in self.rows.values()]

    def get_team(self, name):
        self._check()
        return dict(self.rows[name]) if name in self.rows else None

    def put_team(self, item):
        self._check()
        self.rows[item["name"]] = dict(item)

    def delete_team(self, name):
        self._check()
        self.rows.pop(name, None)

    def append_audit(self, entry):
        self.audit.append(entry)

    def list_audit(self, month, start_iso, end_iso):
        return [e for e in self.audit if start_iso <= e["at"] <= end_iso]


def seed(table, roles, managed_by="platform"):
    """Ready rows, one per name => execution role ARN, as the Organization page
    (or an external stack, with another managed_by) would have left them."""
    for name, role in roles.items():
        table.put_team({"name": name, "label": name, "execution_role_arn": role,
                        "managed_by": managed_by, "status": "ready"})
    return table
