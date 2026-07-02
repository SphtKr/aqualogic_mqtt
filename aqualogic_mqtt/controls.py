# aqualogic_mqtt/controls.py
from __future__ import annotations
import time
import logging
from typing import Callable, List, Tuple, Optional
from collections import deque
from threading import Lock
from .default_menu import DefaultMenuCache
from .vsp import VspDriver
from .equipment import EquipmentController
from .automation import AutomationEngine
from .heater_targets import HeaterTargetDriver
try:
    # Keys enum from swilson/aqualogic
    from aqualogic.keys import Keys
except Exception:  # keep runtime resilient if import fails during edits
    class Keys:
        MENU = 'MENU'
        LEFT = 'LEFT'
        RIGHT = 'RIGHT'
        MINUS = 'MINUS'
        PLUS = 'PLUS'
        FILTER = 'FILTER'
        POOL_SPA = 'POOL_SPA'

logger = logging.getLogger("aqualogic_mqtt.controls")

# ---- Shared display state for the Web UI ----
class DisplayState:
    def __init__(self):
        self.lines: List[str] = ["", "", "", ""]
        self.blink: List[Tuple[int, int]] = []
        self.leds: dict = {}
        self.updated_at: float = time.time()
        self._lock = Lock()

    def as_dict(self) -> dict:
        with self._lock:
            return {
                "lines": list(self.lines),
                "blink": list(self.blink),
                "leds": dict(self.leds),
                "updated_at": self.updated_at,
            }

    def update(self, lines: Optional[List[str]], blink: Optional[List[Tuple[int, int]]], leds: Optional[dict]):
        with self._lock:
            if lines is not None:
                self.lines = (list(lines) + ["", "", "", ""])[:4]
            if blink is not None:
                self.blink = list(blink)
            if leds is not None:
                self.leds = dict(leds)
            self.updated_at = time.time()

_state = DisplayState()
_default_menu = DefaultMenuCache()
_vsp_driver: Optional[VspDriver] = None
_equipment: Optional[EquipmentController] = None
_automation: Optional[AutomationEngine] = None
_heater_targets: Optional[HeaterTargetDriver] = None

def update_display(lines: Optional[List[str]], blink: Optional[List[Tuple[int, int]]], leds: Optional[dict]) -> None:
    _state.update(lines, blink, leds)
    if lines is not None or leds is not None:
        current = _state.as_dict()
        observed_lines = current.get("lines") if lines is not None else []
        observed_leds = current.get("leds") if leds is not None or lines is not None else None
        _default_menu.observe_display(observed_lines, observed_leds, current.get("updated_at"))
        if _heater_targets is not None and observed_lines:
            _heater_targets.observe_display(observed_lines)

def get_display() -> dict:
    return _state.as_dict()

def get_default_menu() -> dict:
    return _default_menu.as_dict()

def set_vsp_driver(driver: VspDriver) -> None:
    global _vsp_driver
    _vsp_driver = driver

def set_equipment_controller(controller: EquipmentController) -> None:
    global _equipment
    _equipment = controller

def set_automation_engine(engine: AutomationEngine) -> None:
    global _automation
    _automation = engine

def set_heater_target_driver(driver: HeaterTargetDriver) -> None:
    global _heater_targets
    _heater_targets = driver

def get_heater_target_status() -> dict:
    if _heater_targets is None:
        return {"available": False, "busy": False, "last_error": "heater target driver is not registered"}
    return _heater_targets.status()

def refresh_heater_targets() -> dict:
    if _heater_targets is None:
        raise RuntimeError("heater target driver is not registered")
    if _vsp_driver is not None and _vsp_driver.is_menu_busy():
        raise RuntimeError("heater target read is blocked while a VSP menu operation is active")
    if not _heater_targets.is_busy() and _automation is not None and _automation.hardware_busy():
        raise RuntimeError("heater target read is blocked while another LCD menu operation is active")
    return _heater_targets.request_refresh()

def scan_heater_target(body: str) -> dict:
    if _heater_targets is None:
        raise RuntimeError("heater target driver is not registered")
    if _vsp_driver is not None and _vsp_driver.is_menu_busy():
        raise RuntimeError("heater target read is blocked while a VSP menu operation is active")
    if not _heater_targets.is_busy() and _automation is not None and _automation.hardware_busy():
        raise RuntimeError("heater target read is blocked while another LCD menu operation is active")
    return _heater_targets.request_scan(body)

def set_heater_target(body: str, target_f: int) -> dict:
    if _heater_targets is None:
        raise RuntimeError("heater target driver is not registered")
    if _vsp_driver is not None and _vsp_driver.is_menu_busy():
        raise RuntimeError("heater target write is blocked while a VSP menu operation is active")
    if not _heater_targets.is_busy() and _automation is not None and _automation.hardware_busy():
        raise RuntimeError("heater target write is blocked while another LCD menu operation is active")
    return _heater_targets.request_set(body, target_f)

def get_automation_status() -> dict:
    if _automation is None:
        return {"available": False, "enabled": False, "last_error": "automation engine is not registered"}
    return _automation.status()

def set_manual_override(values: dict) -> dict:
    if _automation is None:
        raise RuntimeError("automation engine is not registered")
    return _automation.set_manual(**values)

def set_pool_heat(enabled: bool) -> dict:
    if _automation is None:
        raise RuntimeError("automation engine is not registered")
    return _automation.set_pool_heat(enabled)

def clear_manual_override(field: Optional[str] = None) -> dict:
    if _automation is None:
        raise RuntimeError("automation engine is not registered")
    return _automation.clear_manual(field)

def activate_openclaw_spa(values: dict) -> dict:
    if _automation is None:
        raise RuntimeError("automation engine is not registered")
    return _automation.activate_openclaw_spa(
        session_id=values.get("session_id"),
        phase=values.get("phase", "spa"),
        prep_start_utc=values.get("prep_start_utc"),
        preheat_start_utc=values.get("preheat_start_utc"),
    )

def stop_openclaw_spa(session_id: Optional[str] = None) -> dict:
    if _automation is None:
        raise RuntimeError("automation engine is not registered")
    return _automation.stop_openclaw_spa(session_id)

def _web_control_lock(equipment: dict, vsp: dict, automation: dict) -> tuple[bool, Optional[str]]:
    """Lock globally only while automation owns the PL-PLUS LCD menu."""
    clock_sync = automation.get("clock_sync") or {}
    if clock_sync.get("busy"):
        return True, "Synchronizing the PL-PLUS clock"

    heater_targets = get_heater_target_status()
    if heater_targets.get("busy"):
        return True, "Reading or setting PL-PLUS heater targets"

    vsp_phase = str(vsp.get("phase") or "")
    if vsp.get("busy") and vsp_phase != "holding":
        label = vsp_phase.replace("_", " ") or "pump speed control"
        return True, f"Pump control in progress: {label}"

    return False, None

def get_equipment_status() -> dict:
    if _equipment is None:
        return {"available": False, "last_error": "equipment controller is not registered"}
    equipment = _equipment.status()
    vsp = get_vsp_status()
    automation = get_automation_status()
    controls_locked, control_lock_reason = _web_control_lock(equipment, vsp, automation)
    return {
        "available": True,
        **equipment,
        "vsp": vsp,
        "automation": automation,
        "heater_targets": get_heater_target_status(),
        "controls_locked": controls_locked,
        "control_lock_reason": control_lock_reason,
    }

def set_equipment_switch(control: str, enabled: bool) -> dict:
    if _equipment is None:
        raise RuntimeError("equipment controller is not registered")
    if _automation is not None and _automation.is_enabled():
        if control == "auto_heat":
            return _automation.set_pool_heat(enabled)
        field = "filter_on" if control == "filter" else control
        return _automation.set_manual(**{field: enabled})
    if _heater_targets is not None and _heater_targets.is_busy():
        raise RuntimeError("equipment control is blocked while a heater target menu operation is active")
    if _vsp_driver is not None and _vsp_driver.is_busy():
        raise RuntimeError("equipment control is blocked while a VSP menu operation is active")
    return _equipment.set_switch(control, enabled)

def request_equipment_mode(mode: str) -> dict:
    if _equipment is None:
        raise RuntimeError("equipment controller is not registered")
    if _automation is not None and _automation.is_enabled():
        return _automation.set_manual(mode=mode)
    if _heater_targets is not None and _heater_targets.is_busy():
        raise RuntimeError("mode control is blocked while a heater target menu operation is active")
    if _vsp_driver is not None and _vsp_driver.is_busy():
        raise RuntimeError("mode control is blocked while a VSP menu operation is active")
    return _equipment.request_mode(mode)

def get_vsp_status() -> dict:
    if _vsp_driver is None:
        return {"enabled": False, "available": False, "last_error": "VSP driver is not registered"}
    return {"available": True, **_vsp_driver.status()}

def request_vsp_preset(preset: str, lease_seconds: Optional[float] = None) -> dict:
    if _vsp_driver is None:
        raise RuntimeError("VSP driver is not registered")
    if _automation is not None and _automation.is_enabled():
        return _automation.set_manual(pump_preset=preset)
    if _heater_targets is not None and _heater_targets.is_busy():
        raise RuntimeError("pump speed control is blocked while a heater target menu operation is active")
    return _vsp_driver.request_preset(preset, source="manual", lease_seconds=lease_seconds)

def clear_vsp_target() -> dict:
    if _vsp_driver is None:
        raise RuntimeError("VSP driver is not registered")
    if _automation is not None and _automation.is_enabled():
        return _automation.clear_manual("pump_preset")
    return _vsp_driver.clear_target()

def mqtt_automation_command(topic: str, payload: str) -> Optional[tuple[str, object]]:
    value = str(payload or "").strip().upper()
    if value not in ("ON", "OFF"):
        return None
    enabled = value == "ON"
    mappings = {
        "aqualogic_switch_filter/set": ("switch", ("filter", enabled)),
        "aqualogic_light_lights/set": ("switch", ("lights", enabled)),
        "aqualogic_switch_aux_1/set": ("switch", ("blower", enabled)),
        "aqualogic_switch_aux_2/set": ("switch", ("heater_relay", enabled)),
        "aqualogic_switch_heater_auto/set": ("switch", ("auto_heat", enabled)),
    }
    for suffix, command in mappings.items():
        if str(topic).endswith(suffix):
            return command
    if enabled and str(topic).endswith("aqualogic_switch_pool/set"):
        return ("mode", "pool")
    if str(topic).endswith("aqualogic_switch_spa/set"):
        return ("mode", "spa" if enabled else "pool")
    if str(topic).endswith("aqualogic_switch_spillover/set"):
        return ("mode", "spillover" if enabled else "pool")
    return None

def handle_automation_mqtt(topic: str, payload: str) -> bool:
    command = mqtt_automation_command(topic, payload)
    if command is None:
        return False
    kind, value = command
    if kind == "mode":
        # Mode commands always use the semantic controller. This is required
        # for explicit Spillover, which is a guarded two-key transition rather
        # than a state bit supported by pyAqualogic.set_state().
        request_equipment_mode(value)
        return True
    if _automation is None or not _automation.is_enabled():
        return False
    control, enabled = value
    if control == "auto_heat":
        _automation.set_pool_heat(enabled)
        return True
    field = "filter_on" if control == "filter" else control
    _automation.set_manual(**{field: enabled})
    return True

# Convenience for when only text is known
def ingest_display_lines(lines: List[str]) -> None:
    update_display(lines, None, None)

def _clean_lines(lines_like) -> List[str]:
    out: List[str] = []
    try:
        for s in list(lines_like)[:4]:
            out.append(str(s).replace("\x00", "").rstrip())
    except Exception:
        pass
    while len(out) < 4:
        out.append("")
    return out

# ---- Key queue + sender plumbing ----
_key_sender: Optional[Callable[[object], None]] = None
_key_q = deque()
_key_lock = Lock()

_KEY_MAP = {
    "menu": Keys.MENU,
    "left": Keys.LEFT,
    "right": Keys.RIGHT,
    "minus": Keys.MINUS,
    "plus": Keys.PLUS,
    "filter": Keys.FILTER,
    "pool_spa": getattr(Keys, "POOL_SPA", getattr(Keys, "POOL_SPA_TOGGLE", Keys.MENU)),  # fallback to MENU if missing
    # optional aliases:
    "poolspa": getattr(Keys, "POOL_SPA", getattr(Keys, "POOL_SPA_TOGGLE", Keys.MENU)),
    "pool_spa_toggle": getattr(Keys, "POOL_SPA", getattr(Keys, "POOL_SPA_TOGGLE", Keys.MENU)),
}

def set_key_sender(sender: Callable[[object], None]) -> None:
    """Provide the low-level function that actually sends a key to the panel."""
    global _key_sender
    _key_sender = sender
    logger.debug("controls: key sender registered")

def enqueue_key(name: str) -> bool:
    """Queue a keypress by name (menu/left/right/minus/plus/filter/pool_spa)."""
    k = (name or "").strip().lower()
    if _vsp_driver is not None and _vsp_driver.is_menu_busy():
        logger.info("controls: key '%s' blocked while VSP menu operation is active", k)
        return False
    if _heater_targets is not None and _heater_targets.is_busy():
        logger.info("controls: key '%s' blocked while a heater target menu operation is active", k)
        return False
    if _automation is not None and _automation.hardware_busy():
        logger.info("controls: key '%s' blocked while clock synchronization is active", k)
        return False
    if k not in _KEY_MAP:
        logger.debug(f"controls: unknown key '{name}'")
        return False
    with _key_lock:
        _key_q.append(_KEY_MAP[k])
    _default_menu.invalidate_for_key(k)
    logger.info(f"controls: queued key {k}")
    return True

def drain_keypresses() -> None:
    """Send all queued keypresses to the panel; call this right after a panel update or from API."""
    global _key_sender
    if _key_sender is None:
        return
    sent = 0
    while True:
        with _key_lock:
            if not _key_q:
                break
            key = _key_q.popleft()
        try:
            _key_sender(key)
            sent += 1
        except Exception as e:
            logger.debug(f"controls: send key failed: {e}")
            break
    if sent:
        logger.debug(f"controls: sent {sent} key(s)")

# ---- Optional: hook into panel display callbacks when available ----
def register_with_panel(panel: object) -> None:
    """Attach to panel display updates in whatever form the lib exposes."""
    def _push(lines):
        try:
            ingest_display_lines(_clean_lines(lines))
            logger.debug(f"controls: ingested display via callback: {_clean_lines(lines)!r}")
        except Exception as e:
            logger.debug(f"controls: display callback failed: {e}")

    # Try method callback
    try:
        m = getattr(panel, "on_display_update", None)
        if callable(m):
            logger.debug("controls: using on_display_update(handler)")
            m(_push)
            return
    except Exception:
        pass

    # Try attribute assignment
    try:
        if hasattr(panel, "on_display_update") and not callable(getattr(panel, "on_display_update")):
            logger.debug("controls: assigning on_display_update = handler")
            setattr(panel, "on_display_update", _push)
            return
    except Exception:
        pass

    # Try generic event bus
    try:
        add_listener = getattr(panel, "add_listener", None)
        if callable(add_listener):
            logger.debug("controls: using add_listener('display', handler)")
            def _listener(kind, payload):
                if kind == "display":
                    _push(payload)
            add_listener(_listener)
            return
    except Exception:
        pass

    logger.debug("controls: no compatible display callback on panel; relying on PanelManager forwarding")
