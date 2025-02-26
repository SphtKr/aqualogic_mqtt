# aqualogic_mqtt

This is a Python module that connects to the RS485 interface on certain Hayward Aqualogic pool controllers
and interfaces it with an MQTT broker according to the [automatic discovery convention](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery) defined by
[Home Assistant](https://www.home-assistant.io). This means that state information is readable _and_ that control of pumps and relays is 
possible via MQTT (and potentially by Home Assistant).

Connection can be directly via a serial port (recommended) or a serial-to-TCP network adapter.

This software uses the [`aqualogic` module by swilson](https://github.com/swilson/aqualogic) (with some monkey-patching). It requires Python 3.x, paho-mqtt and the original aqualogic Python module. It has been tested with:

* Hayward Aqua Plus (salt water system)
  * Pool only, no spa
  * No heater, no additional wired or wireless remotes
* aqualogic 3.4 (swilson's Python module)
* Python 3.10
* Mosquitto 2.0
* Home Assistant 2024.12

>**NOTE:** Generally speaking, the major difficulty with developing a piece of software like this is that basically no one has more than one pool controller to test with—the author has only one, the system described
above. Bug reports and code contributions are welcome! But, understand that it will be very difficult to support other hardware configurations
at a level beyond guesswork!

This software is not affiliated with or endorsed by Hayward or any other entity in any way. Any trademarks
or other intellectual property remain the property of their respective owners.

## Limitations/TODOs

There are several!

* ~~Not currently possible to set a username or password for MQTT!~~
* ~~Currently only the Filter, Aux 1, Aux 2, and Super Chlorinate controls are exposed~~
* ~~Currently only Air/Pool/Spa Temperature, Pool/Spa Chlorinator (%), Salt Level, and Check System sensors are exposed~~
* Pool/Spa button and Service button not yet supported
* ~~System Messages are not yet supported~~
* ~~Serial failures may result in hanging—the process may not exit nor recover, and may have to be killed manually~~
* Metric unit configured systems are not yet supported
* Not yet possible to use a customized Home Assistant MQTT birth message topic or payload
* Only one pool controller is supported per MQTT broker (please describe your setup in an issue if this affects you 🤨)
* Others I'm forgetting
* Others I don't know about

### IMPORTANT:

It should go without saying, but do not rely on this software for any safety-related function or for freeze protection. _You should leave any safety interlocks and the freeze protection function in the controller enabled._ Only use this software for convenience automation and control—it is not reliable enough for anything else, and never will be. You have been warned!

## Pre-Running

You will probably want to use a [virtual environment](https://docs.python.org/3/library/venv.html) (venv) to install the dependencies, something like this:

```console
$ python3 -m venv ./venv-pool
$ . ./venv-pool/bin/activate
(venv-pool)$ pip install -r requirements.txt
```

This venv should remain activated when you run the module as described below.

## Running

See all options by running: `python -m aqualogic_mqtt.client --help`

The module can be minimally started like so:

```
python -m aqualogic_mqtt.client \
  -s [serial port path] \
  -m [MQTT hostname]:[MQTT port]
```

E.g. the below command starts sending data from a USB RS485 serial device to a MQTT broker running on the same machine, and will enable the "Check System" and "System Messages" sensors (and nothing else):

```console
(venv-pool)$ python -m aqualogic_mqtt.client -s /dev/ttyUSB0 -m localhost:1883
```

> [!NOTE]
> While the topic cannot be covered in depth here, be aware that using multiple USB serial devices (including for example a mix of a USB RS485 interface and Z-Wave or Zigbee stick) may result in unpredictable paths for the serial devices--you may need to set up udev rules to make the correct devices show up at the configured path(s).

It is also possible to use the `-t` option (in lieu of `-s`) to connect to a Serial/TCP converter (e.g. a USR-N510) with a host:port, like so
```console
(venv-pool)$ python -m aqualogic_mqtt.client -t 192.168.1.88:8899 -m localhost:1883
```
Note, however, that using a network converter such as this has
been found to be unreliable for device control (reading values
usually works well enough).

### Enabling Sensors and Switches

Only the "Check System" sensor and "System Messages" sensor are enabled by default. Additional sensors and switches that are present on your system and that you want visible/controllable should be specified using the `-e`/`--enable` option. One or more space-separated device "keys" should be provided to this option; for example, this command:

```
python -m aqualogic_mqtt.client -e l f aux1 t_p -s /dev/ttyUSB0 -m localhost:1883
```
...would enable the Lights (`l`), Filter (`f`), Aux 1 (`aux1`) switches and the Pool Temperature (`t_p`) sensor.

The full list of valid keys is shown in the table below and can be printed by using the `--help` option as mentioned above:

| Key | Sensor/Switch
| --- | -------------
| t_a | Air Temperature
| t_p | Pool Temperature
| t_s | Spa Temperature
| cl_p | Pool Chlorinator
| cl_s | Spa Chlorinator
| salt | Salt Level
| s_p | Pump Speed
| p_p | Pump Power
| l | Lights
| f | Filter
| aux1 | Aux 1
| aux2 | Aux 2
| aux3 | Aux 3
| aux4 | Aux 4
| aux5 | Aux 5
| aux6 | Aux 6
| aux7 | Aux 7
| aux8 | Aux 8
| aux9 | Aux 9
| aux10 | Aux 10
| aux11 | Aux 11
| aux12 | Aux 12
| aux13 | Aux 13
| aux14 | Aux 14
| spill | Spillover
| v3 | Valve 3
| v4 | Valve 4
| h1 | Heater 1
| hauto | Heater Auto Mode
| sc | Super Chlorinate

### Message Sensors

#### System Message String Sensor

By default, a string sensor is added that includes all "Check System" messages that are deemed "active," separated by `, `. Messages are "active" if they have been seen within a time window that defaults to the last three minutes. 

#### System Message Binary Sensors

Additional binary sensors can be created based on strings that appear as "Check System" messages by using the `-sms`/`--system-message-sensor` option. Minimally, the desired message string must be passed as the first argument to the `-sms` option. An optional second argument specifies a (usually shorter) "key" to use in the MQTT state payload for the sensor. An optional third argument can be used to specify a different [Home Assistant Device Class](https://www.home-assistant.io/integrations/binary_sensor/#device-class) for the sensor (the default is `problem`).

For example, the following command...
```
python -m aqualogic_mqtt.client -sms "Inspect Cell" ic -s /dev/ttyUSB0 -m localhost:1883
```
Would add a binary sensor that is "ON" when the text "Check System Inspect Cell" appears on the screen.

The `-sms` option can be specified multiple times to add additional sensors. The string provided should _exactly match_ the text that appears after "Check System" on the display, including letter case. If a "key" is specified, it must not conflict with any of the keys for existing devices listed above (nor can it be `cs` or `sysm`).

#### Text Message Binary Sensors

You can also create binary sensors that activate if a specific text message appears _anywhere_ the controller display. Do this with the `-tms`/`--text-message-sensor` option. The syntax is the same as the `--system-message-sensor` option above.

For example, the following command...
```
python -m aqualogic_mqtt.client -tms "Freeze Warning" frz cold -s /dev/ttyUSB0 -m localhost:1883
```
Would add a binary sensor that is "ON" when the text "Freeze warning" appears on the screen.

Note that unlike `--system-message-sensor`, this option matches regardless of letter case and whitespace (including line breaks), and can be any substring of the text that appears on the screen--e.g. the string `Freeze Warning` does not perfectly match the screen message that appears when freeze protection is active, which is actually `Freeze warning` / `Filter On`. The spacing and capitalization specified in the command will determine the appearance of the entity name in Home Assistant. Also, note that the command above gives an example of using an alternate Home Assistant device class, in this case, `cold`.

#### Adjusting the active time window

The time window before a message is dropped from active messages can be adjusted with the `-x`/`--system-message-expiration` option. A numeric value in seconds should be provided after this flag (default is 180). For example, this would increase the expiration time for system messages to five minutes:
```
python -m aqualogic_mqtt.client -x 300 ic -s /dev/ttyUSB0 -m localhost:1883
```
This will affect both the string sensor and any binary sensors.

### MQTT Connection Options

Besides just the MQTT broker's host and port, there are a number of other options that you can specify regarding the connection:

* `--mqtt-username MQTT_USERNAME`
  * username for the MQTT broker
* `--mqtt-password MQTT_PASSWORD`
  * password for MQTT broker 
    > [!CAUTION]
    > Generally, specifying passwords on the command line is an insecure practice. See below for a better option.
* `--mqtt-clientid MQTT_CLIENTID`
  * client ID provided to the MQTT broker
* `--mqtt-insecure`
    * ignore certificate validation errors for the MQTT broker
      > [!CAUTION]
      > Using this option exposes you to potential impersonation/MITM attacks.
* `--mqtt-version {3,5}`
  * MQTT protocol major version number (default is 5)
* `--mqtt-transport {tcp,websockets}`
  * MQTT transport mode (default is tcp unless dest port is 9001 or 443)

#### `AQUALOGIC_MQTT_PASSWORD` environment variable

To avoid specifying the MQTT client password on the command line (where it may be visible in history and process listings), you should instead store such a password in the environment variable `AQUALOGIC_MQTT_PASSWORD`. This variable will be checked if you specify
`--mqtt-username`. If `--mqtt-password` is also specified, the command line option overrides the environment variable.

### Home Assistant related options

* `-p DISCOVER_PREFIX` or `--discover-prefix DISCOVER_PREFIX`
  * The MQTT Discovery Prefix determines the "path" on the MQTT broker where the interface is exposed. The default for this option is `homeassistant`, which matches the default in Home Assistant. If you have changed it in your Home Assistant configuration, you should specify a different value here.

### Other options

#### Source Timeout

When running, the module keeps track of how long it has been since the last update was received from the pool controller. If no message has been received within the timeout period, the process _exits_. This is designed to allow some other managing process (e.g. container orchestrator, systemd, supervisor, etc.) to restart the module process, hopefully fixing any connection issue. In practice, this has worked to solve serial port "timeout" errors if the cable is disconnected and reconnected or if a power outage takes the pool controller offline.

The default timeout before exit is 10 seconds. Use the `-T`/`--source-timeout` option to change this value, providing some number of seconds as an argument. Using an arbitrarily high number for this value effectively disables the exiting behavior, but this is not recommended.

#### Verbose output

You can specify `-v`, `-vv`, or `-vvv` to get more output, up to debugging output. Please consider including the output with `-vvv` on if you are submitting a bug report.

## Running in a container

There are prebuilt container images for releases at [ghcr.io/sphtkr/aqualogic_mqtt](ghcr.io/sphtkr/aqualogic_mqtt). While details of running containerized software such as this is a large topic and mostly out of scope, the following are starting points.

Generally, when using the provided container image you should pass the same arguments described above (as if you were running the Python module directly) to the container environment. The Python module is configured as the container `ENTRYPOINT` such that it can receive these arguments.

### Docker

You should be able to start this module in Docker with a command like the following:

```console
docker run -d --restart=unless-stopped \
  --privileged --device=/dev/ttyUSB0 \
  ghcr.io/sphtkr/aqualogic_mqtt:latest \
  -s /dev/ttyUSB0 -m 192.168.1.5:1883 \
  -e l f aux1 aux2 cl_p salt t_a t_p sc \
  -sms "Very Low Salt" vls -sms "Inspect Cell" ic
```

Note the use of `--privileged` to guarantee access to the serial port! There are [other, better ways](https://stackoverflow.com/a/66427245/489116) to securely access serial ports within a container but the solution will vary with your hardware configuration. Also note that if you are using a network serial adapter (again, not recommended) you do not need `--privileged`.

It is also possible to use Docker Compose to set up a service, which adds secrets managment capability (e.g. for `AQUALOGIC_MQTT_PASSWORD`).

### Kubernetes

Here is a rudimentary example Kubernetes deployment YAML manifest (tested with [MicroK8s](https://microk8s.io)):

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: aqualogic
spec:
  replicas: 1
  selector:
    matchLabels:
      app: aqualogic
  template:
    metadata:
      labels:
        app: aqualogic
    spec:
      containers:
      - name: aqualogic
        image: ghcr.io/sphtkr/aqualogic_mqtt:latest
        imagePullPolicy: Always
        env:
          - name: "AQUALOGIC_MQTT_PASSWORD"
            valueFrom:
              secretKeyRef:
                name: aqualogic-secrets
                key: aqualogic_mqtt_password
        args: [ "-s", "/dev/ttyRS485",
                "-m", "mosquitto.default.svc.cluster.local:1883",
                "-e", "l", "f", "aux1", "aux2", "cl_p", "salt", "t_a", "t_p", "sc",
                "-sms", "Very Low Salt", "vls", 
                "-sms", "Inspect Cell", "ic" ]
        securityContext:
          privileged: true
        volumeMounts:
        - mountPath: "/dev/ttyRS485"
          name: ttyrs485
      volumes:
      - name: ttyrs485
        hostPath:
          path: /dev/ttyUSB0
          type: CharDevice
```
As in the Docker example above, note the use of `privileged: true`, which it is best practice to avoid! Again, this is not required if you are only using a network serial adapter.

Alternatively, using [Smarter Device Manager](https://community.arm.com/arm-research/b/articles/posts/a-smarter-device-manager-for-kubernetes-on-the-edge) or [Akri](https://docs.akri.sh) can let you more reliably use a serial port device without requiring privilege escalation. Here is an example deployment using Smarter Device Manager:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: aqualogic
spec:
  replicas: 1
  selector:
    matchLabels:
      app: aqualogic
  template:
    metadata:
      labels:
        app: aqualogic
    spec:
      containers:
      - name: aqualogic
        image: ghcr.io/sphtkr/aqualogic_mqtt:latest
        imagePullPolicy: Always
        resources:
          limits:
            smarter-devices/ttyUSB0: 1
          requests:
            smarter-devices/ttyUSB0: 1
        env:
          - name: "AQUALOGIC_MQTT_PASSWORD"
            valueFrom:
              secretKeyRef:
                name: aqualogic-secrets
                key: aqualogic_mqtt_password
        args: [ "-s", "/dev/ttyUSB0",
                "-m", "mosquitto.default.svc.cluster.local:1883",
                "-e", "l", "f", "aux1", "aux2", "cl_p", "salt", "t_a", "t_p", "sc",
                "-sms", "Very Low Salt", "vls", 
                "-sms", "Inspect Cell", "ic" ]
```

In both of these examples note the use of Kubernetes secret management for the `AQUALOGIC_MQTT_PASSWORD` variable, which is preferable to putting it directly in the YAML file's `env` block.

## Using with Home Assistant

Generally, if you already have Home Assistant's MQTT integration set up
and point this software to the same MQTT broker that Home Assistant is
using, then it will "Just Work," with Home Assistant picking up the
entities published into MQTT. If this doesn't happen, check the MQTT
configuration in your Home Assistant instance and make sure that
discovery is enabled and that the discovery prefix matches what you are
providing to this module.

> [!IMPORTANT]
> It's not yet possible to customize the birth message location and the birth message is used—so this must be set to `[prefix]/status` for now!

## Design Goals

This software is designed with the idea of running on a small SBC (e.g. Raspberry Pi Zero W) connected directly to the pool controller via a serial interface and RS485 adapter, and connected via WiFi to an MQTT broker. This should allow reliable control of the pool system wirelessly. It is likely possible to power the SBC via the 10V output from the controller, such as with [this RS485 HAT](https://www.amazon.com/gp/product/B0BKKXB9JJ/), though this combination has not yet been tested.

Ideally, it should be possible to get this package (or a version of it) 
to build with Micro Python and use an ESP32 or similar device instead
of a heavier-weight SBC--though I expect this will require refactoring
swilson's original module substantially. The architecture of this module
tries to keep this possibility in mind for the future.
