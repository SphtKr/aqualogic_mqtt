import logging
import sys
import ssl
from time import sleep, time
import os
import argparse

import paho.mqtt.client as mqtt
from paho.mqtt.reasoncodes import ReasonCode

from aqualogic.core import AquaLogic
from aqualogic.states import States

from .messages import Messages
from .panelmanager import PanelManager

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
    _disconnect_retries = 3
    _disconnect_retry_wait_max = 30
    _disconnect_retry_wait = 1
    _disconnect_retry_num = 0
    _hb_time = 0
    _panel_timeout = 0

    def __init__(self, formatter:Messages, panel_timeout:(float)=0, client_id=None, transport='tcp', protocol_num=5):
        self._formatter = formatter
        self._panel_timeout = panel_timeout

        protocol = mqtt.MQTTv311 if protocol_num == 3 else mqtt.MQTTv5
        self._paho_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                        client_id=client_id, transport=transport,
                                        protocol=protocol)
        self._paho_client.on_message = self._on_message
        self._paho_client.on_connect = self._on_connect
        self._paho_client.on_disconnect = self._on_disconnect
        self._paho_client.on_connect_fail = self._on_connect_fail

    # PanelManager Delegate Protocol
    def handle_panel_changed(self, panel):
        logger.debug(f"_panel_changed called... Publishing to {self._formatter.get_state_topic()}...")
        #self._pman.observe_system_message(panel.check_system_msg)
        msg = self._formatter.get_state_message()
        logger.debug(msg)
        self._paho_client.publish(self._formatter.get_state_topic(), msg)

    def set_heartbeat_time(self, hb_time):
        self._hb_time = hb_time

    # Respond to MQTT events    
    def _on_message(self, client, userdata, msg):
        logger.debug(f"_on_message called for topic {msg.topic} with payload {msg.payload}")
        new_messages = self._formatter.handle_message_on_topic(msg.topic, str(msg.payload.decode("utf-8")))
        for t, m in new_messages:
            self._paho_client.publish(t, m)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        logger.debug("_on_connect called")
        if isinstance(reason_code, ReasonCode):
            if reason_code.is_failure:
                logger.critical(f"Got failure when connecting MQTT: {reason_code.getName()}! Exiting!")
                raise RuntimeError(reason_code)
            #elif : #FIXME: elif what?
            #    logger.debug(f"Got unexpected reason_code when connecting MQTT: {reason_code.getName()}")
            #    logger.debug(reason_code)
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
                #NOTE: Paho documentation is confusing about loop_forever and reconnection. Will
                # this ever be called when loop_forever "automatically handles reconnecting"? If not, it 
                # seems this callback is really only hit on initial connect failures?
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
        try:
            self._paho_client.loop_start()
            #self._paho_client.loop_forever()
            while True:
                hb_age = time() - self._hb_time
                logger.debug(f"Update age: {hb_age}")
                if hb_age > self._panel_timeout:
                    logger.critical("Panel not updated in "+str(hb_age)+"s, exiting!")
                    raise RuntimeError("Panel stopped updating!")
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
    source_group.add_argument('-T', '--source-timeout', nargs=1, type=int, default=10, metavar="SECONDS",
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

    pman = PanelManager(args.system_message_expiration)
    
    formatter = Messages(identifier="aqualogic", discover_prefix=args.discover_prefix,
                         panel_manager=pman,
                         enable=args.enable if args.enable is not None else [], 
                         system_message_sensors=args.system_message_sensor if args.system_message_sensor is not None else [])
    
    mqtt_client = Client(formatter=formatter, panel_timeout=args.source_timeout, 
                         client_id=args.mqtt_clientid, transport=args.mqtt_transport, 
                         protocol_num=args.mqtt_version
                         )
    
    pman.add_delegate(mqtt_client)

    if args.mqtt_username is not None:
        mqtt_password = args.mqtt_password if args.mqtt_password is not None else mqtt_password
        mqtt_client.mqtt_username_pw_set(args.mqtt_username, mqtt_password)
    #TODO Broker client cert
    if args.mqtt_insecure:
        mqtt_client.mqtt_tls_set(cert_reqs=ssl.CERT_NONE)
    print("Connecting MQTT...")
    mqtt_client.mqtt_connect(dest=dest)
    print("Connecting Controller...")
    pman.panel_connect(source)
    print("Starting loop...")
    mqtt_client.loop_forever()

    
