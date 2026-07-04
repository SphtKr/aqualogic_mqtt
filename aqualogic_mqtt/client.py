import threading
import logging
import sys
import ssl
from time import sleep
import os
import argparse

import paho.mqtt.client as mqtt
from paho.mqtt.reasoncodes import ReasonCode

from aqualogic.core import AquaLogic
from aqualogic.states import States
#ALW
from aqualogic.keys import Keys
#

from .messages import Messages
from .panelmanager import PanelManager
from . import controls  # Web/UI controls: key queue + display state
from .webapp import create_app  # Embedded Flask app for Web UI
from .vsp import PanelPumpState, VspDriver
from .equipment import EquipmentController
from .automation import AutomationEngine
from .clock_sync import ClockSyncDriver
from .heater_targets import HeaterTargetDriver

logger = logging.getLogger("aqualogic_mqtt.client")

# Monkey-patch broken serial method in Aqualogic
def _patched_write_to_serial(self, data):
    self._serial.write(data)
    self._serial.flush()
AquaLogic._write_to_serial = _patched_write_to_serial

class Client:
    _panel = None
    _paho_client = None
    _panel_thread = None
    _formatter = None
    _pman = None
    _disconnect_retries = 3
    _disconnect_retry_wait_max = 30
    _disconnect_retry_wait = 1
    _disconnect_retry_num = 0

    def __init__(self, formatter:Messages, panel_manager:PanelManager, client_id=None, transport='tcp', protocol_num=5,
                 vsp_enabled=False, vsp_enable_file=None, vsp_rollback_file=None, vsp_default_lease_seconds=60.0,
                 automation_enabled=False, automation_enable_file=None, automation_state_file=None,
                 clock_sync_state_file=None):
        self._formatter = formatter
        self._pman = panel_manager
        self._panel = AquaLogic(web_port=0)
        # Register low-level key sender so the web/UI can queue button presses
        controls.set_key_sender(self._panel.send_key)
        controls.register_with_panel(self._panel)  # live LCD feed if available
        self._vsp_driver = VspDriver(
            self._panel,
            enabled=vsp_enabled,
            enable_file=vsp_enable_file,
            rollback_file=vsp_rollback_file,
            default_lease_seconds=vsp_default_lease_seconds,
            key_sender=self._panel.send_key,
            display_reader=controls.get_display,
            menu_cache_reader=controls.get_default_menu,
        )
        controls.set_vsp_driver(self._vsp_driver)
        self._equipment = EquipmentController(
            self._panel,
            menu_cache_reader=controls.get_default_menu,
        )
        controls.set_equipment_controller(self._equipment)
        self._clock_sync = ClockSyncDriver(
            key_sender=self._panel.send_key,
            display_reader=controls.get_display,
            menu_cache_reader=controls.get_default_menu,
            state_file=clock_sync_state_file,
        )
        self._heater_targets = HeaterTargetDriver(
            self._panel,
            key_sender=self._panel.send_key,
            display_reader=controls.get_display,
            service_mode_reader=lambda: bool(self._equipment.status().get("service_mode")),
        )
        controls.set_heater_target_driver(self._heater_targets)
        self._automation = AutomationEngine(
            self._equipment,
            self._vsp_driver,
            enabled=automation_enabled,
            enable_file=automation_enable_file,
            state_file=automation_state_file,
            clock_sync=self._clock_sync,
            heater_targets=self._heater_targets,
        )
        controls.set_automation_engine(self._automation)

        protocol = mqtt.MQTTv311 if protocol_num == 3 else mqtt.MQTTv5
        self._paho_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                        client_id=client_id, transport=transport,
                                        protocol=protocol)
        self._paho_client.on_message = self._on_message
        self._paho_client.on_connect = self._on_connect
        self._paho_client.on_disconnect = self._on_disconnect
        self._paho_client.on_connect_fail = self._on_connect_fail

    # Respond to panel events
    def _panel_changed(self, panel):
        # Drain any queued keypresses as soon as a panel update arrives.
        # This closely follows the recommendation to send keys right after keepalive frames.
        try:
            controls.drain_keypresses()
        except Exception as _e:
            logger.debug(f"controls.drain_keypresses() skipped: {_e}")

        logger.debug(f"_panel_changed called... Publishing to {self._formatter.get_state_topic()}...")

        self._observe_vsp_state(panel)

        # Helpful debug to see what LCD attributes exist on this panel object
        try:
            logger.debug(
                f"display candidates: display={getattr(panel,'display',None)} "
                f"lcd_lines={getattr(panel,'lcd_lines',None)} "
                f"get_lcd_lines={'yes' if hasattr(panel,'get_lcd_lines') else 'no'}"
            )
        except Exception:
            pass

        self._pman.observe_system_message(panel.check_system_msg)
        msg = self._formatter.get_state_message(panel, self._pman)
        logger.debug(msg)

        # Optional: if display/LED info is available, expose it to the web UI
        try:
            # Map LEDs with best-effort truthiness from panel state flags.
            def onish(v):
                return v in (True, 'ON', 'On', 'on', '1', 1)

            def state_on(state):
                try:
                    return bool(panel.get_state(state))
                except Exception:
                    return False

            try:
                leds = {
                    'filter': state_on(States.FILTER) or onish(getattr(panel, 'filter_pump', getattr(panel, 'f', None))),
                    'lights': state_on(States.LIGHTS) or onish(getattr(panel, 'lights', getattr(panel, 'l', None))),
                    'spa': state_on(States.SPA) or onish(getattr(panel, 'spa', None)),
                    'pool': state_on(States.POOL) or onish(getattr(panel, 'pool', None)),
                    'spillover': state_on(States.SPILLOVER) or onish(getattr(panel, 'spillover', getattr(panel, 'spill', None))),
                    'heater_1': state_on(States.HEATER_1) or onish(getattr(panel, 'heater_1', getattr(panel, 'h1', None))),
                    'aux1': state_on(States.AUX_1) or onish(getattr(panel, 'aux1', None)),
                    'aux2': state_on(States.AUX_2) or onish(getattr(panel, 'aux2', None)),
                    'aux3': state_on(States.AUX_3) or onish(getattr(panel, 'aux3', None)),
                    'aux4': state_on(States.AUX_4) or onish(getattr(panel, 'aux4', None)),
                }
            except Exception:
                leds = {}

            # 1) Try to read native LCD lines from the panel object
            lines = []
            if hasattr(panel, 'lcd_lines') and panel.lcd_lines:
                lines = list(panel.lcd_lines)
            elif hasattr(panel, 'get_lcd_lines'):
                try:
                    lines = list(panel.get_lcd_lines())
                except Exception:
                    lines = []
            elif hasattr(panel, 'display') and isinstance(panel.display, (list, tuple)) and any(panel.display):
                # Some forks keep the raw list in `display`
                lines = [str(s).replace('\x00', '').rstrip() for s in panel.display][:4]

            # 1.5) Last-ditch pickup: scan attributes for anything that looks like the LCD
            if not any(lines):
                for name in dir(panel):
                    lower = name.lower()
                    if ('disp' in lower) or ('lcd' in lower):
                        try:
                            val = getattr(panel, name)
                            if isinstance(val, (list, tuple)) and any(val):
                                cand = [str(s).replace('\x00', '').rstrip() for s in val][:4]
                                if any(cand):
                                    lines = cand
                                    logger.debug(f"Picked LCD from panel.{name} -> {lines!r}")
                                    break
                        except Exception:
                            pass

            if any(lines):
                # Blink positions (row, col) if available; otherwise keep empty
                blink = []
                if hasattr(panel, 'blink_positions'):
                    try:
                        blink = list(panel.blink_positions) or []
                    except Exception:
                        blink = []

                # Push only when we have native LCD lines so we don't overwrite real display with blanks
                controls.update_display(lines[:4] + [""] * max(0, 4 - len(lines)), blink, leds)
                lit_leds = {name: val for name, val in leds.items() if val}
                logger.debug(f"UI lines={lines!r} blink={blink!r} leds={lit_leds!r}")
            else:
                controls.update_display(None, None, leds)
                lit_leds = {name: val for name, val in leds.items() if val}
                logger.debug(f"UI LEDs={lit_leds!r}; no native LCD lines, leaving prior display text intact")

        except Exception as _e:
            logger.debug(f"controls.update_display skipped: {_e}")

        self._paho_client.publish(self._formatter.get_state_topic(), msg)

    def _observe_vsp_state(self, panel):
        try:
            self._vsp_driver.observe(PanelPumpState(
                requested_speed_pct=getattr(panel, 'pump_speed', None),
                pump_power_w=getattr(panel, 'pump_power', None),
                filter_on=bool(panel.get_state(States.FILTER)),
                service_mode=bool(panel.get_state(States.SERVICE)),
            ))
        except Exception as exc:
            logger.debug(f"VSP state observation failed: {exc}")

    # Respond to MQTT events
    def _on_message(self, client, userdata, msg):
        logger.debug(f"_on_message called for topic {msg.topic} with payload {msg.payload}")

        payload = msg.payload.decode().strip()
        if controls.handle_automation_mqtt(msg.topic, payload):
            logger.info("MQTT command captured as host automation manual override: %s", msg.topic)
            return

        # ALW Handle button press for POOL_SPA toggle
        if msg.topic.endswith("button_pool_spa_toggle/set") and msg.payload.decode().strip().lower() in ["press", "on", "1", "true"]:
            from aqualogic.keys import Keys
            logger.info("POOL_SPA button pressed via MQTT")
            self._panel.send_key(Keys.POOL_SPA)
            return
        #
        # ALW Handle button press for PLUS
        if msg.topic.endswith("button_plus_set") and msg.payload.decode().strip().lower() in ["press", "on", "1", "true"]:
            from aqualogic.keys import Keys
            logger.info("PLUS button pressed via MQTT")
            self._panel.send_key(Keys.PLUS)
            return
        #
        new_messages = self._formatter.handle_message_on_topic(msg.topic, payload, self._panel)
        for t, m in new_messages:
            self._paho_client.publish(t, m)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        logger.debug("_on_connect called")
        if isinstance(reason_code, ReasonCode):
            if reason_code.is_failure:
                logger.critical(f"Got failure when connecting MQTT: {reason_code.getName()}! Exiting!")
                raise RuntimeError(reason_code)
        self._disconnect_retry_num = 0
        self._disconnect_retry_wait = 1

        sub_topics = self._formatter.get_subscription_topics()
        for topic in sub_topics:
            self._paho_client.subscribe(topic)
        logger.debug(f"Publishing to {self._formatter.get_discovery_topic()}...")
        logger.debug(self._formatter.get_discovery_message())
        self._paho_client.publish(self._formatter.get_discovery_topic(), self._formatter.get_discovery_message())
        ...

    def _on_connect_fail(self, userdata, reason_code):
        #TODO: Have not been able to reach here, needs testing!
        logger.debug("_on_connect_fail called")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        if isinstance(reason_code, ReasonCode):
            if reason_code.is_failure:
                logger.error(f"MQTT Disconnected: {reason_code.getName()}!")
                if self._disconnect_retry_num < self._disconnect_retries:
                    self._disconnect_retry_num += 1
                    self._disconnect_retry_wait = min(self._disconnect_retry_wait*2, self._disconnect_retry_wait_max)
                    logger.info(f"Retrying ({self._disconnect_retry_num}) after {self._disconnect_retry_wait}s...")
                    sleep(self._disconnect_retry_wait)
                    self._paho_client.reconnect()
                else:
                    logger.critical("MQTT connection failed!")
                    self._paho_client.disconnect()
                    raise RuntimeError(reason_code)
            else:
                logger.debug(f"MQTT Disconnected: {reason_code.getName()}")
        elif isinstance(reason_code, int):
            if reason_code > 0:
                logger.error(f"MQTT Disconnected: {reason_code}")

    def panel_connect(self, source):
        if ':' in source:
            s_host, s_port = source.split(':')
            self._panel.connect(s_host, int(s_port))
        else:
            self._panel.connect_serial(source)
        ...

    def mqtt_username_pw_set(self, username:(str), password:(str)):
        return self._paho_client.username_pw_set(username=username, password=password)

    def mqtt_tls_set(self, certfile=None, keyfile=None, cert_reqs=ssl.CERT_REQUIRED):
        return self._paho_client.tls_set(certfile=certfile, keyfile=keyfile, cert_reqs=cert_reqs)
    
    def mqtt_connect(self, dest:(str), port:(int)=1883, keepalive=60):
        host = dest
        if dest is not None:
            if ':' in dest:
                host, port = dest.split(':')
                port = int(port)
            else:
                host = dest
        r = self._paho_client.connect(host, port, keepalive)
        logger.debug(f"Connected to {host}:{port} with result {r}")

    def loop_forever(self):
        reconnect_attempts = 0
        MAX_RECONNECT_ATTEMPTS = 5
        RECONNECT_SLEEP_S = 5

        try:
            self._paho_client.loop_start()
            self._panel_thread = threading.Thread(target=self._panel.process, args=[self._panel_changed])
            self._panel_thread.daemon = True # https://stackoverflow.com/a/50788759/489116 ?
            self._panel_thread.start()
            #self._paho_client.loop_forever()
            while True:
                self._observe_vsp_state(self._panel)
                self._vsp_driver.tick()
                self._automation.tick()
                logger.debug(f"Update age: {self._pman.get_last_update_age()}")
                if not self._pman.is_updating():
                    age = self._pman.get_last_update_age()
                    if reconnect_attempts < MAX_RECONNECT_ATTEMPTS:
                        reconnect_attempts += 1
                        logger.critical(
                            f"Panel not updated in {age:.1f}s; reconnect attempt "
                            f"{reconnect_attempts}/{MAX_RECONNECT_ATTEMPTS} after {RECONNECT_SLEEP_S}s sleep"
                        )
                        sleep(RECONNECT_SLEEP_S)
                        continue
                    logger.critical(
                        f"Panel not updated in {age:.1f}s after {MAX_RECONNECT_ATTEMPTS} reconnect attempts; giving up"
                    )
                    raise RuntimeError("Panel stopped updating after reconnect attempts")
                if reconnect_attempts > 0:
                    logger.info("Panel recovered; resetting reconnect counter")
                    reconnect_attempts = 0
                sleep(1)
        finally:
            self._paho_client.loop_stop()
            pass
        
        

if __name__ == "__main__":
    autodisc_prefix = None
    source = None
    dest = None
    mqtt_password = os.environ.get('AQUALOGIC_MQTT_PASSWORD')
    
    parser = argparse.ArgumentParser(
                    prog='aqualogic_mqtt',
                    description='MQTT adapter for pool controllers',
                    )
    
    g_group = parser.add_argument_group("General options")
    g_group.add_argument('-e', '--enable', nargs="+", action="extend",
        choices=[k for k in Messages.get_valid_entity_meta()], metavar='',
        help=f"enable one or more entities; valid options are: {', '.join([k+' ('+v+')' for k, v in Messages.get_valid_entity_meta().items()])}")
    g_group.add_argument('-x', '--system-message-expiration', nargs=1, type=int, default=180, metavar="SECONDS",
        help="seconds after which a Check System message previously seen is dropped from reporting")
    #TODO: metavar here is a bit of a kludge and the help text isn't 100% correct!
    g_group.add_argument('-sms', '--system-message-sensor', nargs="+", type=str, action="append", metavar=("STRING", "KEY [DEV_CLASS]"),
        help="add a binary sensor that is ON when a given \"Check System\" message appears on the display, with the specified message STRING which will use the MQTT state KEY and optionally device class DEV_CLASS (default is \"problem\")--may be specified multiple times")
    g_group.add_argument('-v', '--verbose', action="count", default=0,
        help="seconds after which a Check System message previously seen is dropped from reporting")

    source_group = parser.add_argument_group("source options")
    source_group_mex = source_group.add_mutually_exclusive_group(required=True)
    source_group_mex.add_argument('-s', '--serial', type=str, metavar="/dev/path",
        help="serial device source (path)")
    source_group_mex.add_argument('-t', '--tcp', type=str, metavar="tcpserialhost:port",
        help="network serial adapter source in the format host:port")
    source_group.add_argument('-T', '--source-timeout', nargs=1, type=int, default=30, metavar="SECONDS",
        help="seconds after which the source connection is deemed to be lost if no updates have been seen--the program will exit if the timeout is reached")
    
    mqtt_group = parser.add_argument_group('MQTT destination options')
    mqtt_group.add_argument('-m', '--mqtt-dest', required=True, type=str, metavar="mqtthost:port",
        help="MQTT broker destination in the format host:port")
    mqtt_group.add_argument('--mqtt-username', type=str, help="username for the MQTT broker")
    mqtt_group.add_argument('--mqtt-password', type=str, 
        help="password for MQTT broker (recommend set the environment variable AQUALOGIC_MQTT_PASSWORD instead!)")
    mqtt_group.add_argument('--mqtt-clientid', type=str, help="client ID provided to the MQTT broker")
    mqtt_group.add_argument('--mqtt-insecure', action='store_true', 
        help="ignore certificate validation errors for the MQTT broker (dangerous!)")
    mqtt_group.add_argument('--mqtt-version', type=int, choices=[3,5], default=5, 
        help="MQTT protocol major version number (default is 5)")
    mqtt_group.add_argument('--mqtt-transport', type=str, choices=["tcp","websockets"], default="tcp",
        help="MQTT transport mode (default is tcp unless dest port is 9001 or 443)")
    
    ha_group = parser.add_argument_group("Home Assistant options")
    ha_group.add_argument('-p', '--discover-prefix', default="homeassistant", type=str, 
        help="MQTT prefix path (default is \"homeassistant\")")

    web_group = parser.add_argument_group("Web UI options")
    web_group.add_argument('--http-host', default=os.getenv('AQUALOGIC_HTTP_HOST', '0.0.0.0'), type=str, help='Web UI bind host (default: 0.0.0.0)')
    web_group.add_argument('--http-port', default=int(os.getenv('AQUALOGIC_HTTP_PORT', '0')), type=int, help='Web UI port; 0 disables (default: 0)')
    web_group.add_argument('--http-basic-user', default=os.getenv('AQUALOGIC_HTTP_USER'), type=str, help='Basic auth user for Web UI (optional)')
    web_group.add_argument('--http-basic-pass', default=os.getenv('AQUALOGIC_HTTP_PASS'), type=str, help='Basic auth password for Web UI (optional)')
    web_group.add_argument('--http-static-dir', default=os.getenv('AQUALOGIC_STATIC_DIR'), type=str, help='Path to static dir (defaults to package static)')
    web_group.add_argument('--vsp-control', action='store_true', default=os.getenv('AQUALOGIC_VSP_CONTROL', '0') == '1',
        help='enable the no-power-cycle VSP control API (default: disabled)')
    web_group.add_argument('--vsp-enable-file', default=os.getenv('AQUALOGIC_VSP_ENABLE_FILE', '.vsp-control-enabled'), type=str,
        help='local commissioning interlock file; its presence enables VSP requests (default: .vsp-control-enabled)')
    web_group.add_argument('--vsp-rollback-file', default=os.getenv('AQUALOGIC_VSP_ROLLBACK_FILE', '.vsp-rollback.json'), type=str,
        help='persistent rollback journal for interrupted VSP leases (default: .vsp-rollback.json)')
    web_group.add_argument('--vsp-default-lease-seconds', default=float(os.getenv('AQUALOGIC_VSP_DEFAULT_LEASE_SECONDS', '60')), type=float,
        help='default lifetime for a commissioning VSP target (default: 60; maximum: 900)')
    web_group.add_argument('--automation', action='store_true', default=os.getenv('AQUALOGIC_AUTOMATION', '0') == '1',
        help='enable host-owned PL-PLUS schedule reconciliation (default: disabled)')
    web_group.add_argument('--automation-enable-file', default=os.getenv('AQUALOGIC_AUTOMATION_ENABLE_FILE', '.automation-control-enabled'), type=str,
        help='local interlock file whose presence enables automation (default: .automation-control-enabled)')
    web_group.add_argument('--automation-state-file', default=os.getenv('AQUALOGIC_AUTOMATION_STATE_FILE', '.automation-state.json'), type=str,
        help='persistent calendar/manual automation state (default: .automation-state.json)')
    web_group.add_argument('--clock-sync-state-file', default=os.getenv('AQUALOGIC_CLOCK_SYNC_STATE_FILE', '.clock-sync-state.json'), type=str,
        help='persistent weekly PL-PLUS clock-sync state (default: .clock-sync-state.json)')

    args = parser.parse_args()

    print("aqualogic_mqtt Started")

    if args.verbose >= 3:
        logging.basicConfig(level=logging.DEBUG)
    elif args.verbose == 2:
        logging.basicConfig(level=logging.INFO)
    elif args.verbose == 1:
        logging.basicConfig(level=logging.WARNING)
    else:
        logging.basicConfig(level=logging.ERROR)
    
    source = args.serial if args.serial is not None else args.tcp
    dest = args.mqtt_dest

    pman = PanelManager(args.source_timeout, args.system_message_expiration)
    # Monkey-patch PanelManager into _web so we can avoid running the web server without an error
    AquaLogic._web = pman
    
    formatter = Messages(identifier="aqualogic", discover_prefix=args.discover_prefix,
                         enable=args.enable if args.enable is not None else [], 
                         system_message_sensors=args.system_message_sensor if args.system_message_sensor is not None else [])
    
    mqtt_client = Client(formatter=formatter, panel_manager=pman,
                         client_id=args.mqtt_clientid, transport=args.mqtt_transport, 
                         protocol_num=args.mqtt_version,
                         vsp_enabled=args.vsp_control,
                         vsp_enable_file=args.vsp_enable_file,
                         vsp_rollback_file=args.vsp_rollback_file,
                         vsp_default_lease_seconds=args.vsp_default_lease_seconds,
                         automation_enabled=args.automation,
                         automation_enable_file=args.automation_enable_file,
                         automation_state_file=args.automation_state_file,
                         clock_sync_state_file=args.clock_sync_state_file,
                         )
    if args.mqtt_username is not None:
        mqtt_password = args.mqtt_password if args.mqtt_password is not None else mqtt_password
        mqtt_client.mqtt_username_pw_set(args.mqtt_username, mqtt_password)
    #TODO Broker client cert
    if args.mqtt_insecure:
        mqtt_client.mqtt_tls_set(cert_reqs=ssl.CERT_NONE)

    # Start embedded Web UI server (same process -> shared controls state)
    if args.http_port and args.http_port > 0:
        try:
            app = create_app(static_dir=args.http_static_dir, basic_user=args.http_basic_user, basic_pass=args.http_basic_pass)
            import threading as _threading
            _t = _threading.Thread(target=lambda: app.run(host=args.http_host, port=args.http_port, debug=False, use_reloader=False), daemon=True)
            _t.start()
            print(f"Web UI listening on http://{args.http_host}:{args.http_port}")
        except Exception as _web_e:
            print(f"Failed to start Web UI: {_web_e}")

    print("Connecting MQTT...")
    mqtt_client.mqtt_connect(dest=dest)
    print("Connecting Controller...")
    mqtt_client.panel_connect(source)
    print("Starting loop...")
    mqtt_client.loop_forever()
