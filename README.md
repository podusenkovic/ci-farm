# CI Farm

Простая распределённая CI-система для локальной сети. Позволяет запускать сборку проектов на удалённых машинах (slave) через SSH.

## Возможности

- Синхронизация проекта на удалённую машину через rsync
- Автоматическое определение команды сборки (Make, CMake, npm, Cargo, Go, Python)
- Стриминг логов сборки в реальном времени
- Управление несколькими slave-устройствами
- Lock-файлы для предотвращения параллельных сборок
- Глобальная и проектная конфигурация

## Установка

```bash
pip install -e .
```

Или для глобальной установки:

```bash
pip install git+https://github.com/vpodusenko/ci-farm.git
```

## Быстрый старт

### 1. Добавьте slave-устройство

```bash
ci add desktop 192.168.1.10 --user=myuser --key=~/.ssh/id_rsa
ci add raspberry 192.168.1.20 --user=pi --key=~/.ssh/id_rsa
```

### 2. Проверьте статус

```bash
ci status
```

```
┌─────────────┬────────────────────────┬───────────┬──────┐
│ Name        │ Host                   │ Status    │ Info │
├─────────────┼────────────────────────┼───────────┼──────┤
│ desktop     │ myuser@192.168.1.10:22 │ Available │      │
│ raspberry   │ pi@192.168.1.20:22     │ Available │      │
└─────────────┴────────────────────────┴───────────┴──────┘
```

### 3. Запустите сборку

```bash
# В директории проекта
ci build

# На конкретном slave
ci build --on=raspberry

# С кастомной командой
ci build --command="make clean && make"

# Автовыбор свободного slave
ci build --auto
```

## Конфигурация

### Глобальная конфигурация `~/.ci-farm.yaml`

Создаётся автоматически при добавлении slave через `ci add`.

```yaml
slaves:
  - name: desktop
    host: 192.168.1.10
    user: myuser
    port: 22
    key: ~/.ssh/id_rsa
    build_dir: /tmp/ci-farm-builds

  - name: raspberry
    host: 192.168.1.20
    user: pi
    port: 22
    key: ~/.ssh/id_rsa
    build_dir: /tmp/ci-farm-builds

default_slave: desktop
```

### Проектная конфигурация `.ci-farm.yaml`

Создайте в корне проекта для кастомных настроек:

```bash
ci init
```

```yaml
project:
  # Команда сборки (если не указана - автоопределение)
  build_command: "make -j4"

  # Команды перед синхронизацией (локально)
  pre_sync:
    - "git submodule update --init"

  # Команды после успешной сборки (на slave)
  post_build:
    - "make test"
    - "cp build/firmware.bin /mnt/release/"

  # Исключения при синхронизации
  exclude:
    - .git
    - __pycache__
    - node_modules
    - build
    - .venv

  # Таймаут сборки в секундах
  timeout: 3600
```

## Команды

| Команда | Описание |
|---------|----------|
| `ci build [path]` | Запустить сборку проекта |
| `ci status` | Показать статус всех slave |
| `ci add <name> <host>` | Добавить новый slave |
| `ci remove <name>` | Удалить slave |
| `ci init [path]` | Создать конфиг проекта |
| `ci config [path]` | Показать текущую конфигурацию |
| `ci unlock <name>` | Принудительно разблокировать slave |

### Опции `ci build`

| Опция | Описание |
|-------|----------|
| `--on`, `-o` | Выбрать конкретный slave |
| `--command`, `-c` | Переопределить команду сборки |
| `--auto`, `-a` | Автовыбор свободного slave |

### Опции `ci add`

| Опция | Описание |
|-------|----------|
| `--user`, `-u` | SSH пользователь (default: root) |
| `--port`, `-p` | SSH порт (default: 22) |
| `--key`, `-k` | Путь к SSH ключу |
| `--build-dir`, `-d` | Директория сборки на slave |
| `--force`, `-f` | Добавить даже если нет подключения |

## Автоопределение команды сборки

CI Farm автоматически определяет команду сборки по файлам в проекте:

| Файл | Команда |
|------|---------|
| `.ci/build.sh` | `bash .ci/build.sh` |
| `build.sh` | `bash build.sh` |
| `Makefile` | `make` |
| `CMakeLists.txt` | `cmake -B build && cmake --build build` |
| `package.json` | `npm install && npm run build` |
| `Cargo.toml` | `cargo build --release` |
| `go.mod` | `go build ./...` |
| `pyproject.toml` | `pip install -e . && python -m pytest` |

## Требования

- Python 3.8+
- rsync на локальной машине
- SSH доступ к slave-устройствам
- rsync на slave-устройствах

## Зависимости

- `paramiko` - SSH клиент
- `pyyaml` - парсинг конфигов
- `rich` - красивый вывод в терминал

## Лицензия

MIT
