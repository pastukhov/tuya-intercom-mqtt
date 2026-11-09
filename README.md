# Tuya Intercom MQTT

**Русский** | [English](README.en.md)

Локальный Python-мост между Tuya-домофоном и вашим MQTT-брокером.
Получает события домофона по LAN, при звонке или обнаружении человека делает
снимок и публикует JPEG в MQTT. Также умеет сохранять фото/видео и отдавать
локальный HTTP-видеопоток. Работает самостоятельно, без Home Assistant.

На JVS GRANT HD WiFi поддерживается локальный IMM: настоящее HD 1280×720,
H.264 и звук PCM 8 кГц mono, преобразуемый в AAC 48 кГц mono для видео.
При заданном `TUYA_IMM_AUTH` все режимы выбирают IMM. Без него сохраняется
старый WebRTC-путь с `TUYA_RTC_AUTH`, SD и без звука.

## Что умеет и какие есть ограничения

| Режим | Результат |
| --- | --- |
| `mqtt` | События JSON, снимки JPEG в Base64, статус online/offline |
| `photo` | Один JPEG в указанный файл |
| `video` | H.264 MP4 с AAC при IMM, заданной длительности |
| `serve` | `/snapshot.jpg` и H.264 MPEG-TS `/stream.ts` через HTTP |

Мост читает события и запускает видеосеанс; команды открытия замка и управления
реле не реализованы. MQTT-команды и Home Assistant MQTT Discovery отсутствуют.
По умолчанию фото/видео запрашивают HD. Для старого WebRTC используйте
`--resolution sd`. IMM выбирает HD; звук передаётся в MP4 и HTTP MPEG-TS.

Передача изображения идёт по локальной сети. Однако первичная настройка требует
учётных данных Tuya, включая отдельный WebRTC `auth`. Мост не получает и не
обновляет их автоматически. Работа уже запущенного устройства без WAN проверялась,
но холодный старт при полностью отключённом интернете не обеспечен. Срок жизни
`auth` не установлен. Не блокируйте интернет домофону до проверки своего сценария.

## Что понадобится

- Linux-компьютер с Docker в сети домофона. Команды ниже рассчитаны на Linux
  Docker Engine с `--network host`; Docker Desktop отдельно не проверялся.
- Домофон, уже добавленный в Smart Life / Tuya Smart и работающий в приложении.
- Параметры устройства из следующего раздела.
- Доступный MQTT-брокер, например ваш Mosquitto. В этот образ брокер не входит.
- Доступ хоста к LAN TCP 6668 домофона и к UDP-трафику видеосеанса.
  Изоляция Wi-Fi-клиентов или фильтрация между VLAN может мешать соединению.

Для первого запуска закройте просмотр камеры в приложении и приостановите
другие интеграции, подключающиеся к тому же домофону: число LAN/видеосеансов
у устройства может быть ограничено.

## 1. Где взять параметры и секреты

| Параметр моста | Что это | Где получить |
| --- | --- | --- |
| `TUYA_DEVICE_ID` | Идентификатор устройства | Список Devices облачного проекта Tuya или поле `id` в TinyTuya `devices.json` |
| `TUYA_DEVICE_IP` | Локальный IPv4-адрес домофона | Список DHCP-клиентов роутера; желательно закрепить адрес |
| `TUYA_LOCAL_KEY` | LAN-ключ, ровно 16 байт | Поле `key` устройства в результате TinyTuya wizard |
| `TUYA_RTC_AUTH` | Авторизация старого WebRTC (не нужна для IMM) | Поле `result.auth` ответа WebRTC Configuration API Tuya |

`local_key`, WebRTC `auth`, облачный Access Secret и пароль MQTT — **разные значения**.
Пароль Smart Life не подходит ни вместо LAN-ключа, ни вместо `auth`.

### Device ID и Local Key

1. Войдите в [Tuya Developer Platform](https://platform.tuya.com/).
2. Создайте Cloud Development проект Smart Home с дата-центром, соответствующим
   региону аккаунта приложения. В проекте откройте **Devices → Link Tuya App Account**,
   добавьте аккаунт и подтвердите QR-код через Smart Life / Tuya Smart.
3. Проверьте, что домофон появился в Devices. Сохраните Device ID.
4. На Overview проекта найдите **Access ID / Client ID** и **Access Secret**.
   Они нужны для получения ключей, но не для постоянной работы моста.
5. В отдельной локальной папке запустите TinyTuya wizard:

```sh
mkdir -p "$HOME/.config/tuya-intercom-mqtt/provisioning"
chmod 700 "$HOME/.config/tuya-intercom-mqtt/provisioning"
cd "$HOME/.config/tuya-intercom-mqtt/provisioning"
umask 077
python3 -m venv .venv
.venv/bin/pip install tinytuya==1.20.0
.venv/bin/python -m tinytuya wizard
```

Введите запрошенные API Key (Access ID), API Secret, регион и Device ID.
В созданном `devices.json` найдите свой домофон: `id` соответствует
`TUYA_DEVICE_ID`, `key` — `TUYA_LOCAL_KEY`. Не публикуйте файлы wizard:
они содержат учётные данные. После повторного сопряжения устройства ключ может измениться.

Если список пуст или API отказывает, проверьте привязку аккаунта, дата-центр
и доступ проекта к необходимым API. Доступность сервисов зависит от аккаунта Tuya.
Подробная актуальная инструкция: [TinyTuya Setup Wizard](https://github.com/jasonacox/tinytuya#setup-wizard).

### WebRTC auth — только для старого транспорта

Этот раздел нужен только для WebRTC. Для HD и звука используйте IMM auth
по инструкции ниже; WebRTC auth в этом режиме не требуется.

TinyTuya wizard получает LAN-ключи; он не заполняет `TUYA_RTC_AUTH` для моста.
В Tuya API Explorer выберите тот же проект и API получения WebRTC Configuration.
Для устройства нужен ответ с непустым `result.auth`.

В исследовании этого проекта использован запрос:

```text
GET /v1.0/devices/{device_id}/webrtc-configs
```

В [официальной документации Tuya WebRTC](https://developer.tuya.com/en/docs/iot/webrtc?id=Kacsd4x2hl0se)
также описан вариант:

```text
GET /v1.0/users/{uId}/devices/{deviceId}/webrtc-configs
```

Выбирайте вариант, доступный вашему проекту. Во втором случае `uId` — UID
привязанного аккаунта приложения из проекта Tuya, а не адрес почты и не Access ID.
API Explorer выполняет подпись запроса; обычный неподписанный `curl` недостаточен.

Из успешного ответа перенесите **только значение `result.auth`** в
`TUYA_RTC_AUTH`, без JSON-кавычек и без всего объекта ответа. Не используйте
`access_token`, ICE password или MQTT password из ответа вместо `auth`.

Если API отсутствует, отвечает отказом или устройство не поддерживает WebRTC,
необходим доступ к соответствующему облачному сервису Tuya. Мост не обходит
эти ограничения. Для IMM вместо WebRTC auth задайте `TUYA_IMM_AUTH`.
Автоматическое продление `auth` не реализовано; при отказе видеосеанса проверьте
доступность камеры и получите актуальную конфигурацию повторно.

### Доступ к вашему MQTT-брокеру

`MQTT_HOST` — адрес вашего брокера, **не MQTT-сервер из WebRTC API Tuya**.
Логин и пароль выдаёт администратор брокера либо вы создаёте их в его конфигурации.
Пользователю моста нужны права публикации в топики событий, статуса и изображений,
описанные ниже. Для TLS задайте `MQTT_TLS=true` и подходящий порт брокера:
порт не меняется автоматически. Используется системное хранилище доверенных CA;
отключения проверки сертификата или отдельного CLI-параметра CA нет.

### Как получить IMM auth

Это отдельный 32-символьный шестнадцатеричный токен нативного приложения.
WebRTC API и TinyTuya wizard его не выдают. Проверенный способ — извлечение
из собственного полного LAN-сеанса Smart Life с известным LAN-ключом.

1. Подготовьте файл `device.env` с `TUYA_DEVICE_ID`, `TUYA_DEVICE_IP`,
   `TUYA_LOCAL_KEY`. Для извлечения WebRTC auth не нужен.
2. На роутере или зеркалируемом порту начните захват **всего TCP и UDP**
   между телефоном и домофоном, например:
   `tcpdump -i br-lan -s 0 -w session.pcap host CAMERA_IP`.
   Имя интерфейса зависит от роутера. Обычный захват на другом Wi-Fi-клиенте
   обычно не видит этот обмен.
3. Телефон подключите к той же LAN, отключите на нём VPN. Полностью закройте
   Smart Life, затем откройте приложение и просмотр домофона. Переключите HD,
   включите прослушивание, подождите 10–20 секунд и завершите захват.
   Нужен сеанс с самого начала: offer/answer, AES-ключи и нативный login.
4. На доверенном Linux-хосте установите TShark (`sudo apt install tshark`),
   установите этот пакет в venv и запустите офлайн-извлечение:

```sh
.venv/bin/python -m domofon_capture.provision_cli \
  --capture /secure/session.pcap --env-file /secure/device.env \
  --output /secure/imm.env
```

Выходной файл не должен существовать. Он создаётся с правами 0600 и содержит
`TUYA_IMM_AUTH=...`; секрет в терминал не выводится. Перенесите эту строку в
`bridge.env`. Захват содержит приватные медиа и ключи, храните его локально.
Если login не найден, повторите полный запуск приложения с захватом с начала;
облачный/relay-сеанс вместо прямого LAN для этого способа не подходит.
Срок жизни токена и переносимость между прошивками не установлены.

Для установки утилиты из корня репозитория:

```sh
python3 -m venv .venv
.venv/bin/pip install '.[mqtt]'
```

## 2. Куда положить секреты

Храните их на хосте вне Git-репозитория. Создайте файл, не перезаписывая уже имеющийся:

```sh
mkdir -p "$HOME/.config/tuya-intercom-mqtt"
chmod 700 "$HOME/.config/tuya-intercom-mqtt"
(umask 077; touch "$HOME/.config/tuya-intercom-mqtt/bridge.env")
chmod 600 "$HOME/.config/tuya-intercom-mqtt/bridge.env"
nano "$HOME/.config/tuya-intercom-mqtt/bridge.env"
```

Заполните его своими значениями (адреса ниже — только примеры):

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

Используйте строки `NAME=value`, без `export`, подстановок `$VARIABLE` и комментариев
после значения. Литералы `YOUR_...` необходимо заменить. Для показанного ниже
`docker --env-file` не оборачивайте значения в кавычки: Docker может передать их
как часть значения. Пробелы в начале/конце секрета не поддерживаются загрузчиком.

Этот файл **не копируется в образ**. Docker читает его при запуске контейнера.
Не передавайте секреты прямо в командной строке и не прикладывайте файл к issue.
Администратор Docker может прочитать окружение контейнера; считайте доступ к Docker
доступом к этим секретам.

## 3. Собрать и проверить Docker-образ

```sh
git clone https://github.com/pastukhov/tuya-intercom-mqtt.git
cd tuya-intercom-mqtt
docker build -t tuya-intercom-mqtt:local .
docker run --rm tuya-intercom-mqtt:local --help
```

Образ устанавливает Python-пакет с MQTT-зависимостью. Внутреннее имя пакета и
команды пока `domofon_capture` / `domofon-capture`; имя репозитория отличается.

Сначала проверьте один снимок. Команда ниже использует ваш UID/GID, чтобы файл
в каталоге хоста принадлежал вам:

```sh
mkdir -p output
chmod 700 output
docker run --rm --network host --user "$(id -u):$(id -g)" \
  --env-file "$HOME/.config/tuya-intercom-mqtt/bridge.env" \
  --mount "type=bind,src=$(pwd)/output,dst=/output" \
  tuya-intercom-mqtt:local photo --output /output/test.jpg
```

При успехе появится `output/test.jpg`, команда выведет JSON результата и завершится
с кодом 0. При повторном запуске выберите другое имя или добавьте `--overwrite`.
По умолчанию на начало захвата даётся 30 секунд; можно задать `--timeout 60`.

## 4. Запустить постоянный MQTT-мост

```sh
docker run -d --name tuya-intercom-mqtt \
  --restart unless-stopped --network host \
  --env-file "$HOME/.config/tuya-intercom-mqtt/bridge.env" \
  tuya-intercom-mqtt:local mqtt --source-id entrance
```

`entrance` — ваше логическое имя домофона: 1–64 символа `A–Z`, `a–z`, цифры,
`_` или `-`. Это не секрет и не Tuya Device ID. Для другого устройства создайте
отдельный файл окружения, контейнер и уникальный `--source-id`.

```sh
docker ps --filter name=tuya-intercom-mqtt
docker logs --tail 50 tuya-intercom-mqtt
docker stop tuya-intercom-mqtt
docker start tuya-intercom-mqtt
```

Отсутствие постоянных сообщений в `docker logs` нормально: CLI подавляет
подробные библиотечные логи. Работу проверяйте по MQTT-событиям и реальному звонку.
Статус `online` означает подключение моста к брокеру, а не гарантию доступности камеры.

При потере MQTT-соединения процесс завершится с ошибкой; политика Docker запустит
его заново. При обрыве LAN-подключения слушатель повторяет соединение с задержкой.
После изменения `bridge.env` контейнер нужно **пересоздать**: `docker restart`
не перечитает файл окружения. Выполните `docker rm -f tuya-intercom-mqtt`, затем
повторите команду `docker run` выше.

## MQTT: топики и формат сообщений

Для `--source-id entrance` и `MQTT_TOPIC_IN=intercom/+/image`:

| Топик | Содержимое | Retain |
| --- | --- | --- |
| `domofon/entrance/status` | `{"status":"online"}` или `{"status":"offline"}` | Да |
| `domofon/entrance/event` | Событие JSON | Нет |
| `intercom/entrance/image` | JPEG Base64 в JSON | Нет |

Все публикации используют QoS 1. Для статуса настроен Last Will `offline`.
QoS 1 допускает повторную доставку; для сопоставления события и снимка есть `event_id`.
Пример события (значения условные):

```json
{"event_type":"doorbell_ring","source":"entrance","received_at":"2026-01-01T12:00:00+00:00","panel":1,"event_id":"example-event-id"}
```

Снимок в отдельном сообщении:

```json
{"image_b64":"<base64 JPEG>","device":"entrance","event_id":"example-event-id"}
```

`image_b64` содержит обычный Base64 без префикса `data:image/jpeg;base64,`.
Брокер должен принимать сообщения с JPEG, увеличенным примерно на треть из-за Base64.

Распознаются `doorbell_ring`, `doorbell_idle`, `human_detected`, `doorbell_image`.
Автоматический новый снимок создаётся для звонка и обнаружения человека.
Наличие каждого события зависит от DPS конкретного устройства. Облачные URL
снимков в сообщения не включаются. Снимок снимается после события, поэтому не
гарантирует точное совпадение с моментом нажатия кнопки.

Если снимок не получен, в топик событий приходит `snapshot_unavailable` с исходным
`event_id` и причиной: `capture_failed`, `queue_full`, `snapshot_job_expired` или
`shutdown`. Очередь ограничена восемью заданиями; ожидавшие больше пяти секунд
задания отбрасываются. Это ограничивает накопление устаревших кадров.

`MQTT_TOPIC_IN` поддерживает подстановку `+` или `{source}` и фиксированный топик;
`#` запрещён. Если переменная не задана, используется совместимый с YAIC
топик `yaic/input/+/image`. Несмотря на название `IN`, это **выходной топик моста**,
входной для получателя изображений. Зависимости от сервиса YAIC нет.

Опция `--mqtt-topic-prefix home` добавит `home/` перед **всеми** топиками моста.
Префикс указывают без начального/конечного `/` и без MQTT wildcard.
Для проверки подпишитесь своим MQTT-клиентом на `domofon/entrance/#` и
`intercom/entrance/image`, затем нажмите кнопку звонка на панели.

### Настройка топиков

Каждый выходной топик настраивается отдельно через окружение:

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `MQTT_TOPIC_EVENT` | `domofon/{source}/event` | Звонки, обнаружение человека, ошибки снимков |
| `MQTT_TOPIC_STATUS` | `domofon/{source}/status` | Retained online/offline и Last Will |
| `MQTT_TOPIC_IMAGE` | `yaic/input/+/image` | JSON с JPEG Base64 |
| `MQTT_TOPIC_PREFIX` | пусто | Общий префикс всех трёх топиков |

`{source}` заменяется значением `--source-id`. Можно задать фиксированный топик
без подстановки. Старый `+` также заменяется source ID; это не wildcard подписки.
`MQTT_TOPIC_IMAGE` имеет приоритет над устаревшим `MQTT_TOPIC_IN`.
CLI `--mqtt-topic-prefix` имеет приоритет над `MQTT_TOPIC_PREFIX`.
Префикс добавляется также к фиксированным топикам.

Пример `bridge.env`:

```dotenv
MQTT_TOPIC_PREFIX=home
MQTT_TOPIC_EVENT=intercom/{source}/events
MQTT_TOPIC_STATUS=intercom/{source}/availability
MQTT_TOPIC_IMAGE=intercom/{source}/snapshot
```

С `--source-id entrance` мост публикует соответственно в
`home/intercom/entrance/events`, `home/intercom/entrance/availability` и
`home/intercom/entrance/snapshot`. Формат сообщений, QoS 1 и retain не меняются.
Все три итоговых топика должны различаться. Пустые топики, `#`, неизвестные
подстановки, NUL, пустые сегменты и начальный/конечный `/` отклоняются.
При смене status-топика старое retained-сообщение брокер автоматически не удалит;
удалите его своим MQTT-клиентом при необходимости. Обновите ACL и подписки
потребителей, затем пересоздайте контейнер для применения env-файла.

## Другие режимы

### Видео в файл

```sh
docker run --rm --network host --user "$(id -u):$(id -g)" \
  --env-file "$HOME/.config/tuya-intercom-mqtt/bridge.env" \
  --mount "type=bind,src=$(pwd)/output,dst=/output" \
  tuya-intercom-mqtt:local video --output /output/clip.mp4 --duration 10
```

Каталог `output` должен существовать. При IMM видео содержит AAC-аудио.

### Локальный HTTP-поток

```sh
docker run --rm --network host \
  --env-file "$HOME/.config/tuya-intercom-mqtt/bridge.env" \
  tuya-intercom-mqtt:local serve --bind 127.0.0.1 --port 19845
```

С хоста доступны `http://127.0.0.1:19845/snapshot.jpg` и
`http://127.0.0.1:19845/stream.ts`. Для видео нужен проигрыватель MPEG-TS, например
VLC; это не HTML-страница с плеером. До получения свежего кадра сервер отвечает 503.
HTTP-сервис не имеет встроенной авторизации/TLS. `--bind 0.0.0.0` откроет его
сетевым клиентам, поэтому используйте только доверенную сеть или защищённый прокси.
Режим `serve` сам по себе MQTT не публикует. Одновременные независимые видеосеансы
`serve` и `mqtt` могут конкурировать за камеру; сначала проверяйте режимы отдельно.

### Запуск без Docker

Нужен Python ≥3.12 и совместимые бинарные зависимости:

```sh
python3 -m venv .venv
.venv/bin/pip install '.[mqtt]'
.venv/bin/python -m domofon_capture mqtt --source-id entrance \
  --env-file "$HOME/.config/tuya-intercom-mqtt/bridge.env" \
  --mqtt-env-file "$HOME/.config/tuya-intercom-mqtt/bridge.env"
```

Здесь оба аргумента обязательны для единого файла: `--env-file` читает параметры
Tuya, а `--mqtt-env-file` — брокера. `--env-file` не экспортирует MQTT-переменные
в окружение. В Docker-примерах эту задачу выполняет `docker --env-file`.
Переменные процесса имеют приоритет над файлами.

Для фото/видео `--mqtt-env-file` не нужен. Есть совместимость с
`--device-config /path/device.json`, содержащим `id`, `ip`, `local_key`;
`TUYA_RTC_AUTH` всё равно задаётся отдельно. Приоритет параметров Tuya:
окружение → env-файл → JSON.

## Диагностика

| Симптом | Что проверить |
| --- | --- |
| `Missing required TUYA_...` | ID, IP, LAN-ключ и IMM auth либо WebRTC auth заданы и переданы именно контейнеру |
| `TUYA_LOCAL_KEY must be exactly 16 bytes` | Взято поле `key`, без кавычек/пробелов; это не Access Secret |
| `MQTT connection failed or timed out` | Адрес, порт, пароль, TLS, ACL брокера и сетевой доступ |
| `Capture failed` / `capture_failed` | IP, LAN-ключ, актуальность auth, работа камеры в приложении и конкурирующие подключения |
| MQTT online, но событий нет | Online отражает брокер; проверьте LAN TCP 6668 и совместимость DPS устройства |
| События есть, снимков нет | Проверьте `photo` отдельно; ищите `snapshot_unavailable` в топике событий |
| `Output already exists` | Другое имя файла либо явный `--overwrite` |
| Нет прав на файл | Каталог создан на хосте; для записи указан `--user` как в примере |
| HTTP 503 | Свежий кадр ещё не получен, камера недоступна либо превышен лимит видеоклиентов |
| После обновления env ничего не меняется | Пересоздайте контейнер; проверьте переменные окружения при запуске без Docker |
| Без интернета после перезагрузки камера не работает | Полностью автономный холодный старт не обеспечен; восстановите штатную связь устройства |

Для сообщения об ошибке укажите модель, прошивку, режим запуска и текст ошибки.
Замените секреты и идентификаторы заглушками; не прикладывайте `.env`,
`devices.json`, ответы WebRTC API или фотографии посетителей.

## Состав репозитория

`src/domofon_capture/` — приложение; `Dockerfile` и `.dockerignore` — сборка;
`pyproject.toml` — зависимости и консольная команда. Здесь нет приватных захватов,
исследовательских скриптов и сторонних checkout. `.gitignore` использует явный
список публикуемых файлов; добавление нового файла требует обновления этого списка.

## Проверка этой версии

Перенесённый IMM-транспорт покрыт регрессионными тестами. HD H.264 и AAC
проверены кодированием и полным декодированием внутри Docker Python 3.13.
Извлечение IMM auth проверено на реальном сохранённом LAN-захвате.
Повторная живая проверка самостоятельного моста пока не завершилась:
устройство закрыло LAN-сигнализацию до начала медиа. Предыдущая реализация
этого транспорта в Tuya Local была проверена на настоящем устройстве; это
не заменяет проверку нового Docker-моста в вашей сети.
