import time
import logging
import threading

from aqualogic.core import AquaLogic
from aqualogic.states import States

logger = logging.getLogger(__name__)

# At present PanelManager only keeps track of system messages, though
# it may expand to handle all aqualogic.panel concerns in the future.
class PanelManager:
    _exp_s = None
    _delegates = None
    _panel = None

    def __init__(self, message_exp_seconds:(int), text_message_sensors:(list)=[]):
        self._exp_s = message_exp_seconds
        self._text_messages = [' '.join(x[0].split()).lower() for x in text_message_sensors] # strings to watch for
        self._registry = {}
        self._delegates = []
        # Monkey-patch PanelManager into _web so we can avoid running the web server without an error
        AquaLogic._web = self

    def panel_connect(self, source):
        self._panel = AquaLogic(web_port=0)
        if ':' in source:
            s_host, s_port = source.split(':')
            self._panel.connect(s_host, int(s_port))
        else:
            self._panel.connect_serial(source)
        self._panel_thread = threading.Thread(target=self._panel.process, args=[self.handle_panel_changed])
        self._panel_thread.daemon = True # https://stackoverflow.com/a/50788759/489116 ?
        self._panel_thread.start()

    # Hopefully temporary
    def get_panel(self):
        return self._panel
    
    # Delegates
    def add_delegate(self, delegate):
        self._delegates.append(delegate)
        delegate.set_heartbeat_time(time.time())

    def _observe_message(self, message:(str), is_system:(bool)=True):
        if message is None:
            return
        message = message.strip(' \x00')
        now = time.time()
        self._registry[message] = { 'time': now, 'is_system': is_system }
        exp = now - self._exp_s
        self._registry = { k:v for k,v in self._registry.items() if v['time'] > exp }

    def get_messages(self, is_system=None):
        r_filtered = { k: v for k, v in self._registry.items() if is_system is None or v['is_system'] == is_system }
        return sorted(r_filtered.keys())
    
    def has_message(self, message, is_system=None):
        r_filtered = { k: v for k, v in self._registry.items() if is_system is None or v['is_system'] == is_system }
        if is_system is not None:
            return message in r_filtered.keys()
        else:
            #NOTE: This MAY fuzzy-match system messages if is_system is None (all messages)!
            #k_fuzzed = [ ' '.join(k.split()).lower() for k in r_filtered.keys ]
            k_fuzzed = r_filtered.keys() # Text message keys are already fuzzed in constructor
            return ' '.join(message.split()).lower() in k_fuzzed

    def handle_panel_changed(self, panel):
        for d in self._delegates:
            d.handle_panel_changed(panel)

    def set_state(self, state, enable):
        self._panel.set_state(state, enable)

    # This is a method with the same name/sig as one in aqualogic.web.WebServer. This
    # allows 1: monkey-patching this class into aqualogic to allow the process loop to
    # function without its web server running, 2: us to pick up activity and screen
    # updates from the panel (e.g. to determine if the connection is lost).
    def text_updated(self, str:str):
        self._observe_message(self._panel.check_system_msg, is_system=True)
        logger.debug(f"text_updated: {str}")
        for tm in self._text_messages:
            if tm in ' '.join(str.split()).lower():
                self._observe_message(tm, is_system=False)
        for d in self._delegates:
            d.set_heartbeat_time(time.time())
        return
