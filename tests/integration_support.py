"""
Shared fixtures for the webhook, scheduler and TMS/ERP integration tests: an
application on temporary folders (the sample shipments plus a contracted one),
a signing key, a sandbox folder apart from the data, a fake receiver instead
of the network and a fake DNS resolver. Nothing reaches the network and no
real data folder is used.
"""

import socket
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from auth import UserStore  # noqa: E402
from authz import ADMIN, Principal  # noqa: E402
from test_api import PASSWORD, Rig  # noqa: E402

SIGNING_KEY = "test-signing-key-" + "s" * 40
PUBLIC_ADDRESS = "93.184.216.34"
RECEIVER = "https://erp.partner-example.net/logidoc/hooks"

ALICE = Principal("alice", "admin", True, 1)
DAVE = Principal("dave", "admin", True, 4)
BOB = Principal("bob", "reviewer", True, 2)


class Receiver:
    """Stands in for the network: records each request, answers what the test queued (200 by default)."""

    def __init__(self):

        self.requests = []
        self.answers = []

    def __call__(self, destination, headers, body, timeout):

        self.requests.append({"destination": destination, "headers": dict(headers), "body": body, "timeout": timeout})
        answer = self.answers.pop(0) if self.answers else 200

        if isinstance(answer, BaseException):
            raise answer

        if isinstance(answer, tuple):
            return answer

        return answer, {}


class Resolver:
    """DNS: every name resolves to a public address unless the test maps it elsewhere."""

    def __init__(self):

        self.names = {}

    def __call__(self, host, port, type=None):

        address = self.names.get(host, PUBLIC_ADDRESS)

        if isinstance(address, BaseException):
            raise address

        family = socket.AF_INET6 if ":" in address else socket.AF_INET

        return [(family, socket.SOCK_STREAM, 6, "", (address, port))]


def integration_rig(tmp_path, signing_key=True, sandbox=True, extra=None):
    """A Rig whose integration runtime uses the fake receiver and resolver and never sleeps."""

    from test_freight_audit import write_contracted_shipment

    rig = Rig(tmp_path, "required")
    UserStore(tmp_path / "auth.sqlite3").create_user("dave", PASSWORD, ADMIN)
    write_contracted_shipment(rig.raw)
    rig.state.service.invalidate()

    environ = rig.state.environ

    if signing_key:
        environ["LOGIDOC_WEBHOOK_SECRET_KEY"] = SIGNING_KEY

    if sandbox:
        rig.sandbox = tmp_path / "sandbox"
        environ["LOGIDOC_INTEGRATION_SANDBOX_DIR"] = str(rig.sandbox)
        environ["LOGIDOC_INTEGRATION_SANDBOX_TOKEN"] = "sandbox-token"

    environ.update(extra or {})

    rig.receiver = Receiver()
    rig.resolver = Resolver()
    rig.sleeps = []
    rig.state.integration_options = {"transport": rig.receiver, "resolver": rig.resolver, "sleep": rig.sleeps.append}

    return rig


def register(client, url=RECEIVER, event_types=("carrier.created", "carrier.updated", "carrier_name_link.approved",
                                                "exception.created", "exception.status_changed",
                                                "freight_audit.completed", "freight_audit.discrepancy_detected"),
             name="ERP receiver"):

    reply = client.post("/api/integrations/webhooks", json={"name": name, "url": url, "event_types": list(event_types)})
    assert reply.status_code == 201, reply.text

    return reply.json()
