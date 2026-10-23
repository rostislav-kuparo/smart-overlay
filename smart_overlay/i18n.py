"""Переводы интерфейса: русский, украинский, английский.

Тексты для модели (промпты, обрамление запроса) не переводятся — Gemini понимает их на любом языке,
а язык ответа задаётся отдельной настройкой.
"""

from typing import NamedTuple


class T(NamedTuple):
    ru: str
    uk: str
    en: str


INTERFACE_LANGUAGES = (("ru", "Русский"), ("uk", "Українська"), ("en", "English"))

_language = "ru"


def set_language(language: str) -> None:
    global _language
    _language = language if language in T._fields else "ru"


def get_language() -> str:
    return _language


def tr(text: T | str) -> str:
    return getattr(text, _language) if isinstance(text, T) else text


def msg(name: str, /, **kwargs) -> str:
    text = tr(MESSAGES[name])
    return text.format(**kwargs) if kwargs else text


def web_messages() -> dict[str, str]:
    return {key[4:]: tr(text) for key, text in MESSAGES.items() if key.startswith("web.")}


MESSAGES: dict[str, T] = {
    "config.header": T(
        "Smart Overlay — настройки. Удобнее менять через окно настроек (значок в трее → «Настройки»).",
        "Smart Overlay — налаштування. Зручніше змінювати у вікні налаштувань (значок у треї → «Налаштування»).",
        "Smart Overlay settings. Easier to change in the settings window (tray icon → Settings).",
    ),
    # ---------- состояние ----------
    "status.starting": T("Запуск…", "Запуск…", "Starting…"),
    "status.loading_model": T(
        "Загрузка модели распознавания (при первом запуске — скачивание, несколько минут)…",
        "Завантаження моделі розпізнавання (під час першого запуску — звантаження, кілька хвилин)…",
        "Loading the speech model (the first run downloads it and may take a few minutes)…",
    ),
    "status.cpu_fallback": T(
        "Видеокарта недоступна — распознаю на процессоре (медленнее)…",
        "Відеокарта недоступна — розпізнаю на процесорі (повільніше)…",
        "GPU unavailable — transcribing on the CPU (slower)…",
    ),
    "status.whisper_failed": T("Whisper не запустился: {error}", "Whisper не запустився: {error}",
                               "Whisper failed to start: {error}"),
    "status.no_audio": T("Нет доступа к звуку: {error}", "Немає доступу до звуку: {error}",
                         "Can't capture audio: {error}"),
    "status.listening": T("Слушаю", "Слухаю", "Listening"),
    "status.listening_manual": T("Слушаю — отвечаю только по команде", "Слухаю — відповідаю лише за командою",
                                 "Listening — answering on demand only"),
    "status.paused": T("Пауза — звук не обрабатывается", "Пауза — звук не обробляється", "Paused — audio is ignored"),
    "status.thinking": T("Думаю…", "Думаю…", "Thinking…"),
    "status.gemini_error": T("Gemini: {error}", "Gemini: {error}", "Gemini: {error}"),
    "error.no_key": T(
        "нет ключа API — откройте настройки (значок в трее)",
        "немає ключа API — відкрийте налаштування (значок у треї)",
        "no API key — open the settings (tray icon)",
    ),
    # ---------- окно ответа ----------
    "overlay.manual_question": T(
        "Ручной запрос: последний вопрос или то, что на экране",
        "Ручний запит: останнє питання або те, що на екрані",
        "Manual request: the last question or what's on screen",
    ),
    "overlay.move_hint": T(
        "Тащите окно мышью, края и углы — растянуть. {hotkey} — закрепить",
        "Тягніть вікно мишею, краї й кути — розтягнути. {hotkey} — закріпити",
        "Drag the window; drag edges and corners to resize. {hotkey} locks it",
    ),
    "overlay.move_hint_tray": T(
        "Тащите окно мышью, края и углы — растянуть. Закрепить — снова пункт в трее",
        "Тягніть вікно мишею, краї й кути — розтягнути. Закріпити — знову пункт у треї",
        "Drag the window; drag edges and corners to resize. Lock it again from the tray",
    ),
    # ---------- трей ----------
    "tray.settings": T("Настройки…", "Налаштування…", "Settings…"),
    "tray.history": T("История…", "Історія…", "History…"),
    "tray.listen": T("Слушать звук", "Слухати звук", "Listen to audio"),
    "tray.auto_answer": T("Отвечать автоматически", "Відповідати автоматично", "Answer automatically"),
    "tray.screenshots": T("Прикладывать скриншот", "Додавати скриншот", "Attach screenshot"),
    "tray.llm_check": T("Проверять вопрос через Gemini", "Перевіряти питання через Gemini", "Confirm questions with Gemini"),
    "tray.preset": T("Пресет", "Пресет", "Preset"),
    "tray.no_preset": T("Без пресета", "Без пресету", "No preset"),
    "tray.show_window": T("Показывать окно", "Показувати вікно", "Show window"),
    "tray.click_through": T("Пропускать клики сквозь окно", "Пропускати кліки крізь вікно", "Click-through window"),
    "tray.move": T("Двигать и растягивать окно", "Рухати й розтягувати вікно", "Move and resize window"),
    "tray.ask": T("Спросить сейчас", "Запитати зараз", "Ask now"),
    "tray.clear": T("Убрать ответ", "Прибрати відповідь", "Clear answer"),
    "tray.quit": T("Выход", "Вихід", "Quit"),
    # ---------- мониторы ----------
    "monitor.all": T("Все мониторы ({w}×{h})", "Усі монітори ({w}×{h})", "All monitors ({w}×{h})"),
    "monitor.one": T("Монитор {n}: {w}×{h}", "Монітор {n}: {w}×{h}", "Monitor {n}: {w}×{h}"),
    "monitor.primary": T(" — основной", " — основний", " — primary"),
    # ---------- сервер настроек ----------
    "server.open_from_tray": T(
        "Откройте настройки через значок Smart Overlay в трее.",
        "Відкрийте налаштування через значок Smart Overlay у треї.",
        "Open the settings from the Smart Overlay tray icon.",
    ),
    "test.no_key": T("Ключ не задан", "Ключ не задано", "No API key set"),
    "test.ok": T("Ключ работает: {model} ответила за {seconds} с", "Ключ працює: {model} відповіла за {seconds} с",
                 "The key works: {model} replied in {seconds} s"),
    # ---------- ошибки проверки ----------
    "error.number": T("нужно число", "потрібне число", "must be a number"),
    "error.range": T("допустимо от {min} до {max}", "допустимо від {min} до {max}", "allowed: {min} to {max}"),
    "error.invalid": T("недопустимое значение", "неприпустиме значення", "invalid value"),
    "error.hotkey_taken": T("то же сочетание, что у «{other}»", "те саме сполучення, що й у «{other}»",
                            "same combination as “{other}”"),
    "error.hotkey_modifier": T("неизвестный модификатор «{mod}»", "невідомий модифікатор «{mod}»",
                               "unknown modifier “{mod}”"),
    "error.hotkey_key": T("неизвестная клавиша «{key}»", "невідома клавіша «{key}»", "unknown key “{key}”"),
    "error.preset_name_empty": T("у пресета должно быть название", "пресет повинен мати назву", "a preset needs a name"),
    "error.preset_name_taken": T("название «{name}» уже занято", "назва «{name}» вже зайнята",
                                 "the name “{name}” is already taken"),
    "error.no_preset": T("нет пресета «{name}»", "немає пресету «{name}»", "no preset named “{name}”"),
    # ---------- демо ----------
    "demo.transcript": T(
        "Хорошо, тогда следующий вопрос. Чем процесс отличается от потока?",
        "Добре, тоді наступне питання. Чим процес відрізняється від потоку?",
        "Okay, next question. What's the difference between a process and a thread?",
    ),
    "demo.question": T("Чем процесс отличается от потока?", "Чим процес відрізняється від потоку?",
                       "What's the difference between a process and a thread?"),
    "demo.answer": T(
        "**Процесс — отдельная программа со своей памятью, поток — ход выполнения внутри процесса, "
        "который делит память с другими потоками.**\n\n"
        "- У каждого процесса своё адресное пространство; потоки одного процесса видят общие данные.\n"
        "- Создать поток и переключиться на него дешевле, чем на процесс.\n"
        "- Ошибка в одном потоке может уронить весь процесс; процессы изолированы друг от друга.\n"
        "- Процессы обмениваются данными через IPC (`pipe`, сокеты, shared memory), потокам нужна синхронизация "
        "(`mutex`).\n",
        "**Процес — окрема програма зі своєю пам'яттю, потік — хід виконання всередині процесу, "
        "який ділить пам'ять з іншими потоками.**\n\n"
        "- Кожен процес має власний адресний простір; потоки одного процесу бачать спільні дані.\n"
        "- Створити потік і перемкнутися на нього дешевше, ніж на процес.\n"
        "- Помилка в одному потоці може обвалити весь процес; процеси ізольовані один від одного.\n"
        "- Процеси обмінюються даними через IPC (`pipe`, сокети, shared memory), потокам потрібна синхронізація "
        "(`mutex`).\n",
        "**A process is a separate program with its own memory; a thread is a path of execution inside a process "
        "that shares memory with other threads.**\n\n"
        "- Each process has its own address space; threads of one process see shared data.\n"
        "- Creating and switching to a thread is cheaper than to a process.\n"
        "- A crash in one thread can take down the whole process; processes are isolated from each other.\n"
        "- Processes exchange data via IPC (`pipe`, sockets, shared memory); threads need synchronization "
        "(`mutex`).\n",
    ),
    # ---------- окно настроек ----------
    "web.title": T("Smart Overlay — настройки", "Smart Overlay — налаштування", "Smart Overlay — settings"),
    "web.save": T("Сохранить", "Зберегти", "Save"),
    "web.revert": T("Отменить", "Скасувати", "Revert"),
    "web.status_title": T("Состояние программы", "Стан програми", "App status"),
    "web.load_failed": T(
        "Не удалось загрузить настройки. Откройте окно заново через значок Smart Overlay в трее.",
        "Не вдалося завантажити налаштування. Відкрийте вікно знову через значок Smart Overlay у треї.",
        "Couldn't load the settings. Reopen this window from the Smart Overlay tray icon.",
    ),
    "web.saved": T("Сохранено и применено", "Збережено й застосовано", "Saved and applied"),
    "web.saved_restart": T("Сохранено. Распознавание перезапускается…", "Збережено. Розпізнавання перезапускається…",
                           "Saved. Restarting speech recognition…"),
    "web.save_failed": T("Не сохранено — исправьте отмеченные поля", "Не збережено — виправте позначені поля",
                         "Not saved — fix the highlighted fields"),
    "web.apply_failed": T("Не удалось применить", "Не вдалося застосувати", "Couldn't apply the change"),
    "web.not_running": T("Программа не запущена — изменения не сохранятся", "Програма не запущена — зміни не збережуться",
                         "The app isn't running — changes won't be saved"),
    "web.restart_badge": T("перезапуск распознавания", "перезапуск розпізнавання", "restarts recognition"),
    "web.restart_title": T(
        "После сохранения модель распознавания перезагрузится, это ~10 секунд",
        "Після збереження модель розпізнавання перезавантажиться, це ~10 секунд",
        "Saving reloads the speech model (~10 seconds)",
    ),
    "web.key_saved": T("Ключ сохранён — вставьте новый, чтобы заменить", "Ключ збережено — вставте новий, щоб замінити",
                       "Key saved — paste a new one to replace it"),
    "web.key_env": T("Используется ключ из переменной GEMINI_API_KEY", "Використовується ключ зі змінної GEMINI_API_KEY",
                     "Using the key from GEMINI_API_KEY"),
    "web.key_empty": T("Вставьте ключ AIza…", "Вставте ключ AIza…", "Paste your AIza… key"),
    "web.key_stored": T("Ключ сохранён", "Ключ збережено", "Key saved"),
    "web.show": T("Показать", "Показати", "Show"),
    "web.hide": T("Скрыть", "Сховати", "Hide"),
    "web.test": T("Проверить", "Перевірити", "Test"),
    "web.testing": T("Проверяю…", "Перевіряю…", "Testing…"),
    "web.hotkey_off": T("отключено", "вимкнено", "disabled"),
    "web.hotkey_need_mod": T(
        "Нужен Ctrl, Alt, Shift или Win — иначе клавиша перестанет работать в других программах",
        "Потрібен Ctrl, Alt, Shift або Win — інакше клавіша перестане працювати в інших програмах",
        "Add Ctrl, Alt, Shift or Win — otherwise the key stops working in other apps",
    ),
    "web.reset_text": T("Вернуть стандартный текст", "Повернути стандартний текст", "Restore the default text"),
    "web.device_default": T("Устройство по умолчанию в Windows", "Пристрій за замовчуванням у Windows",
                            "Windows default device"),
    "web.device_missing": T("(сейчас не подключено)", "(зараз не підключено)", "(not connected)"),
    "web.config_file": T("Файл настроек:", "Файл налаштувань:", "Settings file:"),
    "web.log_file": T("Журнал:", "Журнал:", "Log:"),
    "web.control": T("Управление", "Керування", "Controls"),
    "web.control_desc": T("Переключатели применяются сразу, сохранять не нужно.",
                          "Перемикачі застосовуються одразу, зберігати не потрібно.",
                          "Switches apply instantly — no need to save."),
    "web.listening": T("Слушать звук", "Слухати звук", "Listen to audio"),
    "web.listening_help": T("Выключите, чтобы поставить программу на паузу.", "Вимкніть, щоб поставити програму на паузу.",
                            "Turn off to pause the app."),
    "web.overlay_visible": T("Показывать окно ответа", "Показувати вікно відповіді", "Show the answer window"),
    "web.move_window": T("Двигать и растягивать окно", "Рухати й розтягувати вікно", "Move and resize the window"),
    "web.move_help": T(
        "Нужно, только если включены сквозные клики: временно делает окно «хватаемым» мышью. "
        "Место и размер запоминаются сами.",
        "Потрібно, лише якщо ввімкнено наскрізні кліки: тимчасово робить вікно «хапальним» для миші. "
        "Місце й розмір запам'ятовуються самі.",
        "Only needed when click-through is on: temporarily lets you grab the window with the mouse. "
        "Position and size are remembered automatically.",
    ),
    "web.preset_none": T("Без пресета", "Без пресету", "No preset"),
    "web.preset_add": T("Добавить пресет", "Додати пресет", "Add preset"),
    "web.preset_delete": T("Удалить", "Видалити", "Delete"),
    "web.preset_active": T("активный", "активний", "active"),
    "web.preset_name_ph": T("Название, например «Созвон по проекту»", "Назва, наприклад «Дзвінок щодо проєкту»",
                            "Name, e.g. “Project call”"),
    "web.preset_context_ph": T(
        "Тема, ваша роль, стек, термины, что важно в ответах. Например: «Я backend-разработчик, стек — Python, "
        "Django, PostgreSQL. Обсуждаем переход на микросервисы. Отвечай коротко, с примерами кода.»",
        "Тема, ваша роль, стек, терміни, що важливо у відповідях. Наприклад: «Я backend-розробник, стек — Python, "
        "Django, PostgreSQL. Обговорюємо перехід на мікросервіси. Відповідай коротко, з прикладами коду.»",
        "Topic, your role, tech stack, terms, what matters in answers. For example: “I'm a backend developer, "
        "stack: Python, Django, PostgreSQL. We're discussing a move to microservices. Keep answers short, "
        "with code examples.”",
    ),
    # ---------- окно истории ----------
    "web.history": T("История", "Історія", "History"),
    "web.settings": T("Настройки", "Налаштування", "Settings"),
    "web.history_title": T("Smart Overlay — история", "Smart Overlay — історія", "Smart Overlay — history"),
    "web.tab_events": T("Вопросы и ответы", "Питання й відповіді", "Questions & answers"),
    "web.tab_transcript": T("Расшифровка", "Розшифровка", "Transcript"),
    "web.search_ph": T("Поиск по вопросам и ответам", "Пошук у питаннях і відповідях", "Search questions and answers"),
    "web.all_days": T("Все дни", "Усі дні", "All days"),
    "web.filter_all": T("Все", "Усі", "All"),
    "web.filter_answered": T("Ответы", "Відповіді", "Answered"),
    "web.filter_rejected": T("Отсеяно", "Відсіяно", "Filtered out"),
    "web.filter_errors": T("Ошибки", "Помилки", "Errors"),
    "web.filter_manual": T("Вручную", "Вручну", "Manual"),
    "web.status_answering": T("Отвечает…", "Відповідає…", "Answering…"),
    "web.status_answered": T("Ответ", "Відповідь", "Answered"),
    "web.status_interrupted": T("Прервано новым вопросом", "Перервано новим питанням", "Interrupted by a newer question"),
    "web.status_not_question": T("Не вопрос — отсеяно проверкой", "Не питання — відсіяно перевіркою",
                                 "Not a question — filtered out by the check"),
    "web.status_repeat": T("Повтор — пропущено", "Повтор — пропущено", "Repeat — skipped"),
    "web.status_error": T("Ошибка", "Помилка", "Error"),
    "web.kind_auto": T("автоматически", "автоматично", "automatic"),
    "web.kind_manual": T("ручной запрос", "ручний запит", "manual request"),
    "web.kind_reask": T("повторный запрос", "повторний запит", "asked again"),
    "web.heard": T("Услышано", "Почуто", "Heard"),
    "web.question": T("Вопрос", "Питання", "Question"),
    "web.preset": T("Пресет", "Пресет", "Preset"),
    "web.model": T("Модель", "Модель", "Model"),
    "web.context": T("Контекст разговора", "Контекст розмови", "Conversation context"),
    "web.screenshot": T("Скриншот", "Скриншот", "Screenshot"),
    "web.timing_check": T("проверка", "перевірка", "check"),
    "web.timing_wait": T("до первых слов", "до перших слів", "to first words"),
    "web.timing_answer": T("ответ целиком", "уся відповідь", "full answer"),
    "web.seconds": T("{n} с", "{n} с", "{n} s"),
    "web.ask_again": T("Спросить снова", "Запитати знову", "Ask again"),
    "web.answer_this": T("Ответить на это", "Відповісти на це", "Answer this"),
    "web.asked": T("Отправлено — ответ появится в окне ответа", "Надіслано — відповідь з'явиться у вікні відповіді",
                   "Sent — the answer will appear in the answer window"),
    "web.copy": T("Копировать ответ", "Копіювати відповідь", "Copy answer"),
    "web.copied": T("Скопировано", "Скопійовано", "Copied"),
    "web.delete": T("Удалить", "Видалити", "Delete"),
    "web.delete_confirm": T("Удалить эту запись?", "Видалити цей запис?", "Delete this entry?"),
    "web.load_more": T("Показать ещё", "Показати ще", "Show more"),
    "web.export": T("Экспорт", "Експорт", "Export"),
    "web.clear": T("Очистить историю", "Очистити історію", "Clear history"),
    "web.clear_confirm": T(
        "Удалить всю историю, расшифровку и скриншоты? Это нельзя отменить.",
        "Видалити всю історію, розшифровку та скриншоти? Це не можна скасувати.",
        "Delete all history, transcripts and screenshots? This can't be undone.",
    ),
    "web.empty_events": T(
        "Пока пусто. Здесь появятся вопросы, которые заметила программа, и ответы на них.",
        "Поки порожньо. Тут з'являться питання, які помітила програма, і відповіді на них.",
        "Nothing yet. Questions the app notices and their answers will appear here.",
    ),
    "web.empty_transcript": T("За этот день расшифровки нет.", "За цей день розшифровки немає.",
                              "No transcript for this day."),
    "web.transcript_hint": T(
        "Все распознанные фразы. Если программа пропустила вопрос, нажмите «Ответить на это» рядом с фразой.",
        "Усі розпізнані фрази. Якщо програма пропустила питання, натисніть «Відповісти на це» поруч із фразою.",
        "Every transcribed phrase. If the app missed a question, click “Answer this” next to the phrase.",
    ),
    "web.history_off": T(
        "История выключена — новые записи не сохраняются. Включить можно в настройках, раздел «История».",
        "Історію вимкнено — нові записи не зберігаються. Увімкнути можна в налаштуваннях, розділ «Історія».",
        "History is off — nothing new is saved. Turn it on in Settings → History.",
    ),
    "web.preset_empty": T(
        "Пресетов пока нет. Добавьте первый — например, для созвонов по проекту или для учёбы.",
        "Пресетів поки немає. Додайте перший — наприклад, для дзвінків щодо проєкту чи для навчання.",
        "No presets yet. Add one — for example, for project calls or for studying.",
    ),
}
