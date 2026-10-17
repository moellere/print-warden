import json
from datetime import UTC, datetime

from print_warden import config, ha_entities, mqtt_entities

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def _rows(state="standby", connected=True):
    return ha_entities.printer_entities(
        "Garage X4",
        {"moonraker_connected": connected, "state": state, "klippy": "ready"},
        None,
        None,
        1,
        "clear",
        "PLA",
        NOW,
    )


class FakeClient:
    def __init__(self):
        self.sent = []

    async def publish(self, topic, payload, *, retain):
        assert retain
        self.sent.append((topic, payload))


def test_discovery_gives_unique_ids_a_device_and_the_rest_entity_ids():
    discovery, states = mqtt_entities.messages(
        "Garage X4", "garage_x4", _rows(), "homeassistant", "0.3.0"
    )
    configs = {t: json.loads(p) for t, p in discovery}
    state = configs["homeassistant/sensor/print_warden_garage_x4_state/config"]
    assert state["unique_id"] == "print_warden_garage_x4_state"
    assert state["default_entity_id"] == "sensor.print_warden_garage_x4_state"
    assert state["device"]["identifiers"] == ["print_warden_garage_x4"]
    assert state["device"]["sw_version"] == "0.3.0"
    assert state["availability_topic"] == "print_warden/status"
    assert state["icon"] == "mdi:printer-3d"
    progress = configs["homeassistant/sensor/print_warden_garage_x4_progress/config"]
    assert progress["unit_of_measurement"] == "%"
    assert len(configs) == 6
    by_topic = dict(states)
    assert by_topic["print_warden/garage_x4/state/state"] == "standby"
    attrs = json.loads(by_topic["print_warden/garage_x4/state/attributes"])
    assert attrs["printer"] == "Garage X4"
    assert "friendly_name" not in attrs
    assert "icon" not in attrs


def test_unknown_timestamp_is_published_as_none():
    _, states = mqtt_entities.messages(
        "Garage X4", "garage_x4", _rows(), "homeassistant", "x"
    )
    assert dict(states)["print_warden/garage_x4/finish/state"] == "None"


async def test_publishes_only_what_changed_and_everything_after_reconnect():
    pub = mqtt_entities.MqttEntities(config.Mqtt(host="broker"), "0.3.0")
    client = FakeClient()
    snapshot = {"Garage X4": ("garage_x4", _rows())}
    assert await pub.publish(client, snapshot) == 18
    assert await pub.publish(client, snapshot) == 0
    client.sent.clear()
    assert (
        await pub.publish(client, {"Garage X4": ("garage_x4", _rows(connected=False))})
        == 1
    )
    assert client.sent == [("print_warden/garage_x4/state/state", "offline")]
    pub.sent.clear()
    assert await pub.publish(client, snapshot) == 18


def test_mqtt_config(tmp_path, monkeypatch):
    monkeypatch.setenv("BROKER_PW", "s3cret")
    f = tmp_path / "printers.yaml"
    f.write_text(
        "printers:\n  p:\n    moonraker: http://p\n"
        "home_assistant:\n  url: http://ha\n  webhook_id: w\n"
        "  mqtt:\n    host: broker.lan\n    username: warden\n    password_env: BROKER_PW\n"
    )
    cfg = config.load(f)
    assert cfg.ha is not None
    assert cfg.ha.mqtt == config.Mqtt(
        host="broker.lan", port=1883, username="warden", password="s3cret"
    )
