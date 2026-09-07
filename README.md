# Yandex Disk Backup

Python-библиотека, CLI `yd` / `yadisk` и приложение **«Яндекс Бэкап»** в строке меню macOS. Версия 0.3.1. Неофициальный клиент, не связан с Яндексом.

- Загрузка и скачивание файлов и папок, исключения, очередь из нескольких файлов.
- `ls`, `get`, `put`, `cp`, `mv`, `mkdir`, `rm`, размеры, свободное место и публичные ссылки.
- Резервные копии с проверкой содержимого и сохранением старых версий.
- Автоматическое копирование выбранной папки, пауза, ручной запуск и статус на Mac.
- Повтор сетевых операций при временном обрыве связи.

## 1. Установка CLI и Python-библиотеки

Нужен Python 3.10 или новее. Пакет пока не опубликован в PyPI. Установка из исходников в отдельное окружение:

```sh
git clone https://github.com/Patrik1352/yandex-disk-backup.git
cd yandex-disk-backup
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
yd --help
```

Эти команды предназначены для macOS/Linux. На Windows активируйте окружение командой `.venv\Scripts\Activate.ps1`. Автобэкап и приложение в строке меню предназначены для macOS; Python API и обычные команды CLI не требуют приложения.

Для доступной из любого терминала команды вместо виртуального окружения можно использовать установленный `pipx`: `pipx install .`. После установки через обычное окружение активируйте его в каждом новом терминале.

## 2. Получение ключей и токена Яндекс.Диска

Для операций с файлами нужен **OAuth access token**. Пароль Яндекса библиотеке не нужен. `Client ID` и `Client secret` необходимы только для получения/обновления токена через OAuth-команды.

1. Войдите в нужный аккаунт на [Яндекс OAuth](https://oauth.yandex.ru/).
2. Создайте [приложение для доступа к API](https://oauth.yandex.ru/client/new/), задайте название.
3. Добавьте права `cloud_api:disk.read` и `cloud_api:disk.write` — чтение и запись Яндекс.Диска.
4. Укажите Redirect URI: `https://oauth.yandex.ru/verification_code`.
5. Сохраните приложение. Его `Client ID` и `Client secret` находятся в свойствах приложения.
6. Для личного использования получите токен вручную: откройте следующий адрес, заменив `YOUR_CLIENT_ID` своим идентификатором, и разрешите доступ:

```text
https://oauth.yandex.ru/authorize?response_type=token&client_id=YOUR_CLIENT_ID
```

Скопируйте выданный access token в локальный файл настроек. Сам токен и URL после авторизации никому не отправляйте.

Официальные инструкции: [регистрация приложения](https://yandex.ru/dev/id/doc/ru/register-api), [получение токена вручную](https://yandex.ru/dev/id/doc/ru/tokens/debug-token).

### Хранение токена вне репозитория

```sh
mkdir -p "$HOME/.config/yandex-backup"
chmod 700 "$HOME/.config/yandex-backup"
# Выполняйте копирование один раз: не заменяйте уже настроенный файл.
cp -n .env.example "$HOME/.config/yandex-backup/.env"
chmod 600 "$HOME/.config/yandex-backup/.env"
```

Откройте этот `.env` в текстовом редакторе и заполните `YANDEX_ACCESS_TOKEN`. Не вставляйте токен в командную строку — так он не попадёт в историю shell. Остальные поля для готового токена можно оставить пустыми.

```sh
export YADISK_ENV_FILE="$HOME/.config/yandex-backup/.env"
yd auth check
yd df
yd ls disk:/
```

Чтобы не задавать путь каждый раз, добавьте строку `export YADISK_ENV_FILE=...` с путём, **без токена**, в `~/.zshrc` или `~/.bashrc`.

### Альтернатива: вход через CLI и обновление токена

Заполните `YANDEX_CLIENT_ID`, `YANDEX_CLIENT_SECRET` и `YANDEX_REDIRECT_URI` в том же `.env`. Затем:

```sh
yd auth login --browser --env-file "$HOME/.config/yandex-backup/.env" \
  --token-file "$HOME/.config/yandex-backup/disk-token.json"
```

После разрешения доступа скопируйте полный URL перенаправления из адресной строки браузера и вставьте в скрытый запрос CLI. Нужен URL с `code` и `state`, а не только код. CLI проверяет `state` и использует PKCE; токены сохраняются в JSON с ограниченными правами доступа.

```sh
yd --token-file "$HOME/.config/yandex-backup/disk-token.json" ls disk:/
yd auth refresh --env-file "$HOME/.config/yandex-backup/.env" \
  --token-file "$HOME/.config/yandex-backup/disk-token.json"
```

Refresh возможен, если Яндекс выдал refresh token. Автоматического обновления токена в фоне нет. **Служба macOS читает `YANDEX_ACCESS_TOKEN` из `.env`, а не JSON CLI**: для неё используйте ручной способ выше или перенесите access token из JSON в `.env` локальным редактором.

## 3. Основные команды

```sh
yd mkdir disk:/Example
yd put ./report.txt disk:/Example/report.txt
yd get disk:/Example/report.txt ./downloaded.txt
yd ls disk:/Example
yd cp disk:/Example/report.txt disk:/Example/copy.txt
yd mv disk:/Example/copy.txt disk:/Example/renamed.txt
yd share disk:/Example/report.txt
yd rm disk:/Example/renamed.txt
```

`rm` по умолчанию отправляет объект в корзину. Существующие файлы не перезаписываются без явного `--overwrite`. Для папок и дополнительных флагов смотрите `yd put --help`, `yd get --help` и [справочник API/CLI](docs/REFERENCE.md).

```sh
yd backup ./project disk:/Backups/project --exclude .env --exclude node_modules --workers 4
```

## 4. Установка приложения macOS

Нужны macOS 13+, Python 3.10+ и Apple Command Line Tools. Сборка создаёт приложение для архитектуры текущего Mac; проверено на Apple Silicon. Приложение подписано локальной ad-hoc подписью, не notarized Apple. При ограничении запуска используйте штатный диалог macOS для разрешения запуска знакомого приложения.

Если Command Line Tools ещё не установлены:

```sh
xcode-select --install
```

Из корня репозитория, после установки Python-окружения и настройки `.env`:

```sh
source .venv/bin/activate
python -m pip wheel --no-deps . --wheel-dir dist
bash macos/build_menu_app.sh
python macos/install.py \
  --wheel dist/yadisk_client_local-0.3.1-py3-none-any.whl \
  --app-bundle "macos/dist/Yandex Backup.app" \
  --env-file "$HOME/.config/yandex-backup/.env" \
  --source "$HOME/Desktop" \
  --remote-root "/Backups/MyMac/Desktop"
```

**Последняя команда устанавливает и сразу запускает копирование выбранной папки на ваш Диск.** Для установки без запуска добавьте `--no-start`; затем службы можно запустить через `launchctl bootstrap` с двумя созданными plist из `~/Library/LaunchAgents`.

Установщик создаёт:

- `~/Applications/Яндекс Бэкап.app` — приложение в строке меню.
- `~/Library/Application Support/YandexBackup/` — независимое Python-окружение, настройки и журнал.
- Два LaunchAgent `local.egor.yandexbackup.worker` и `local.egor.yandexbackup.menu` — запуск службы и меню при входе в macOS.

Разрешите доступ к рабочему столу, если macOS его запросит. Исходный `.env` должен оставаться по указанному пути. Повторная установка сохраняет настройки; папку и интервал можно менять в меню. Для открытия установленного клиента кнопкой «Открыть Яндекс.Диск» нужен [официальный клиент Яндекс.Диска](https://disk.yandex.ru/download).

По умолчанию проверка запускается через 15 минут после окончания предыдущего прохода, используются 4 потока. Во время сна Mac копирование не идёт. Закрытие значка **не останавливает** фоновую службу; используйте «Пауза».

## Как устроена резервная копия

- `current/` содержит последние сохранённые файлы.
- `history/<дата-и-id>/` содержит предыдущие версии изменённых файлов.
- `.staging/` — временные проверяемые загрузки; при неопределённом результате операции могут остаться для восстановления.

Локальное удаление не удаляет файл из облачной копии. Это односторонний бэкап, не двусторонняя синхронизация. История автоматически не очищается. Не изменяйте папку назначения параллельно другим приложением.

Файлы сравниваются по содержимому. При каждом проходе читаются локальные файлы; загружаются только отличающиеся. Первая копия множества маленьких файлов может быть медленной из-за запросов API. Очередь пока обрабатывает папки последовательно, `.git` сохраняется пофайлово; упаковка в архивы не реализована.

Исключены `.env`, `.env.*`, `.venv*`, `site-packages`, `node_modules`, кэши и некоторые файлы ключей; точный список — `DEFAULT_EXCLUDES` в `service.py`. Эти правила не распознают произвольные секреты внутри обычных файлов. Символические ссылки пропускаются. Бэкап не создаёт согласованный снимок работающих баз данных.

В меню видны обработанные файлы, переданные байты, скорость, последняя успешная копия. Общий процент и ETA неизвестны до окончания обхода, поэтому не показываются. Успешное завершение — **«Копирование завершено ✓»**, в строке меню — **«Готово»**.

Восстановление обычных файлов: `yd get --help` и рекурсивное скачивание `current/`; прошлую версию можно скачать из `history/`.

## Разработка и проверки

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

84 теста без доступа к настоящему Диску. Интеграционные проверки загрузки, версий и удаления локальных файлов выполнялись отдельно на временных данных. Длительная работа и сборка macOS Intel не проверены.

В репозитории есть только пустой `.env.example`. Не коммитьте реальные `.env`, JSON токенов, локальные настройки и журналы. Перед публикацией изменений проверяйте `git diff --cached`.
