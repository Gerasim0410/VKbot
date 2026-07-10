import json
import os
import re
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Any, Dict, Optional

import vk_api
from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll
from vk_api.utils import get_random_id

# ========= НАСТРОЙКИ =========

BASE_DIR = Path(__file__).resolve().parent


def load_env_file(path: Path) -> None:
    """Минимальная загрузка .env без отдельной зависимости."""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        os.environ.setdefault(key, value)


load_env_file(BASE_DIR / ".env")

VK_GROUP_TOKEN = os.getenv("VK_GROUP_TOKEN", "").strip()
VK_GROUP_ID = int(os.getenv("VK_GROUP_ID", "0") or 0)


def parse_int_list(value: str) -> list[int]:
    result: list[int] = []

    for item in re.split(r"[\s,;]+", value.strip()):
        if item and item.lstrip("-").isdigit():
            result.append(int(item))

    return result


# ВАЖНО: сюда нужны именно VK ID администраторов, а не Telegram ID.
ADMIN_IDS = parse_int_list(os.getenv("VK_ADMIN_IDS", ""))

DATA_FILE = BASE_DIR / os.getenv("DATA_FILE", "data.json")

DATES = ["9 июля", "10 июля", "11 июля"] 
CURRENT_YEAR = int(os.getenv("CURRENT_YEAR", "2026") or 2026) 

TIMES_BY_DATE = { 
    "9 июля": [ 
        "9:00", "9:30", "10:00", "10:30", "11:00", "11:30", "12:00", "12:30", 
        "13:00", "13:30", "14:00", "14:30", "15:30", "16:00", "17:00", "17:30", "18:00",
    ], 
    "10 июля": [ 
        "8:30", "9:00", "9:30", "10:00", "10:30", "11:00", "11:30", "12:00", "12:30", "13:00", 
        "13:30", "14:00", "14:30", "15:30", "16:00", "17:00",
    ], 
    "11 июля": [ 
        "9:00", "9:30", "10:00", "10:30", "11:00", "11:30", "12:00", "12:30", "13:00", "13:30", "14:00", 
    ], 
}

MAX_PEOPLE = 20
MAX_VK_MESSAGE = 4000

# Лимиты клавиатуры VK: держим inline-клавиатуру маленькой.
# На практике VK может отклонять inline-клавиатуру, если в ней слишком много кнопок,
# поэтому показываем по 5 времён на страницу + кнопки навигации.
TIME_BUTTONS_PER_ROW = 3
TIME_PAGE_SIZE = 5

# Простая память состояний.
# После перезапуска бот не помнит незавершённые диалоги,
# но сохранённые записи остаются в data.json.
sessions: Dict[int, Dict[str, Any]] = {}

vk = None

# ========= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =========

MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}


def parse_slot_datetime(date_str: str, time_str: str = "00:00") -> datetime:
    """Преобразует строки вроде '9 июля' и '8:30' в datetime."""
    parts = date_str.strip().lower().split()

    if len(parts) < 2:
        raise ValueError(f"Некорректная дата: {date_str}")

    day = int(parts[0])
    month = MONTHS[parts[1]]
    time_obj = datetime.strptime(time_str, "%H:%M").time()

    return datetime.combine(datetime(CURRENT_YEAR, month, day).date(), time_obj)


def is_time_passed(date_str: str, time_str: str) -> bool:
    """Проверяет, прошло ли уже время слота."""
    try:
        return datetime.now() > parse_slot_datetime(date_str, time_str)
    except Exception:
        return False


def save_json(path: Path, data: dict) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_json(path: Path) -> dict:
    """Загружаем данные и сразу чистим прошедшие слоты."""
    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {}

    cleaned_data = {}

    for uid, info in data.items():
        date = info.get("date")
        time = info.get("time")

        if date and time and not is_time_passed(date, time):
            cleaned_data[uid] = info

    if cleaned_data != data:
        save_json(path, cleaned_data)

    return cleaned_data


def load_data() -> dict:
    return load_json(DATA_FILE)


def save_data(data: dict) -> None:
    save_json(DATA_FILE, data)


def get_free_times(date: str, data: dict) -> list[str]:
    result = []

    for time_value in TIMES_BY_DATE.get(date, []):
        if is_time_passed(date, time_value):
            continue

        count = sum(
            1
            for value in data.values()
            if value["date"] == date and value["time"] == time_value
        )

        if count < MAX_PEOPLE:
            result.append(time_value)

    return result


def chunked(values: list[str], size: int):
    iterator = iter(values)
    return iter(lambda: list(islice(iterator, size)), [])


def payload_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def callback_button(label: str, payload: dict, color: str = "secondary") -> dict:
    return {
        "action": {
            "type": "callback",
            "label": label,
            "payload": payload_json(payload),
        },
        "color": color,
    }


def text_button(label: str, color: str = "secondary") -> dict:
    return {
        "action": {
            "type": "text",
            "label": label,
            "payload": payload_json({"cmd": label}),
        },
        "color": color,
    }


def keyboard_json(
    buttons: list[list[dict]],
    inline: bool = True,
    one_time: bool = False,
) -> str:
    return json.dumps(
        {
            "one_time": one_time,
            "inline": inline,
            "buttons": buttons,
        },
        ensure_ascii=False,
    )


def empty_keyboard() -> str:
    return keyboard_json([], inline=True)


def main_keyboard() -> str:
    return keyboard_json(
        [
            [text_button("Запись", "primary")],
            [text_button("Удаление записи", "negative")],
            [text_button("/admin", "secondary")],
        ],
        inline=False,
    )


def get_time_page_bounds(
    date_index: int,
    data: dict,
    page: int = 0,
) -> tuple[str, list[str], int, int]:
    date = DATES[date_index]
    free_times = get_free_times(date, data)

    max_page = max(0, (len(free_times) - 1) // TIME_PAGE_SIZE) if free_times else 0
    page = min(max(page, 0), max_page)

    return date, free_times, page, max_page


def slot_prompt(date_index: int, data: dict, page: int = 0) -> str:
    date, free_times, page, max_page = get_time_page_bounds(date_index, data, page)

    text = f"Выберите время для Ментального ГТО.\nДата: {date}"

    if free_times and max_page > 0:
        text += f"\nСтраница времени: {page + 1}/{max_page + 1}"

    return text


def generate_date_keyboard(date_index: int, data: dict, page: int = 0) -> str:
    date, free_times, page, max_page = get_time_page_bounds(date_index, data, page)

    buttons: list[list[dict]] = []

    # Не добавляем отдельную кнопку с названием даты:
    # она считается лишней кнопкой, а VK может отклонить inline-клавиатуру.
    date_nav = []

    if date_index > 0:
        date_nav.append(callback_button("◀️ дата", {"cmd": "date_prev"}))

    if date_index < len(DATES) - 1:
        date_nav.append(callback_button("дата ▶️", {"cmd": "date_next"}))

    if date_nav:
        buttons.append(date_nav)

    if free_times:
        start = page * TIME_PAGE_SIZE
        page_times = free_times[start:start + TIME_PAGE_SIZE]

        for row in chunked(page_times, TIME_BUTTONS_PER_ROW):
            buttons.append(
                [
                    callback_button(
                        time_value,
                        {
                            "cmd": "slot",
                            "date": date,
                            "time": time_value,
                        },
                        "primary",
                    )
                    for time_value in row
                ]
            )

        time_nav = []

        if max_page > 0:
            if page > 0:
                time_nav.append(
                    callback_button(
                        "◀️ время",
                        {
                            "cmd": "time_page",
                            "page": page - 1,
                        },
                    )
                )

            if page < max_page:
                time_nav.append(
                    callback_button(
                        "ещё время ▶️",
                        {
                            "cmd": "time_page",
                            "page": page + 1,
                        },
                    )
                )

        if time_nav:
            buttons.append(time_nav)

    else:
        buttons.append([callback_button("Слоты закончились", {"cmd": "noop"})])

    return keyboard_json(buttons, inline=True)


def get_current_date_index() -> int:
    """Определяет индекс текущей даты или ближайшей следующей."""
    try:
        current_date = datetime.now().date()

        for i, date_str in enumerate(DATES):
            date_obj = parse_slot_datetime(date_str).date()

            if date_obj >= current_date:
                return i

        return len(DATES) - 1

    except Exception:
        return 0


def get_field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)

    return getattr(obj, key, default)


def parse_payload(payload: Any) -> dict:
    if isinstance(payload, dict):
        return payload

    if isinstance(payload, str):
        try:
            loaded = json.loads(payload)
            return loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            return {}

    return {}


def send_message(
    peer_id: int,
    text: str,
    keyboard: Optional[str] = None,
) -> None:
    params = {
        "peer_id": peer_id,
        "message": text,
        "random_id": get_random_id(),
    }

    if keyboard is not None:
        params["keyboard"] = keyboard

    vk.messages.send(**params)


def edit_or_send(
    peer_id: int,
    conversation_message_id: Optional[int],
    text: str,
    keyboard: Optional[str] = None,
) -> None:
    if conversation_message_id:
        try:
            params = {
                "peer_id": peer_id,
                "conversation_message_id": conversation_message_id,
                "message": text,
                "keyboard": keyboard if keyboard is not None else empty_keyboard(),
            }

            vk.messages.edit(**params)
            return

        except Exception:
            pass

    send_message(peer_id, text, keyboard)


def send_event_answer(
    event_id: Optional[str],
    user_id: int,
    peer_id: int,
    text: str,
) -> None:
    if not event_id:
        return

    try:
        vk.messages.sendMessageEventAnswer(
            event_id=event_id,
            user_id=user_id,
            peer_id=peer_id,
            event_data=json.dumps(
                {
                    "type": "show_snackbar",
                    "text": text,
                },
                ensure_ascii=False,
            ),
        )

    except Exception:
        pass


def send_long_message(peer_id: int, text: str) -> None:
    if not text:
        return

    lines = text.split("\n")
    chunk = ""

    for line in lines:
        if len(chunk) + len(line) + 1 > MAX_VK_MESSAGE:
            send_message(peer_id, chunk)
            chunk = line
        else:
            chunk = f"{chunk}\n{line}" if chunk else line

    if chunk:
        send_message(peer_id, chunk)


def clear_session(user_id: int) -> None:
    sessions.pop(user_id, None)


def set_session(user_id: int, state: str, **data: Any) -> None:
    sessions[user_id] = {
        "state": state,
        **data,
    }


def get_session(user_id: int) -> dict:
    return sessions.get(user_id, {})


def normalize_command(text: str) -> str:
    return text.strip().lower()


# ========= ЛОГИКА КОМАНД =========

def cmd_start(peer_id: int, user_id: int) -> None:
    clear_session(user_id)

    send_message(
        peer_id,
        "Бот запущен. Используйте кнопки «Запись» или «Удаление записи».",
        keyboard=main_keyboard(),
    )


def cmd_record(peer_id: int, user_id: int) -> None:
    send_message(peer_id, "Введите ваш ID на мероприятии:")
    set_session(user_id, "booking_waiting_for_id")


def cmd_del_record(peer_id: int, user_id: int) -> None:
    send_message(peer_id, "Введите ваш ID для удаления записи на Ментальное ГТО:")
    set_session(user_id, "delete_waiting_for_id")


def format_records(title: str, data: dict) -> str:
    lines = [title]

    for date in DATES:
        for time_value in TIMES_BY_DATE.get(date, []):
            ids = [
                uid
                for uid, info in data.items()
                if info["date"] == date and info["time"] == time_value
            ]

            if ids:
                lines.append(f"\n{date} {time_value}")
                lines.extend(f"• {uid}" for uid in ids)

    return "\n".join(lines)


def cmd_admin(peer_id: int, user_id: int) -> None:
    if user_id not in ADMIN_IDS:
        send_message(peer_id, "У вас нет доступа.")
        return

    send_message(peer_id, "Админ-панель открыта. Собираю данные...")

    data = load_data()

    if not data:
        send_message(peer_id, "Нет записей на Ментальное ГТО.")
    else:
        send_long_message(
            peer_id,
            format_records("Записи на Ментальное ГТО:", data),
        )


def handle_booking_id(peer_id: int, user_id: int, text: str) -> None:
    event_id = text.strip()

    if not event_id.isdigit():
        send_message(peer_id, "ID должен состоять только из цифр.")
        return

    data = load_data()

    if event_id in data:
        send_message(
            peer_id,
            f"Вы уже записаны на {data[event_id]['date']} в {data[event_id]['time']}",
        )
        clear_session(user_id)
        return

    start_date_index = get_current_date_index()

    set_session(
        user_id,
        "booking_choosing_slot",
        event_id=event_id,
        date_index=start_date_index,
        time_page=0,
    )

    keyboard = generate_date_keyboard(start_date_index, data, page=0)

    send_message(
        peer_id,
        slot_prompt(start_date_index, data, page=0),
        keyboard=keyboard,
    )


def handle_delete_id(peer_id: int, user_id: int, text: str) -> None:
    delete_id = text.strip()

    if not delete_id.isdigit():
        send_message(peer_id, "ID должен состоять только из цифр.")
        return

    data = load_data()

    if delete_id in data:
        del data[delete_id]
        save_data(data)

        send_message(
            peer_id,
            f"Запись на Ментальное ГТО для ID {delete_id} удалена.",
        )

    else:
        send_message(
            peer_id,
            "Такой ID не найден в записях на Ментальное ГТО.",
        )

    clear_session(user_id)


def handle_message(peer_id: int, user_id: int, text: str) -> None:
    text = text.strip()

    if not text:
        return

    normalized_text = normalize_command(text)
    command = normalized_text.split()[0]

    if command in {"/start", "начать", "старт"}:
        cmd_start(peer_id, user_id)
        return

    if command in {"/record", "запись"}:
        cmd_record(peer_id, user_id)
        return

    if normalized_text in {"/del_record", "удаление записи"}:
        cmd_del_record(peer_id, user_id)
        return

    if command == "/admin":
        cmd_admin(peer_id, user_id)
        return

    session = get_session(user_id)
    state = session.get("state")

    if state == "booking_waiting_for_id":
        handle_booking_id(peer_id, user_id, text)

    elif state == "delete_waiting_for_id":
        handle_delete_id(peer_id, user_id, text)

    else:
        send_message(
            peer_id,
            "Не понял команду. Используйте кнопки «Запись» или «Удаление записи».",
            keyboard=main_keyboard(),
        )


# ========= CALLBACK-КНОПКИ =========

def change_date(
    peer_id: int,
    user_id: int,
    conversation_message_id: Optional[int],
    direction: int,
) -> None:
    session = get_session(user_id)

    if session.get("state") != "booking_choosing_slot":
        edit_or_send(
            peer_id,
            conversation_message_id,
            "Сессия устарела. Начните заново кнопкой «Запись».",
        )
        clear_session(user_id)
        return

    current_index = int(session.get("date_index", get_current_date_index()))
    new_index = min(len(DATES) - 1, max(0, current_index + direction))

    session["date_index"] = new_index
    session["time_page"] = 0
    sessions[user_id] = session

    data = load_data()
    keyboard = generate_date_keyboard(new_index, data, page=0)

    edit_or_send(
        peer_id,
        conversation_message_id,
        slot_prompt(new_index, data, page=0),
        keyboard=keyboard,
    )


def change_time_page(
    peer_id: int,
    user_id: int,
    conversation_message_id: Optional[int],
    page: int,
) -> None:
    session = get_session(user_id)

    if session.get("state") != "booking_choosing_slot":
        edit_or_send(
            peer_id,
            conversation_message_id,
            "Сессия устарела. Начните заново кнопкой «Запись».",
        )
        clear_session(user_id)
        return

    date_index = int(session.get("date_index", get_current_date_index()))

    data = load_data()
    free_times = get_free_times(DATES[date_index], data)
    max_page = max(0, (len(free_times) - 1) // TIME_PAGE_SIZE) if free_times else 0

    page = min(max(page, 0), max_page)

    session["time_page"] = page
    sessions[user_id] = session

    keyboard = generate_date_keyboard(date_index, data, page=page)

    edit_or_send(
        peer_id,
        conversation_message_id,
        slot_prompt(date_index, data, page=page),
        keyboard=keyboard,
    )


def confirm_slot(
    peer_id: int,
    user_id: int,
    conversation_message_id: Optional[int],
    date: str,
    time_value: str,
) -> None:
    session = get_session(user_id)

    if session.get("state") != "booking_choosing_slot":
        edit_or_send(
            peer_id,
            conversation_message_id,
            "Сессия устарела. Начните заново кнопкой «Запись».",
        )
        clear_session(user_id)
        return

    if is_time_passed(date, time_value):
        edit_or_send(
            peer_id,
            conversation_message_id,
            "Этот слот уже прошел. Выберите другой.",
        )
        clear_session(user_id)
        return

    data = load_data()

    count = sum(
        1
        for value in data.values()
        if value["date"] == date and value["time"] == time_value
    )

    if count >= MAX_PEOPLE:
        edit_or_send(
            peer_id,
            conversation_message_id,
            "Этот слот только что заняли. Попробуйте другой.",
        )
        clear_session(user_id)
        return

    event_id = str(session.get("event_id", "")).strip()

    if not event_id:
        edit_or_send(
            peer_id,
            conversation_message_id,
            "Не удалось найти ваш ID. Начните запись заново.",
        )
        clear_session(user_id)
        return

    data[event_id] = {
        "date": date,
        "time": time_value,
    }

    save_data(data)

    edit_or_send(
        peer_id,
        conversation_message_id,
        f"Вы успешно записались на Ментальное ГТО на {date} в {time_value}.",
    )

    clear_session(user_id)


def handle_callback(obj: Any) -> None:
    user_id = int(get_field(obj, "user_id", 0) or 0)
    peer_id = int(get_field(obj, "peer_id", user_id) or user_id)
    conversation_message_id = get_field(obj, "conversation_message_id")
    event_id = get_field(obj, "event_id")
    payload = parse_payload(get_field(obj, "payload", {}))
    cmd = payload.get("cmd")

    if cmd == "noop":
        send_event_answer(
            event_id,
            user_id,
            peer_id,
            "Это просто навигационная кнопка.",
        )
        return

    if cmd == "date_prev":
        change_date(peer_id, user_id, conversation_message_id, -1)
        return

    if cmd == "date_next":
        change_date(peer_id, user_id, conversation_message_id, 1)
        return

    if cmd == "time_page":
        change_time_page(
            peer_id,
            user_id,
            conversation_message_id,
            int(payload.get("page", 0) or 0),
        )
        return

    if cmd == "slot":
        confirm_slot(
            peer_id,
            user_id,
            conversation_message_id,
            payload.get("date", ""),
            payload.get("time", ""),
        )
        return

    send_event_answer(event_id, user_id, peer_id, "Неизвестная кнопка.")


# ========= MAIN =========

def main() -> None:
    global vk

    if not VK_GROUP_TOKEN:
        raise RuntimeError(
            "Не задан VK_GROUP_TOKEN. Создайте .env по примеру .env.example"
        )

    if not VK_GROUP_ID:
        raise RuntimeError(
            "Не задан VK_GROUP_ID. Создайте .env по примеру .env.example"
        )

    vk_session = vk_api.VkApi(token=VK_GROUP_TOKEN)
    vk = vk_session.get_api()
    longpoll = VkBotLongPoll(vk_session, VK_GROUP_ID)

    print("VK bot started")

    for event in longpoll.listen():
        try:
            obj = getattr(event, "object", None) or getattr(event, "obj", None)

            if event.type == VkBotEventType.MESSAGE_NEW:
                message = get_field(obj, "message", obj)
                text = get_field(message, "text", "") or ""

                user_id = int(get_field(message, "from_id", 0) or 0)
                peer_id = int(get_field(message, "peer_id", user_id) or user_id)

                # Сообщения от групп и системные события игнорируем.
                if user_id <= 0:
                    continue

                handle_message(peer_id, user_id, text)

            elif event.type == VkBotEventType.MESSAGE_EVENT:
                handle_callback(obj)

        except Exception as exc:
            print(f"Ошибка обработки события: {exc}")


if __name__ == "__main__":
    main()
