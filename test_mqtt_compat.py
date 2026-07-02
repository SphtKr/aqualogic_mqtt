import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from aqualogic.states import States

from aqualogic_mqtt.messages import Messages
from aqualogic_mqtt import controls
from aqualogic_mqtt.controls import handle_automation_mqtt, mqtt_automation_command


class FakePanel:
    def __init__(self):
        self.calls = []

    def set_state(self, state, enabled):
        self.calls.append((state, enabled))
        return True


class MqttCompatibilityTest(unittest.TestCase):
    def setUp(self):
        self.messages = Messages(
            identifier="aqualogic",
            discover_prefix="homeassistant",
            enable=["l", "f", "aux1", "aux2", "hauto", "pool", "spa", "spill"],
            system_message_sensors=[],
        )

    def test_existing_hubitat_component_ids_and_topics_are_unchanged(self):
        discovery = json.loads(self.messages.get_discovery_message())
        expected = {
            "aqualogic_light_lights": "homeassistant/device/aqualogic/aqualogic_light_lights/set",
            "aqualogic_switch_filter": "homeassistant/device/aqualogic/aqualogic_switch_filter/set",
            "aqualogic_switch_aux_1": "homeassistant/device/aqualogic/aqualogic_switch_aux_1/set",
            "aqualogic_switch_aux_2": "homeassistant/device/aqualogic/aqualogic_switch_aux_2/set",
            "aqualogic_switch_heater_auto": "homeassistant/device/aqualogic/aqualogic_switch_heater_auto/set",
            "aqualogic_switch_pool": "homeassistant/device/aqualogic/aqualogic_switch_pool/set",
            "aqualogic_switch_spa": "homeassistant/device/aqualogic/aqualogic_switch_spa/set",
            "aqualogic_switch_spillover": "homeassistant/device/aqualogic/aqualogic_switch_spillover/set",
        }
        for component_id, topic in expected.items():
            self.assertEqual(discovery["cmps"][component_id]["cmd_t"], topic)

    def test_existing_heater_auto_command_still_uses_set_state(self):
        panel = FakePanel()
        topic = "homeassistant/device/aqualogic/aqualogic_switch_heater_auto/set"
        self.messages.handle_message_on_topic(topic, "ON", panel)
        self.assertEqual(panel.calls, [(States.HEATER_AUTO_MODE, True)])

    def test_existing_filter_command_still_uses_set_state(self):
        panel = FakePanel()
        topic = "homeassistant/device/aqualogic/aqualogic_switch_filter/set"
        self.messages.handle_message_on_topic(topic, "OFF", panel)
        self.assertEqual(panel.calls, [(States.FILTER, False)])

    def test_automation_maps_existing_hubitat_topics_without_renaming(self):
        topic = "homeassistant/device/aqualogic/aqualogic_switch_filter/set"
        self.assertEqual(mqtt_automation_command(topic, "OFF"), ("switch", ("filter", False)))
        spa = "homeassistant/device/aqualogic/aqualogic_switch_spa/set"
        self.assertEqual(mqtt_automation_command(spa, "ON"), ("mode", "spa"))
        self.assertEqual(mqtt_automation_command(spa, "OFF"), ("mode", "pool"))
        spillover = "homeassistant/device/aqualogic/aqualogic_switch_spillover/set"
        self.assertEqual(mqtt_automation_command(spillover, "ON"), ("mode", "spillover"))
        self.assertEqual(mqtt_automation_command(spillover, "OFF"), ("mode", "pool"))

    def test_hubitat_driver_exposes_explicit_mode_buttons(self):
        root = Path(__file__).parent / "hubitat_driver"
        for name in (
            "driver_MQTT_aqualogic_parent_children.groovy",
            "Hubitat_Aqualogic_MQTT.txt",
        ):
            source = (root / name).read_text(encoding="utf-8")
            for marker in (
                '["mode_pool", "Pool"]',
                '["mode_spa", "Spa"]',
                '["mode_spillover", "Spa Spillover"]',
                '"Generic Component Button Controller"',
                "void componentPush(cd, buttonNumber)",
                "case 'mode_pool': return (poolModeCmdTopic ?: null)",
                "case 'mode_spa': return (spaModeCmdTopic ?: null)",
                "case 'mode_spillover': return (spilloverModeCmdTopic ?: null)",
            ):
                self.assertIn(marker, source, f"{marker!r} missing from {name}")

    def test_hubitat_auto_heat_updates_persistent_pool_heat_preference(self):
        automation = MagicMock()
        automation.is_enabled.return_value = True
        topic = "homeassistant/device/aqualogic/aqualogic_switch_heater_auto/set"
        with patch.object(controls, "_automation", automation):
            self.assertTrue(handle_automation_mqtt(topic, "ON"))
        automation.set_pool_heat.assert_called_once_with(True)
        automation.set_manual.assert_not_called()

    def test_hubitat_modes_use_semantic_controller_without_automation(self):
        automation = MagicMock()
        automation.is_enabled.return_value = False
        equipment = MagicMock()
        with (
            patch.object(controls, "_automation", automation),
            patch.object(controls, "_equipment", equipment),
        ):
            for suffix, target in (
                ("pool", "pool"),
                ("spa", "spa"),
                ("spillover", "spillover"),
            ):
                with self.subTest(target=target):
                    topic = f"homeassistant/device/aqualogic/aqualogic_switch_{suffix}/set"
                    self.assertTrue(handle_automation_mqtt(topic, "ON"))
                    equipment.request_mode.assert_called_once_with(target)
                    equipment.reset_mock()
        automation.set_manual.assert_not_called()


if __name__ == "__main__":
    unittest.main()
