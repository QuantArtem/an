from lf_common.context import correlation_id
from lf_common.events import make_envelope


def test_envelope_contract():
    correlation_id.set("corr-1")
    env = make_envelope("shipment.created", "key-1", {"a": 1}, "shipment-service")
    assert set(env) == {"eventId", "eventType", "eventVersion", "occurredAt", "source", "correlationId", "key",
                        "payload"}
    assert env["correlationId"] == "corr-1" and env["eventVersion"] == 1 and env["payload"] == {"a": 1}


def test_event_ids_are_unique():
    assert make_envelope("t", "k", {}, "s")["eventId"] != make_envelope("t", "k", {}, "s")["eventId"]
