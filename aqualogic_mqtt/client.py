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

from .messages import Messages

# Monkey-patch broken serial method in Aqualogic
def _patched_write_to_serial(self, data):
    self._serial.write(data)
    self._serial.flush()
AquaLogic._write_to_serial = _patched_write_to_serial
# Monkey-patch class property into _web so we can avoid running the web server without an error
class _WebDummy:
    def text_updated(self, str):
        return
AquaLogic._web = _WebDummy()

logging.basicConfig(level=logging.DEBUG)

class Client:
    _panel = None
    _paho_client = None
    _panel_thread = None
    _formatter = None
    _disconnect_retries = 3
    _disconnect_retry_wait_max = 30
    _disconnect_retry_wait = 1
    _disconnect_retry_num = 0

    def __init__(self, formatter:Messages, client_id=None, transport='tcp', protocol_num=5):
        self._formatter = formatter
        self._panel = AquaLogic(web_port=0)

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
        logging.debug(f"_panel_changed called... Publishing to {self._formatter.get_state_topic()}...")
        msg = self._formatter.get_state_message(panel)
        logging.debug(msg)
        self._paho_client.publish(self._formatter.get_state_topic(), msg)

    # Respond to MQTT events    
    def _on_message(self, client, userdata, msg):
        logging.debug(f"_on_message called for topic {msg.topic} with payload {msg.payload}")
        new_messages = self._formatter.handle_message_on_topic(msg.topic, str(msg.payload.decode("utf-8")), self._panel)
        for t, m in new_messages:
            self._paho_client.publish(t, m)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        logging.debug("_on_connect called")
        if isinstance(reason_code, ReasonCode):
            if reason_code.is_failure:
                logging.critical(f"Got failure when connecting MQTT: {reason_code.getName()}! Exiting!")
                raise RuntimeError(reason_code)
            #elif : #FIXME: elif what?
            #    logging.debug(f"Got unexpected reason_code when connecting MQTT: {reason_code.getName()}")
            #    logging.debug(reason_code)
        self._disconnect_retry_num = 0
        self._disconnect_retry_wait = 1

        sub_topics = self._formatter.get_subscription_topics()
        for topic in sub_topics:
            self._paho_client.subscribe(topic)
        logging.debug(f"Publishing to {self._formatter.get_discovery_topic()}...")
        logging.debug(self._formatter.get_discovery_message())
        self._paho_client.publish(self._formatter.get_discovery_topic(), self._formatter.get_discovery_message())
        ...
    
    def _on_connect_fail(self, userdata, reason_code):
        #TODO: Have not been able to reach here, needs testing!
        logging.debug("_on_connect_fail called")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        if isinstance(reason_code, ReasonCode):
            if reason_code.is_failure:
                logging.error(f"MQTT Disconnected: {reason_code.getName()}!")
                #NOTE: Paho documentation is confusing about loop_forever and reconnection. Will
                # this ever be called when loop_forever "automatically handles reconnecting"? If not, it 
                # seems this callback is really only hit on initial connect failures?
                if self._disconnect_retry_num < self._disconnect_retries:
                    self._disconnect_retry_num += 1
                    self._disconnect_retry_wait = min(self._disconnect_retry_wait*2, self._disconnect_retry_wait_max)
                    logging.info(f"Retrying ({self._disconnect_retry_num}) after {self._disconnect_retry_wait}s...")
                    sleep(self._disconnect_retry_wait)
                    self._paho_client.reconnect()
                else:
                    logging.critical("MQTT connection failed!")
                    self._paho_client.disconnect()
                    raise RuntimeError(reason_code)
            else:
                logging.debug(f"MQTT Disconnected: {reason_code.getName()}")
        elif isinstance(reason_code, int):
            if reason_code > 0:
                logging.error(f"MQTT Disconnected: {reason_code}")

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
        logging.debug(f"Connected to {host}:{port} with result {r}")

    def loop_forever(self):
        try:
            #self._paho_client.loop_start()
            self._panel_thread = threading.Thread(target=self._panel.process, args=[self._panel_changed])
            self._panel_thread.start()
            self._paho_client.loop_forever()
            #while True:
            #    sleep(1)
        finally:
            #self._paho_client.loop_stop()
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

    source_group = parser.add_argument_group("source options")
    source_group_mex = source_group.add_mutually_exclusive_group(required=True)
    source_group_mex.add_argument('-s', '--serial', type=str, metavar="/dev/path",
        help="serial device source (path)")
    source_group_mex.add_argument('-t', '--tcp', type=str, metavar="tcpserialhost:port",
        help="network serial adapter source in the format host:port")
    
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
    
    source = args.serial if args.serial is not None else args.tcp
    dest = args.mqtt_dest
    
    formatter = Messages(identifier="aqualogic", discover_prefix=args.discover_prefix, enable=args.enable)
    print(args.enable)
    
    mqtt_client = Client(formatter=formatter, 
                         client_id=args.mqtt_clientid, transport=args.mqtt_transport, 
                         protocol_num=args.mqtt_version
                         )
    if args.mqtt_username is not None:
        mqtt_password = args.mqtt_password if args.mqtt_password is not None else mqtt_password
        mqtt_client.mqtt_username_pw_set(args.mqtt_username, mqtt_password)
    #TODO Broker client cert
    if args.mqtt_insecure:
        mqtt_client.mqtt_tls_set(cert_reqs=ssl.CERT_NONE)
    mqtt_client.mqtt_connect(dest=dest)
    mqtt_client.panel_connect(source)
    mqtt_client.loop_forever()

    
