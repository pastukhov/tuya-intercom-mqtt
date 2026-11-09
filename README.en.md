# Tuya Intercom MQTT

[Русский](README.md) | **English**

A standalone Python bridge from a Tuya intercom to your MQTT broker. It reads
LAN events, captures a JPEG on a doorbell ring or human detection, and publishes
it to MQTT. It also saves photos/video and provides a local HTTP video stream.
Home Assistant is not required.

The IMM transport supports native **1280×720 HD H.264 and audio** on the
JVS GRANT HD WiFi. PCM 8 kHz mono audio is converted to AAC 48 kHz mono.
Set `TUYA_IMM_AUTH` to select IMM in all modes. Without it, the legacy WebRTC
transport uses `TUYA_RTC_AUTH`, SD video and no audio.

**Validation status:** regression tests pass; HD H.264 and AAC were encoded and
fully decoded inside Docker with Python 3.13. IMM credential extraction was
verified against a real saved LAN capture. The new standalone bridge's latest
live device check did not reach media: the intercom closed LAN signalling.
The source transport was previously verified on hardware in Tuya Local, but
that does not establish a successful live run of this Docker bridge.

## Features and limits

| Command | Output |
| --- | --- |
| `mqtt` | JSON events, Base64 JPEG snapshots, online/offline status |
| `photo` | A JPEG file |
| `video` | H.264 MP4, with AAC audio when using IMM |
| `serve` | HTTP `/snapshot.jpg` and H.264/AAC MPEG-TS `/stream.ts` |

Photo/video default to HD. For legacy WebRTC, pass `--resolution sd`.
IMM selects HD. Door unlocking, relay control, incoming MQTT commands and
Home Assistant MQTT Discovery are not implemented. Compatibility with other
models and firmware has to be verified separately.

Media travels over the LAN. Initial provisioning requires Tuya credentials;
the bridge does not fetch or renew them automatically. Operation of an already
initialized device without WAN was tested, but fully offline cold startup is
not guaranteed. Authentication lifetime is unknown.

## Requirements

- A Linux Docker Engine host on the intercom's LAN. Examples use `--network host`;
  Docker Desktop has not been separately tested.
- An intercom already paired with Smart Life / Tuya Smart and working in that app.
- Device ID, local IPv4 address, LAN key, and IMM auth or legacy WebRTC auth.
- Your own MQTT broker, such as Mosquitto; the image does not include one.
- Network access to the intercom's TCP 6668 and media UDP traffic. Wi-Fi client
  isolation and VLAN firewall rules can prevent a connection.

For initial testing, close the app's live view and pause competing integrations.
The device may support only a limited number of LAN/media connections.

## 1. Obtain credentials

| Variable | Meaning | Source |
| --- | --- | --- |
| `TUYA_DEVICE_ID` | Device identifier | Tuya project's Devices list or TinyTuya `devices.json` field `id` |
| `TUYA_DEVICE_IP` | Local IPv4 address | Router DHCP leases; reserve a stable address |
| `TUYA_LOCAL_KEY` | Exactly 16-byte LAN key | TinyTuya wizard device field `key` |
| `TUYA_IMM_AUTH` | Native IMM login token, 32 hexadecimal characters | Extract from your own complete Smart Life LAN session as described below |
| `TUYA_RTC_AUTH` | Legacy WebRTC auth; unnecessary with IMM | WebRTC Configuration API response field `result.auth` |

The LAN key, IMM auth, WebRTC auth, cloud Access Secret and MQTT password are
different values. Your Smart Life password cannot replace them.

### Device ID and LAN key

1. Sign in to the [Tuya Developer Platform](https://platform.tuya.com/).
2. Create a Smart Home Cloud Development project in the data center matching
   your app account. Open **Devices → Link Tuya App Account** and link the account
   by confirming its QR code in Smart Life / Tuya Smart.
3. Confirm the intercom appears in Devices and note its Device ID.
4. Find the project's **Access ID / Client ID** and **Access Secret** on Overview.
   These are needed for provisioning, not normal bridge operation.
5. Run TinyTuya wizard in a separate private directory:

```sh
mkdir -p "$HOME/.config/tuya-intercom-mqtt/provisioning"
chmod 700 "$HOME/.config/tuya-intercom-mqtt/provisioning"
cd "$HOME/.config/tuya-intercom-mqtt/provisioning"
umask 077
python3 -m venv .venv
.venv/bin/pip install tinytuya==1.20.0
.venv/bin/python -m tinytuya wizard
```

Enter the requested API Key (Access ID), API Secret, region and Device ID.
Find your device in the generated `devices.json`: use `id` for `TUYA_DEVICE_ID`
and `key` for `TUYA_LOCAL_KEY`. Wizard files contain credentials; keep them private.
Re-pairing may change the local key.

If devices are missing or requests fail, check account linking, data center and
project API permissions. Service availability depends on your Tuya account.
See the [TinyTuya setup instructions](https://github.com/jasonacox/tinytuya#setup-wizard).

### IMM auth for HD and audio

This is a separate 32-character hexadecimal native-app login token. Neither
TinyTuya wizard nor the investigated WebRTC API returns it. The verified method
extracts it from your own complete direct LAN session using the known LAN key.

1. Create a private `device.env` containing `TUYA_DEVICE_ID`, `TUYA_DEVICE_IP`
   and `TUYA_LOCAL_KEY`. WebRTC auth is not needed for extraction.
2. Capture **all TCP and UDP** between the phone and intercom on your router or
   a mirrored port, for example:
   `tcpdump -i br-lan -s 0 -w session.pcap host CAMERA_IP`.
   Replace the interface and camera IP. An ordinary capture on another Wi-Fi
   client usually cannot see this traffic.
3. Connect the phone to the same LAN and disable its VPN. Fully close Smart Life,
   then reopen it and start the intercom's live view. Select HD, enable listening,
   wait 10–20 seconds, then stop the capture. It must include the session start:
   offer/answer, AES keys and native login.
4. On a trusted Linux host, install TShark (`sudo apt install tshark`). Install
   this package from the repository root:

```sh
python3 -m venv .venv
.venv/bin/pip install '.[mqtt]'
```

5. Run offline extraction:

```sh
.venv/bin/python -m domofon_capture.provision_cli \
  --capture /secure/session.pcap --env-file /secure/device.env \
  --output /secure/imm.env
```

The output must not already exist. It is created with mode 0600 and contains
`TUYA_IMM_AUTH=...`; the token is not printed. Copy this line into `bridge.env`.
The capture contains private media and keys: retain it locally.

If no login is found, repeat capture from before the app starts. A cloud/relay
session does not substitute for the required direct LAN session. Token lifetime
and portability across firmware versions are not established.

### Legacy WebRTC auth

Skip this section when using IMM. TinyTuya wizard does not populate WebRTC auth.
In Tuya API Explorer, select the same project and its WebRTC Configuration API.
The request used during this project's investigation was:

```text
GET /v1.0/devices/{device_id}/webrtc-configs
```

The [official Tuya WebRTC documentation](https://developer.tuya.com/en/docs/iot/webrtc?id=Kacsd4x2hl0se)
also describes:

```text
GET /v1.0/users/{uId}/devices/{deviceId}/webrtc-configs
```

Use the endpoint available to your project. `uId` is the linked app account UID,
not an email address or Access ID. API Explorer signs requests; an unsigned
`curl` request is insufficient.

Copy only the successful response's `result.auth` value into `TUYA_RTC_AUTH`,
without JSON quotes. Do not substitute `access_token`, ICE password or the cloud
MQTT password. Missing APIs or permission errors require the corresponding Tuya
service access. The bridge does not renew auth automatically.

### Your MQTT broker

`MQTT_HOST` is **your broker**, not the server returned by Tuya WebRTC APIs.
Obtain credentials from your broker administrator or configure them yourself.
The bridge user needs publish permission for the topics below.

For TLS set `MQTT_TLS=true` and explicitly choose the broker's TLS port; the
port does not change automatically. Verification uses the system CA store.
There is no CLI option to disable certificate verification or select a CA file.

## 2. Store secrets outside the repository

Create a private host file without replacing any existing contents:

```sh
mkdir -p "$HOME/.config/tuya-intercom-mqtt"
chmod 700 "$HOME/.config/tuya-intercom-mqtt"
(umask 077; touch "$HOME/.config/tuya-intercom-mqtt/bridge.env")
chmod 600 "$HOME/.config/tuya-intercom-mqtt/bridge.env"
nano "$HOME/.config/tuya-intercom-mqtt/bridge.env"
```

Replace every placeholder with your values; addresses below are examples:

```dotenv
TUYA_DEVICE_ID=YOUR_DEVICE_ID
TUYA_DEVICE_IP=192.168.1.50
TUYA_LOCAL_KEY=YOUR_16_BYTE_KEY
TUYA_IMM_AUTH=YOUR_32_HEX_IMM_AUTH
MQTT_HOST=192.168.1.10
MQTT_PORT=1883
MQTT_USERNAME=intercom
MQTT_PASSWORD=YOUR_BROKER_PASSWORD
MQTT_TLS=false
MQTT_TOPIC_IN=intercom/+/image
```

Use `NAME=value`, without `export`, variable interpolation or inline comments.
For Docker `--env-file`, do not quote values: quotes may become part of the value.
Leading/trailing whitespace in secrets is not supported by the loader.

Secrets are not copied into the image. Docker reads this file when creating the
container. Keep it out of issues and command-line arguments. Anyone with Docker
administrative access can inspect the container environment.

## 3. Build and check Docker

```sh
git clone https://github.com/pastukhov/tuya-intercom-mqtt.git
cd tuya-intercom-mqtt
docker build -t tuya-intercom-mqtt:local .
docker run --rm tuya-intercom-mqtt:local --help
```

The image includes MQTT dependencies. The internal package/command names remain
`domofon_capture` / `domofon-capture`.

Test one snapshot first. Your host UID/GID ensures you own the output:

```sh
mkdir -p output
chmod 700 output
docker run --rm --network host --user "$(id -u):$(id -g)" \
  --env-file "$HOME/.config/tuya-intercom-mqtt/bridge.env" \
  --mount "type=bind,src=$(pwd)/output,dst=/output" \
  tuya-intercom-mqtt:local photo --output /output/test.jpg
```

Success creates `output/test.jpg`, prints a JSON result and exits with code 0.
For another run, change the name or add `--overwrite`. Startup timeout defaults
to 30 seconds; use `--timeout 60` to request a longer bound.

## 4. Run the MQTT bridge

```sh
docker run -d --name tuya-intercom-mqtt \
  --restart unless-stopped --network host \
  --env-file "$HOME/.config/tuya-intercom-mqtt/bridge.env" \
  tuya-intercom-mqtt:local mqtt --source-id entrance
```

`entrance` is your logical source name, not the Tuya Device ID. Use 1–64 ASCII
letters, digits, underscores or hyphens. Each device needs its own configuration,
container and unique source ID.

```sh
docker ps --filter name=tuya-intercom-mqtt
docker logs --tail 50 tuya-intercom-mqtt
docker stop tuya-intercom-mqtt
docker start tuya-intercom-mqtt
```

Quiet logs are normal: detailed library logs are suppressed. Verify operation
using MQTT events and a real doorbell ring. `online` indicates broker connectivity,
not camera readiness.

Loss of MQTT connectivity terminates the process; Docker's restart policy starts
it again. LAN disconnections trigger listener retries. After editing `bridge.env`,
**recreate** the container: `docker restart` does not reread the file. Run
`docker rm -f tuya-intercom-mqtt`, then repeat the `docker run` command.

## MQTT topics and payloads

With `--source-id entrance` and `MQTT_TOPIC_IN=intercom/+/image`:

| Topic | Payload | Retained |
| --- | --- | --- |
| `domofon/entrance/status` | `{"status":"online"}` / `{"status":"offline"}` | Yes |
| `domofon/entrance/event` | JSON event | No |
| `intercom/entrance/image` | Base64 JPEG in JSON | No |

All publications use QoS 1. Status has an `offline` Last Will. QoS 1 can deliver
duplicates; use `event_id` to correlate an event and its image.

```json
{"event_type":"doorbell_ring","source":"entrance","received_at":"2026-01-01T12:00:00+00:00","panel":1,"event_id":"example-event-id"}
```

The corresponding image:

```json
{"image_b64":"<base64 JPEG>","device":"entrance","event_id":"example-event-id"}
```

`image_b64` has no `data:image/jpeg;base64,` prefix. Configure broker message-size
limits for JPEG data enlarged by approximately one third by Base64.

Recognized events: `doorbell_ring`, `doorbell_idle`, `human_detected`,
`doorbell_image`. Rings and human detection trigger a new local snapshot.
Available events depend on device DPS. Cloud snapshot URLs are omitted.
A snapshot is taken after the event, so it may differ from the exact ring moment.

Failed snapshots publish `snapshot_unavailable` to the event topic with the
original `event_id` and a reason: `capture_failed`, `queue_full`,
`snapshot_job_expired` or `shutdown`. The queue holds eight jobs; jobs waiting
longer than five seconds are discarded.

`MQTT_TOPIC_IN` supports `+`, `{source}` or a fixed topic; `#` is forbidden.
The source ID replaces `+`. Its default is `yaic/input/+/image` for YAIC
compatibility; despite `IN`, this is the bridge's output topic. YAIC is optional.

`--mqtt-topic-prefix home` prepends `home/` to all bridge topics. Do not include
leading/trailing slashes or wildcards in the prefix. To test, subscribe to
`domofon/entrance/#` and `intercom/entrance/image`, then ring the panel.

### Topic configuration

Configure each outgoing topic independently through the environment:

| Variable | Default | Purpose |
| --- | --- | --- |
| `MQTT_TOPIC_EVENT` | `domofon/{source}/event` | Rings, human detection and snapshot failures |
| `MQTT_TOPIC_STATUS` | `domofon/{source}/status` | Retained availability and Last Will |
| `MQTT_TOPIC_IMAGE` | `yaic/input/+/image` | JSON containing a Base64 JPEG |
| `MQTT_TOPIC_PREFIX` | empty | Prefix applied to all three topics |

`{source}` expands to `--source-id`. Fixed topics without a placeholder are
supported. Legacy `+` also expands to the source ID; it is not a subscription
wildcard. `MQTT_TOPIC_IMAGE` overrides legacy `MQTT_TOPIC_IN`. The CLI option
`--mqtt-topic-prefix` overrides `MQTT_TOPIC_PREFIX`. Prefixes apply to fixed topics too.

Example `bridge.env`:

```dotenv
MQTT_TOPIC_PREFIX=home
MQTT_TOPIC_EVENT=intercom/{source}/events
MQTT_TOPIC_STATUS=intercom/{source}/availability
MQTT_TOPIC_IMAGE=intercom/{source}/snapshot
```

With `--source-id entrance`, the resulting topics are
`home/intercom/entrance/events`, `home/intercom/entrance/availability` and
`home/intercom/entrance/snapshot`. Payloads, QoS 1 and retain behavior are unchanged.
The three resolved topics must differ. Empty topics, `#`, unknown placeholders,
NUL, empty segments and leading/trailing slashes are rejected.
Changing the status topic does not remove the broker's old retained message;
clear it with your MQTT client if needed. Update broker ACLs and consumer
subscriptions, then recreate the container to reload its env file.

## Other modes

### Save video

```sh
docker run --rm --network host --user "$(id -u):$(id -g)" \
  --env-file "$HOME/.config/tuya-intercom-mqtt/bridge.env" \
  --mount "type=bind,src=$(pwd)/output,dst=/output" \
  tuya-intercom-mqtt:local video --output /output/clip.mp4 --duration 10
```

Create `output` first. IMM video includes AAC audio.

### HTTP stream

```sh
docker run --rm --network host \
  --env-file "$HOME/.config/tuya-intercom-mqtt/bridge.env" \
  tuya-intercom-mqtt:local serve --bind 127.0.0.1 --port 19845
```

Access `http://127.0.0.1:19845/snapshot.jpg` and
`http://127.0.0.1:19845/stream.ts` from the host. Use an MPEG-TS player such as
VLC for video. HTTP returns 503 until a fresh frame is available.
There is no built-in HTTP authentication or TLS. Binding to `0.0.0.0` exposes
the service to network clients; use a trusted network or protected proxy.
`serve` does not publish MQTT. Separate `serve` and `mqtt` media sessions can
compete for camera access; test them separately first.

### Run Python directly

Python ≥3.12 and compatible binary dependencies are required:

```sh
python3 -m venv .venv
.venv/bin/pip install '.[mqtt]'
.venv/bin/python -m domofon_capture mqtt --source-id entrance \
  --env-file "$HOME/.config/tuya-intercom-mqtt/bridge.env" \
  --mqtt-env-file "$HOME/.config/tuya-intercom-mqtt/bridge.env"
```

For a shared file, specify both arguments: `--env-file` loads Tuya settings,
`--mqtt-env-file` loads broker settings. The first does not export MQTT values
into the process environment. Docker examples use `docker --env-file` for that.
Process environment values override files.

Photo/video do not need `--mqtt-env-file`. Legacy `--device-config /path/device.json`
accepts `id`, `ip`, `local_key`; provide IMM or WebRTC auth separately.
Tuya configuration precedence is environment → env file → JSON.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Missing required configuration | Device ID, IP, LAN key and IMM or WebRTC auth reach the container |
| Local key must be exactly 16 bytes | Use device `key`, without quotes/whitespace, not Access Secret |
| MQTT connection failed or timed out | Broker host, port, password, TLS, ACL and network |
| `Capture failed` / `capture_failed` | IP, local key, auth validity, app camera access and competing sessions |
| MQTT online but no events | Online only reflects broker access; check TCP 6668 and device DPS compatibility |
| Events but no images | Run `photo` separately; inspect `snapshot_unavailable` events |
| `Output already exists` | Choose another name or use `--overwrite` |
| Output permission denied | Create the host directory and use `--user` as shown |
| HTTP 503 | No fresh frame, unavailable camera or streaming client limit |
| Configuration changes ignored | Recreate the container; check environment overrides for direct Python |
| Camera fails after reboot without WAN | Fully offline cold startup is not guaranteed; restore normal device connectivity |

When reporting a problem, include model, firmware, command mode and error text.
Redact credentials and identifiers. Do not attach env files, `devices.json`,
WebRTC API responses or visitor images.

## Repository contents

`src/domofon_capture/` contains the application; `Dockerfile` and `.dockerignore`
control the image; `pyproject.toml` defines dependencies and the console command.
The adapted IMM helpers include their MIT attribution in `imm_sdk_license.txt`.
Private captures, research scripts and third-party checkouts are excluded.
`.gitignore` allows an explicit list of published files; update it when adding files.
