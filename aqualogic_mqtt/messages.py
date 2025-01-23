import json
import logging
from aqualogic.core import AquaLogic
from aqualogic.states import States

class Messages:
    _identifier = None
    _discover_prefix = None
    _root = None
    _values_for_control_state_dict = None
    _ha_status_path = None
    _onoff = {False: "OFF", True: "ON"}
    
    def __init__(self, identifier, discover_prefix, enable):
        self._identifier = identifier #TODO: Sanitize?
        self._discover_prefix = discover_prefix #TODO: Sanitize?
        self._root = f"{self._discover_prefix}/device/{self._identifier}"
        self._ha_status_path = f"{self._discover_prefix}/status" #TODO: Make path configurable

        self._control_dict = { k:v for k,v in Messages.get_control_dict(self._identifier).items() if k in enable }
        self._sensor_dict = { k:v for k,v in Messages.get_sensor_dict(self._identifier).items() if k in enable }
    
    def get_control_dict(identifier = "aqualogic"):
        return {
            #"cs": { "state": States.CHECK_SYSTEM, "id": f"{ identifier }_binary_sensor_check_system", "name": "Check System" },
            "l": { "state": States.LIGHTS, "id": f"{ identifier }_light_lights", "name": "Lights" },
            "f": { "state": States.FILTER, "id": f"{ identifier }_switch_filter", "name": "Filter" },
            "aux1": { "state": States.AUX_1, "id": f"{ identifier }_switch_aux_1", "name": "Aux 1" },
            "aux2": { "state": States.AUX_2, "id": f"{ identifier }_switch_aux_2", "name": "Aux 2" },
            "aux3": { "state": States.AUX_3, "id": f"{ identifier }_switch_aux_3", "name": "Aux 3" },
            "aux4": { "state": States.AUX_4, "id": f"{ identifier }_switch_aux_4", "name": "Aux 4" },
            "aux5": { "state": States.AUX_5, "id": f"{ identifier }_switch_aux_5", "name": "Aux 5" },
            "aux6": { "state": States.AUX_6, "id": f"{ identifier }_switch_aux_6", "name": "Aux 6" },
            "aux7": { "state": States.AUX_7, "id": f"{ identifier }_switch_aux_7", "name": "Aux 7" },
            "aux8": { "state": States.AUX_8, "id": f"{ identifier }_switch_aux_8", "name": "Aux 8" },
            "aux9": { "state": States.AUX_9, "id": f"{ identifier }_switch_aux_9", "name": "Aux 9" },
            "aux10": { "state": States.AUX_10, "id": f"{ identifier }_switch_aux_10", "name": "Aux 10" },
            "aux11": { "state": States.AUX_11, "id": f"{ identifier }_switch_aux_11", "name": "Aux 11" },
            "aux12": { "state": States.AUX_12, "id": f"{ identifier }_switch_aux_12", "name": "Aux 12" },
            "aux13": { "state": States.AUX_13, "id": f"{ identifier }_switch_aux_13", "name": "Aux 13" },
            "aux14": { "state": States.AUX_14, "id": f"{ identifier }_switch_aux_14", "name": "Aux 14" },
            "spill": { "state": States.SPILLOVER, "id": f"{ identifier }_switch_spillover", "name": "Spillover" },
            "v3": { "state": States.VALVE_3, "id": f"{ identifier }_switch_valve_3", "name": "Valve 3" },
            "v4": { "state": States.VALVE_4, "id": f"{ identifier }_switch_valve_4", "name": "Valve 4" },
            "h1": { "state": States.HEATER_1, "id": f"{ identifier }_switch_heater_1", "name": "Heater 1" },
            "hauto": { "state": States.HEATER_AUTO_MODE, "id": f"{ identifier }_switch_heater_auto", "name": "Heater Auto Mode" },
            "sc": { "state": States.SUPER_CHLORINATE, "id": f"{ identifier }_switch_super_chlorinate", "name": "Super Chlorinate" }
        }
    
    def get_sensor_dict(identifier = "aqualogic"):
        return {
            "t_a": {
                "id": f"{ identifier }_sensor_air_temperature",
                "attr": "air_temp",
                "p": "sensor",
                "dev_cla":"temperature",
                "unit_of_meas":"°F",
                "name": "Air Temperature"
            },
            "t_p": {
                "id": f"{ identifier }_sensor_pool_temperature",
                "attr": "pool_temp", 
                "p": "sensor",
                "dev_cla": "temperature",
                "unit_of_meas": "°F",
                "name": "Pool Temperature"
            },
            "t_s": {
                "id": f"{ identifier }_sensor_spa_temperature",
                "attr": "spa_temp",
                "p": "sensor",
                "dev_cla": "temperature",
                "unit_of_meas": "°F",
                "name": "Spa Temperature"
            },
            "cl_p": {
                "id": f"{ identifier }_sensor_pool_chlorinator",
                "attr": "pool_chlorinator",
                "p": "sensor",
                "dev_cla": None,
                "unit_of_meas": "%",
                "name": "Pool Chlorinator"
            },
            "cl_s": {
                "id": f"{ identifier }_sensor_spa_chlorinator",
                "attr": "spa_chlorinator",
                "p": "sensor",
                "dev_cla": None,
                "unit_of_meas": "%",
                "name": "Spa Chlorinator"
            },
            "salt": {
                "id": f"{ identifier }_sensor_salt_level",
                "attr": "salt_level",
                "p": "sensor",
                "dev_cla": None,
                "unit_of_meas": "ppm",
                "name": "Salt Level"
            },
            "s_p": {
                "id": f"{ identifier }_sensor_pump_speed",
                "attr": "pump_speed",
                "p": "sensor",
                "dev_cla": None,
                "unit_of_meas": None,
                "name": "Pump Speed"
            },
            "p_p": {
                "id": f"{ identifier }_sensor_pump_power",
                "attr": "pump_power",
                "p": "sensor",
                "dev_cla": "power",
                "unit_of_meas": "W",
                "name": "Pump Power"
            }
        }
    
    def get_valid_entity_meta():
        return { k: v['name'] for k, v in (Messages.get_sensor_dict() | Messages.get_control_dict()).items() }

    def get_subscription_topics(self):
        return [f"{self._discover_prefix}/device/{self._identifier}/+/set"]
    
    def get_discovery_topic(self):
        return f"{self._root}/config"
    
    def get_state_topic(self):
        return f"{self._root}/state"
    
    def get_state_message(self, panel):
        state = {
            "cs": self._onoff[panel.get_state(States.CHECK_SYSTEM)],
        }
        for k, v in self._sensor_dict.items():
            state[k] = getattr(panel, v['attr'])

        for k, v in self._control_dict.items():
            state[k] = self._onoff[panel.get_state(v['state'])]

        return json.dumps(state)
    
    #TODO: ^ and v move out of this class, to divorce it from Aqualogic panel?

    def handle_message_on_topic(self, topic, msg, panel):
        if topic == self._ha_status_path and msg == "online": #TODO: Make configurable?
            return [(self.get_discovery_topic(), self.get_discovery_message())] 
        
        state_dict_filtered = { k:v for (k,v) in self._control_dict.items() if f"{self._root}/{v['id']}/set" == topic }
        logging.debug(f"{state_dict_filtered=}")
        for k,v in state_dict_filtered.items(): # Really there will be only one...
            panel.set_state(v['state'], True if msg == "ON" else False)
            return []

    def get_discovery_message(self):
        p =  {
            "dev": {
                "ids": self._identifier,
                "name": self._identifier,
                "mf": "Hayward",
                "mdl": "RS485", #TODO: Probably not.
                "sw": "0.0",
                "sn": self._identifier,
                "hw": "0.0"
            },
            "o": {
                "name":"aqualogic_mqtt",
                "sw": "0.0.1a",
                "url": "https://github.com/SphtKr/aqualogic_mqtt"
            },
            "cmps": {
                f"{ self._identifier }_binary_sensor_check_system": {
                    "p": "binary_sensor",
                    "dev_cla":"problem",
                    "val_tpl":"{{ value_json.cs }}",
                    "obj_id": f"{ self._identifier }_binary_sensor_check_system",
                    "uniq_id": f"{ self._identifier }_binary_sensor_check_system",
                    "name": "Check System"
                }
            },
            "stat_t": self.get_state_topic(),
            "qos": 2
        }
        for k,v in self._sensor_dict.items():
            cmp = {
                "p": v["p"],
                "dev_cla": v["dev_cla"],
                "unit_of_meas": v["unit_of_meas"],
                "val_tpl":"{{ value_json." + k + "}}",
                "obj_id": v["id"],
                "uniq_id": v["id"],
                "name": v["name"]
            }
            p['cmps'][v["id"]] = cmp

        for k,v in self._control_dict.items():
            cmp = {
                "p": "switch",
                "dev_cla": "switch",
                "val_tpl":"{{ value_json." + k + " }}",
                "uniq_id": v["id"],
                "obj_id": v["id"],
                "name": v["name"],
                "cmd_t": f"{self._root}/{v['id']}/set"
            }
            if k == "l":
                cmp['p'] = "light"
                cmp['stat_val_tpl'] = cmp['val_tpl']
                del cmp['val_tpl']
                del cmp['dev_cla']
            p['cmps'][v["id"]] = cmp
        return json.dumps(p)
