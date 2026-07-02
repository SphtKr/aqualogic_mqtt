/**
 *  Aqualogic MQTT – Parent Driver with Component Children (Direct MQTT Client)
 *
 *  Connects directly to Mosquitto via interfaces.mqtt, subscribes to
 *  "homeassistant/device/aqualogic/state", maps JSON to parent attributes,
 *  and mirrors them to component child devices.
 *
 *  Author: alwineinger + ChatGPT helper
 *  Updated: 2026-07-02
 */

import groovy.json.JsonSlurper
import groovy.transform.Field

@Field static final Integer RECONNECT_SECS = 20

metadata {
    definition(name: "MQTT Pool Status – Aqualogic (Parent + Children)", namespace: "alwineinger", author: "AE + GPT") {
        capability "Initialize"
        capability "Refresh"
        capability "Sensor"
        capability "TemperatureMeasurement"   // parent: 'temperature' = pool water (t_p)
        capability "PowerMeter"               // parent: 'power' in watts (p_p)

        // Numeric telemetry (parent attributes)
        attribute "airTemperature", "number"   // t_a (°F)
        attribute "spaTemperature", "number"   // t_s (°F)
        attribute "salt", "number"             // ppm
        attribute "chlorinePool", "number"     // cl_p (%)
        attribute "chlorineSpa", "number"      // cl_s (%)
        attribute "pumpSpeed", "number"        // s_p (%)

        // System / strings
        attribute "systemMessage", "string"    // sysm
        attribute "cellStatus", "string"       // cs

        // Binary/enum flags – normalized to "on"/"off"
        attribute "filter", "string"           // f
        attribute "light", "string"            // l
        attribute "aux1", "string"
        attribute "aux2", "string"
        attribute "aux3", "string"
        attribute "aux4", "string"
        attribute "spill", "string"
        attribute "valve3", "string"           // v3
        attribute "valve4", "string"           // v4
        attribute "heater1", "string"          // h1
        attribute "heaterAuto", "string"       // hauto
        attribute "superChlorinate", "string"  // sc
        attribute "pool", "string"
        attribute "spa", "string"
        attribute "indoorControl", "string"    // ic

        // troubleshooting
        attribute "lastPayload", "string"
        attribute "lastTopic", "string"
        attribute "lastMqtt", "string"
        attribute "lastChildEvent", "string"
        attribute "mqttStatus", "string"

        command "testInject"
        command "forceReconnect"
    }

    preferences {
        // MQTT connection
        input name: "mqttHost", type: "string", title: "MQTT Broker Host/IP", defaultValue: "10.40.1.61", required: true
        input name: "mqttPort", type: "number", title: "MQTT Broker Port", defaultValue: 1883, required: true
        input name: "mqttUsername", type: "string", title: "MQTT Username (optional)", required: false
        input name: "mqttPassword", type: "password", title: "MQTT Password (optional)", required: false
        input name: "mqttClientId", type: "string", title: "Client ID (optional, default = device id)", required: false
        input name: "cleanSession", type: "bool", title: "Clean Session", defaultValue: true
        input name: "keepAlive", type: "number", title: "Keep Alive (seconds)", defaultValue: 60

        // Subscriptions
        input name: "stateTopic", type: "string", title: "Device State Topic", defaultValue: "homeassistant/device/aqualogic/state", required: true

        // Command Topics (optional; leave blank to disable control)
        input name: "lightCmdTopic", type: "string", title: "Command Topic – Light (ON/OFF)", defaultValue: "homeassistant/device/aqualogic/aqualogic_light_lights/set", required: false
        input name: "aux1CmdTopic", type: "string", title: "Command Topic – Aux1 (ON/OFF)", defaultValue: "homeassistant/device/aqualogic/aqualogic_switch_aux_1/set", required: false
        input name: "aux2CmdTopic", type: "string", title: "Command Topic – Aux2 (ON/OFF)", defaultValue: "homeassistant/device/aqualogic/aqualogic_switch_aux_2/set", required: false
        input name: "heaterAutoCmdTopic", type: "string", title: "Command Topic – Heater Auto (ON/OFF)", defaultValue: "homeassistant/device/aqualogic/aqualogic_switch_heater_auto/set", required: false
        input name: "poolSpaToggleTopic", type: "string", title: "Command Topic – Pool/Spa Toggle (button; PRESS)", defaultValue: "homeassistant/device/aqualogic/aqualogic_button_pool_spa_toggle/set", required: false
        input name: "filterCmdTopic", type: "string", title: "Command Topic – Filter (ON/OFF)", defaultValue: "homeassistant/device/aqualogic/aqualogic_switch_filter/set", required: false
        input name: "poolModeCmdTopic", type: "string", title: "Command Topic – Pool Mode (button)", defaultValue: "homeassistant/device/aqualogic/aqualogic_switch_pool/set", required: false
        input name: "spaModeCmdTopic", type: "string", title: "Command Topic – Spa Mode (button)", defaultValue: "homeassistant/device/aqualogic/aqualogic_switch_spa/set", required: false
        input name: "spilloverModeCmdTopic", type: "string", title: "Command Topic – Spa Spillover Mode (button)", defaultValue: "homeassistant/device/aqualogic/aqualogic_switch_spillover/set", required: false

        input name: "cmdOnPayload", type: "string", title: "ON payload (default = ON)", defaultValue: "ON", required: false
        input name: "cmdOffPayload", type: "string", title: "OFF payload (default = OFF)", defaultValue: "OFF", required: false
        input name: "cmdPressPayload", type: "string", title: "Button payload (default = PRESS)", defaultValue: "PRESS", required: false

        // Behavior
        input name: "autoReconnect", type: "bool", title: "Auto-reconnect on drop", defaultValue: true
        input name: "createChildren", type: "bool", title: "Create/maintain component child devices", defaultValue: true

        // Logging
        input name: "logEnable", type: "bool", title: "Enable Debug Logging", defaultValue: true
        input name: "descLogEnable", type: "bool", title: "Description Text Logging", defaultValue: true
        input name: "heartbeat", type: "bool", title: "Log heartbeat every minute (debug)", defaultValue: false
    }
}

// === Lifecycle ===

def installed() {
    log.info "Installed with settings: ${settings}"
    initialize()
}

def updated() {
    log.info "Updated with settings: ${settings}"
    unschedule()
    // Close any existing connection before reconnecting
    try { interfaces.mqtt.disconnect() } catch (ignored) {}
    initialize()
}

def initialize() {
    if (logEnable) runIn(1800, logsOff) // auto-disable debug after 30 minutes

    if (heartbeat) runEvery1Minute("hb")

    // Child devices
    if (createChildren) {
        ensureChild("tpool", "Generic Component Temperature Sensor", "${device.displayName} Pool Temp")
        ensureChild("tspa",  "Generic Component Temperature Sensor", "${device.displayName} Spa Temp")
        ensureChild("tair",  "Generic Component Temperature Sensor", "${device.displayName} Air Temp")
        ensureChild("pwr",   "Generic Component Power Meter",        "${device.displayName} Pump Power")
        [
            ["filter","Filter"], ["light","Light"], ["aux1","AUX1"], ["aux2","AUX2"],
            ["aux3","AUX3"], ["aux4","AUX4"], ["spill","SPILL"], ["v3","V3"],
            ["v4","V4"], ["h1","H1"], ["hauto","HAUTO"], ["sc","SC"],
            ["pool","POOL"], ["spa","SPA"]
        ].each { arr -> ensureChild(arr[0], "Generic Component Switch", "${device.displayName} ${arr[1]}") }
        [
            ["mode_pool", "Pool"],
            ["mode_spa", "Spa"],
            ["mode_spillover", "Spa Spillover"]
        ].each { arr ->
            ensureChild(arr[0], "Generic Component Button Controller", "${device.displayName} ${arr[1]}")
            sendChildEvt(arr[0], "numberOfButtons", 1)
        }
    }

    // MQTT connect
    connectMqtt()
    // Push current values to children (in case values already present)
    pushCurrentToChildren()
}

def uninstalled() {
    try { interfaces.mqtt.disconnect() } catch (ignored) {}
}

void logsOff() {
    device.updateSetting("logEnable", [value: "false", type: "bool"])
    log.info "Debug logging disabled."
}

void hb() {
    if (logEnable) log.debug "heartbeat – driver alive; mqttStatus=${device.currentValue('mqttStatus')}, lastMqtt=${device.currentValue('lastMqtt')}, lastChildEvent=${device.currentValue('lastChildEvent')}"
}

void refresh() {
    if (descLogEnable) log.info "Refresh requested; waiting for next MQTT update on ${stateTopic}"
}

void forceReconnect() {
    log.warn "Force reconnect requested"
    try { interfaces.mqtt.disconnect() } catch (ignored) {}
    runIn(1, "connectMqtt")
}

// === MQTT connection ===

private void connectMqtt() {
    String host = (mqttHost ?: "").trim()
    Integer port = (mqttPort ?: 1883) as Integer
    String uri = "tcp://${host}:${port}"

    String clientId = (mqttClientId && mqttClientId.trim()) ? mqttClientId.trim() : "hubitat-" + java.util.UUID.randomUUID().toString().replaceAll(/[^A-Za-z0-9_-]/, "")
    Map options = [
        cleanSession: (cleanSession != null ? cleanSession : true),
        keepAliveInterval: (keepAlive ?: 60) as Integer,
        userName: (mqttUsername ?: null),
        password: (mqttPassword ?: null)
    ]

    try {
        if (logEnable) log.debug "MQTT connect to ${uri} (clientId=${clientId}, clean=${options.cleanSession}, keepAlive=${options.keepAliveInterval})"
        interfaces.mqtt.connect(uri, clientId, options.userName, options.password)
        // Some hubs allow subscribe immediately; if not available until 'Connection succeeded'
        // we also subscribe in mqttClientStatus on success.
        runIn(2, "subscribeTopics")
        sendEvent(name: "mqttStatus", value: "connecting")
    } catch (Exception e) {
        log.error "MQTT connect error: ${e.message}"
        sendEvent(name: "mqttStatus", value: "connect error")
        scheduleReconnect()
    }
}

private void subscribeTopics() {
    try {
        if (!interfaces?.mqtt?.isConnected()) {
            if (logEnable) log.debug "subscribeTopics: not connected yet; will retry"
            runIn(2, "subscribeTopics")
            return
        }
        interfaces.mqtt.subscribe(stateTopic)
        if (descLogEnable) log.info "Subscribed to ${stateTopic}"
        sendEvent(name: "lastTopic", value: stateTopic as String)
    } catch (Exception e) {
        log.error "subscribeTopics error: ${e.message}"
        scheduleReconnect()
    }
}

def mqttClientStatus(String status) {
    // Called by Hubitat when connection status changes
    if (logEnable) log.debug "mqttClientStatus: ${status}"
    sendEvent(name: "mqttStatus", value: status)
    if (status?.contains("Connection succeeded")) {
        // Ensure subscription
        runIn(1, "subscribeTopics")
    } else if (status?.contains("lost") || status?.toLowerCase()?.contains("error")) {
        if (autoReconnect) scheduleReconnect()
    }
}

private void scheduleReconnect() {
    if (!autoReconnect) return
    if (logEnable) log.debug "Scheduling reconnect in ${RECONNECT_SECS}s"
    runIn(RECONNECT_SECS, "connectMqtt")
}

// === MQTT message entry point ===
// Hubitat calls this with a description like "topic: <topic>, payload: <json>"
// or sometimes "<topic> {json}"

def parse(String description) {
    if (logEnable) log.debug "parse(): ${description}"

    // Support plain and Base64-encoded topic/payload
    def ex = extractTopicAndJson(description)
    String jsonText = ex?.json
    String topic = ex?.topic ?: (state.lastTopic ?: stateTopic)

    if (!jsonText) {
        log.warn "MQTT parse: no JSON after decode attempt: ${description}"
        return
    }

    if (topic) {
        state.lastTopic = topic
        if (descLogEnable) sendEvent(name: "lastTopic", value: topic)
    }

    Map payload
    try {
        def parsed = new JsonSlurper().parseText(jsonText)
        payload = (parsed instanceof Map) ? (Map) parsed : null
    } catch (Exception e) {
        log.warn "MQTT parse: bad JSON: ${e.message}"
        return
    }
    if (!payload) return

    if (topic) {
        String got = topic.trim().toLowerCase()
        String want = (stateTopic as String).trim().toLowerCase()
        if (got != want) {
            if (logEnable) log.debug "Ignoring topic '${topic}'; expecting '${stateTopic}'"
            return
        }
    }

    def nowStr = new Date().format("yyyy-MM-dd HH:mm:ss")
    sendEvent(name: "lastMqtt", value: nowStr)

    // Keep a trimmed copy for troubleshooting
    try {
        String trimmed = jsonText
        if (trimmed?.length() > 1024) trimmed = trimmed.substring(0, 1024) + "…"
        sendEvent(name: "lastPayload", value: trimmed)
    } catch (ignored) {}

    // === Mapping to parent AND children ===

    Map childCache = childMapBySuffix()

    // Numeric telemetry
    setNum("temperature",   payload.t_p, "°F");     sendChildEvt("tpool", "temperature", payload.t_p, "°F", childCache)
    setNum("spaTemperature",payload.t_s, "°F");     sendChildEvt("tspa",  "temperature", payload.t_s, "°F", childCache)
    setNum("airTemperature",payload.t_a, "°F");     sendChildEvt("tair",  "temperature", payload.t_a, "°F", childCache)
    setNum("salt",          payload.salt, "ppm")
    setNum("chlorinePool",  payload.cl_p, "%")
    setNum("chlorineSpa",   payload.cl_s, "%")
    setNum("pumpSpeed",     payload.s_p, "%")
    setNum("power",         payload.p_p, "W");      sendChildEvt("pwr",   "power",      payload.p_p, "W", childCache)

    // Strings/system
    setStr("systemMessage", payload.sysm)
    setStr("cellStatus",    payload.cs)

    // Binary flags -> parent attr + child switch state
    setBin("filter",          payload.f);     sendChildEvt("filter", "switch", onOff(payload.f), null, childCache)
    setBin("light",           payload.l);     sendChildEvt("light",  "switch", onOff(payload.l), null, childCache)

    ["aux1","aux2","aux3","aux4","spill","v3","v4","h1","hauto","sc","pool","spa"].each { key ->
        def val = payload[key]
        setBin(key as String, val)
        sendChildEvt(key as String, "switch", onOff(val), null, childCache)
    }
}

// === Helpers ===

private String extractJson(String s) {
    // Find the FIRST '{' and the matching LAST '}', tolerant of prefix/suffix noise
    int start = s.indexOf("{")
    int end   = s.lastIndexOf("}")
    if (start < 0 || end < 0 || end <= start) return null
    String candidate = s.substring(start, end + 1)
    // Quick sanity check: looks like JSON object
    return (candidate.startsWith("{") && candidate.endsWith("}")) ? candidate : null
}

private String extractTopic(String description, String jsonText) {
    // 1) "topic: <t>, payload: <json>" (case-insensitive)
    def m1 = (description =~ /(?i)topic:\s*([^,]+),\s*(payload|message):/)
    if (m1.find()) return m1.group(1).trim()

    // 2) "<topic> {json}"
    int idx = description.indexOf(jsonText)
    if (idx > 0) {
        String before = description.substring(0, idx).trim()
        if (before) {
            // drop any leading labels like "mqtt:" or "message:" (case-insensitive)
            before = before.replaceFirst(~/(?i)^(mqtt:|message:)\s*/, "")
            // Take first token up to comma/space if needed
            return before.split(/[\s,]+/)[0]
        }
    }

    // 3) Fallback search for "topic: <t>" (case-insensitive)
    def m2 = (description =~ /(?i)topic:\s*([^,\s]+)/)
    if (m2.find()) return m2.group(1).trim()

    return null
}

/** Try to Base64-decode a string; return decoded text if plausible, else null */
private String tryBase64Decode(String s) {
    if (!s) return null
    try {
        String t = s.trim()
        // Strip surrounding quotes if present
        if ((t.startsWith('"') && t.endsWith('"')) || (t.startsWith("'") && t.endsWith("'"))) {
            t = t.substring(1, t.length()-1)
        }
        byte[] bytes = t.decodeBase64()
        if (!bytes) return null
        String out = new String(bytes, 'UTF-8')
        // Heuristic: must contain expected separators or JSON braces
        if (out.contains("/") || out.contains("{") || out.contains(":")) return out
    } catch (Exception ignored) {}
    return null
}

/** Extract topic and JSON, handling both plain and base64-encoded forms */
private Map extractTopicAndJson(String description) {
    String topic = null
    String json = null

    // Pattern: "topic: <t>, payload: <p>" (p or t may be base64)
    def m = (description =~ /(?i)topic:\s*([^,]+),\s*(payload|message):\s*(.+)$/)
    if (m.find()) {
        String t = m.group(1).trim()
        String p = m.group(3).trim()
        String tDec = tryBase64Decode(t) ?: t
        String pDec = tryBase64Decode(p) ?: p
        if (pDec?.startsWith('{') && pDec?.endsWith('}')) json = pDec
        topic = tDec
    }

    // Fallback: "<topic> {json}"
    if (!json) {
        json = extractJson(description)
        if (!topic && json) topic = extractTopic(description, json)
    }

    return [topic: topic, json: json]
}

private String onOff(def v) {
    if (v == null) return null
    String s = v.toString().trim().toUpperCase()
    return (s == "ON" || s == "1" || s == "TRUE") ? "on" : "off"
}

private void setNum(String name, def v, String unit = null) {
    BigDecimal num = safeToBigDecimal(v)
    if (num == null) return
    if (!valueChanged(name, num, unit)) return
    Map evt = [name: name, value: num]
    if (unit) evt.unit = unit
    sendEvent(evt)
}

private BigDecimal safeToBigDecimal(def v) {
    if (v == null) return null
    try { return v as BigDecimal } catch (ignored) { return null }
}

private void setStr(String name, def v) {
    if (v == null) return
    String value = v.toString()
    if (!valueChanged(name, value)) return
    sendEvent(name: name, value: value)
}

private void setBin(String name, def v) {
    if (v == null) return
    String value = onOff(v)
    if (value == null) return
    if (!valueChanged(name, value)) return
    sendEvent(name: name, value: value)
}

private boolean valueChanged(String name, Object newValue, String unit = null) {
    if (newValue == null) return false
    Object currentValue = device.currentValue(name)
    def state = device.currentState(name)
    Object normalizedNew = normalizeForComparison(newValue)
    Object normalizedCurrent = normalizeForComparison(currentValue)

    boolean valuesEqual
    if (normalizedNew == null && normalizedCurrent == null) {
        valuesEqual = true
    } else if (normalizedNew != null) {
        valuesEqual = (normalizedNew == normalizedCurrent) || normalizedNew.equals(normalizedCurrent)
    } else {
        valuesEqual = false
    }

    if (!valuesEqual) {
        return true
    }

    if (unit != null) {
        String currentUnit = state?.unit
        return currentUnit != unit
    }

    return false
}

private Object normalizeForComparison(Object value) {
    if (value == null) return null
    if (value instanceof Number) {
        BigDecimal bd = safeToBigDecimal(value)
        return bd != null ? bd.stripTrailingZeros() : value
    }
    String s = value.toString()
    String trimmed = s?.trim()
    if (!trimmed) return ""
    BigDecimal numeric = safeToBigDecimal(trimmed)
    if (numeric != null) {
        return numeric.stripTrailingZeros()
    }
    return trimmed
}

// ---- Child helpers ----

private String childDni(String suffix) { return "${device.deviceNetworkId}-${suffix}" }
private String suffixFromChild(cd) {
    String dni = cd?.deviceNetworkId
    String prefix = device.deviceNetworkId + "-"
    return (dni?.startsWith(prefix)) ? dni.substring(prefix.length()) : null
}

private void ensureChild(String suffix, String typeName, String label) {
    def dni = childDni(suffix)
    if (!getChildDevice(dni)) {
        addChildDevice("hubitat", typeName, dni, [name: label, label: label, isComponent: true])
        if (logEnable) log.info "Created child: ${label} (${typeName})"
    }
}

/** Build a suffix -> child device cache for quick lookups. */
private Map<String, Object> childMapBySuffix() {
    Map<String, Object> cache = [:]
    childDevices?.each { cd ->
        String suffix = suffixFromChild(cd)
        if (suffix) cache[suffix] = cd
    }
    return cache
}

/** Deliver event to child using the Generic Component convention. */
private void sendChildEvt(String suffix, String name, Object value, String unit=null, Object childOrMap=null) {
    def cd = null
    if (childOrMap instanceof Map) {
        cd = childOrMap[suffix]
    } else if (childOrMap) {
        cd = childOrMap
    } else {
        cd = getChildDevice(childDni(suffix))
    }
    if (!cd) return
    Map evt = [name: name, value: value]
    if (unit) evt.unit = unit
    try {
        cd.parse([evt]) // preferred for Generic Component drivers
        if (logEnable) log.debug "Child ${suffix} <- ${evt}"
    } catch (Exception e) {
        if (logEnable) log.warn "Child ${suffix} parse failed: ${e.message}"
        try { cd.sendEvent(evt) } catch (ignored) {}
    }
}

private void emitChildCurrentValue(String suffix, Map childCache) {
    switch (suffix) {
        case 'tpool':   sendChildEvt('tpool','temperature', device.currentValue('temperature'), '°F', childCache); break
        case 'tspa':    sendChildEvt('tspa','temperature', device.currentValue('spaTemperature'), '°F', childCache); break
        case 'tair':    sendChildEvt('tair','temperature', device.currentValue('airTemperature'), '°F', childCache); break
        case 'pwr':     sendChildEvt('pwr','power',        device.currentValue('power'), 'W', childCache); break
        case 'filter':  sendChildEvt('filter','switch',    device.currentValue('filter'), null, childCache); break
        case 'light':   sendChildEvt('light','switch',     device.currentValue('light'), null, childCache); break
        case 'aux1':    sendChildEvt('aux1','switch',      device.currentValue('aux1'), null, childCache); break
        case 'aux2':    sendChildEvt('aux2','switch',      device.currentValue('aux2'), null, childCache); break
        case 'aux3':    sendChildEvt('aux3','switch',      device.currentValue('aux3'), null, childCache); break
        case 'aux4':    sendChildEvt('aux4','switch',      device.currentValue('aux4'), null, childCache); break
        case 'spill':   sendChildEvt('spill','switch',     device.currentValue('spill'), null, childCache); break
        case 'v3':      sendChildEvt('v3','switch',        device.currentValue('valve3'), null, childCache); break
        case 'v4':      sendChildEvt('v4','switch',        device.currentValue('valve4'), null, childCache); break
        case 'h1':      sendChildEvt('h1','switch',        device.currentValue('heater1'), null, childCache); break
        case 'hauto':   sendChildEvt('hauto','switch',     device.currentValue('heaterAuto'), null, childCache); break
        case 'sc':      sendChildEvt('sc','switch',        device.currentValue('superChlorinate'), null, childCache); break
        case 'pool':    sendChildEvt('pool','switch',      device.currentValue('pool'), null, childCache); break
        case 'spa':     sendChildEvt('spa','switch',       device.currentValue('spa'), null, childCache); break
    }
}


// Map child suffix to configured command topic (if any)
private String commandTopicForSuffix(String suffix) {
    switch (suffix) {
        case 'light': return (lightCmdTopic ?: null)
        case 'aux1':  return (aux1CmdTopic ?: null)
        case 'aux2':  return (aux2CmdTopic ?: null)
        case 'hauto': return (heaterAutoCmdTopic ?: null)
        case 'filter': return (filterCmdTopic ?: null)
        case 'mode_pool': return (poolModeCmdTopic ?: null)
        case 'mode_spa': return (spaModeCmdTopic ?: null)
        case 'mode_spillover': return (spilloverModeCmdTopic ?: null)
        // Optional: using either pool or spa child as a trigger for the toggle button
        case 'pool':
        case 'spa':   return (poolSpaToggleTopic ?: null)
    }
    return null
}

private void mqttPublish(String topic, String payload) {
    if (!topic) { if (logEnable) log.debug "mqttPublish: no topic provided"; return }
    try {
        if (!interfaces?.mqtt?.isConnected()) {
            log.warn "MQTT not connected; cannot publish to ${topic}"
            return
        }
        if (logEnable) log.debug "Publish -> ${topic} : ${payload}"
        interfaces.mqtt.publish(topic, payload)
    } catch (Exception e) {
        log.error "Publish error to ${topic}: ${e.message}"
    }
}

// === Component (child -> parent) callbacks ===

void componentRefresh(cd){
    if (logEnable) log.debug "componentRefresh from ${cd.displayName}"
    Map childCache = childMapBySuffix()
    def suffix = suffixFromChild(cd)
    if (!suffix) return
    emitChildCurrentValue(suffix, childCache)
    sendEvent(name: "lastChildEvent", value: new Date().format("yyyy-MM-dd HH:mm:ss"))
}


void componentOn(cd)  {
    String suffix = suffixFromChild(cd)
    if (logEnable) log.debug "componentOn from ${cd.displayName} (suffix=${suffix})"
    String topic = commandTopicForSuffix(suffix)
    if (!topic) {
        log.info "No command topic configured for '${suffix}' – ignoring on()"
        return
    }
    // Pool/Spa toggle is a button press; send press payload regardless of on/off
    if (suffix in ['pool','spa']) {
        mqttPublish(topic, (cmdPressPayload ?: 'PRESS'))
        return
    }
    mqttPublish(topic, (cmdOnPayload ?: 'ON'))
}

void componentOff(cd) {
    String suffix = suffixFromChild(cd)
    if (logEnable) log.debug "componentOff from ${cd.displayName} (suffix=${suffix})"
    String topic = commandTopicForSuffix(suffix)
    if (!topic) {
        log.info "No command topic configured for '${suffix}' – ignoring off()"
        return
    }
    // Pool/Spa toggle is a button press; treat off() the same as on()
    if (suffix in ['pool','spa']) {
        mqttPublish(topic, (cmdPressPayload ?: 'PRESS'))
        return
    }
    mqttPublish(topic, (cmdOffPayload ?: 'OFF'))
}

void componentPush(cd, buttonNumber) {
    String suffix = suffixFromChild(cd)
    if (logEnable) log.debug "componentPush from ${cd.displayName} (suffix=${suffix}, button=${buttonNumber})"
    if (!(suffix in ['mode_pool', 'mode_spa', 'mode_spillover'])) {
        log.info "Ignoring unsupported component button '${suffix}'"
        return
    }
    if ((buttonNumber as Integer) != 1) {
        log.info "Ignoring button ${buttonNumber} for '${suffix}'; only button 1 is supported"
        return
    }
    String topic = commandTopicForSuffix(suffix)
    if (!topic) {
        log.info "No command topic configured for '${suffix}' – ignoring push()"
        return
    }
    // Mode buttons are explicit targets carried on the existing switch topics.
    mqttPublish(topic, (cmdOnPayload ?: 'ON'))
}

// Manual test: inject a known-good payload into parse()
void testInject() {
    String sample = 'homeassistant/device/aqualogic/state {"cs":"OFF","sysm":"Low Volts","t_a":85,"t_p":87,"t_s":86,"cl_p":55,"cl_s":5,"salt":2800.0,"s_p":70,"p_p":465,"l":"OFF","f":"ON","aux1":"OFF","aux2":"ON","aux3":"OFF","aux4":"OFF","spill":"OFF","v3":"OFF","v4":"OFF","h1":"OFF","hauto":"OFF","sc":"OFF","pool":"ON","spa":"OFF","ic":"OFF"}'
    log.info "Running testInject() with sample payload"
    parse(sample)
}

// Push the current parent values to all children
private void pushCurrentToChildren() {
    Map childCache = childMapBySuffix()
    if (!childCache) return

    boolean anySent = false
    ["tpool","tspa","tair","pwr","filter","light","aux1","aux2","aux3","aux4","spill","v3","v4","h1","hauto","sc","pool","spa"].each { s ->
        if (childCache[s]) {
            emitChildCurrentValue(s, childCache)
            anySent = true
        }
    }

    if (anySent) {
        sendEvent(name: "lastChildEvent", value: new Date().format("yyyy-MM-dd HH:mm:ss"))
    }
}
