"""История: распознанные фразы, найденные вопросы и ответы — в SQLite рядом с настройками.

Каждое срабатывание детектора записывается «событием» с итогом:
- answering / answered / interrupted — ответ идёт, получен или прерван новым вопросом;
- not_question — проверка Gemini решила, что это не вопрос;
- repeat — вопрос совпал с недавним и пропущен;
- error — ошибка проверки или ответа.
Так видно и ложные срабатывания, и пропущенные вопросы (по расшифровке), и к любому ответу можно вернуться.
"""

import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import APP_DIR
from .i18n import msg

DB_PATH = APP_DIR / "history.sqlite3"
SHOTS_DIR = APP_DIR / "history_shots"

SCHEMA = """
CREATE TABLE IF NOT EXISTS phrases (
    id INTEGER PRIMARY KEY, session TEXT NOT NULL, ts REAL NOT NULL, text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, session TEXT NOT NULL, ts REAL NOT NULL,
    kind TEXT NOT NULL,            -- auto | manual | reask
    status TEXT NOT NULL,          -- answering | answered | interrupted | not_question | repeat | error
    heard TEXT DEFAULT '',         -- фраза, на которую сработал детектор
    question TEXT DEFAULT '',      -- формулировка вопроса после проверки
    answer TEXT DEFAULT '',
    error TEXT DEFAULT '',
    context TEXT DEFAULT '[]',     -- JSON: реплики, отправленные вместе с вопросом
    preset TEXT DEFAULT '',
    model TEXT DEFAULT '',
    check_ms INTEGER,              -- сколько шла проверка «это вопрос?»
    wait_ms INTEGER,               -- от конца фразы до первых слов ответа
    answer_ms INTEGER,             -- от запроса до конца ответа
    screenshot INTEGER DEFAULT 0,
    phrase_id INTEGER
);
CREATE INDEX IF NOT EXISTS phrases_ts ON phrases (ts);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS events_phrase ON events (phrase_id);
"""

# Фильтры списка в окне истории
FILTERS = {
    "answered": "status IN ('answering', 'answered', 'interrupted')",
    "rejected": "status IN ('not_question', 'repeat')",
    "errors": "status = 'error'",
    "manual": "kind IN ('manual', 'reask')",
}
LOCAL_DAY = "date(ts, 'unixepoch', 'localtime')"


class HistoryStore:
    def __init__(self, path: Path = DB_PATH, shots_dir: Path = SHOTS_DIR):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._shots = shots_dir
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        # LIKE в SQLite не различает регистр только для латиницы — для поиска по кириллице свой lower()
        self._db.create_function("ulower", 1, lambda text: text.lower() if text else text, deterministic=True)
        self._db.executescript(SCHEMA)
        self.session = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.version = 0  # растёт при каждой записи — окно истории по нему понимает, что пора обновиться

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---------- запись ----------

    def _write(self, sql: str, params=()) -> int:
        with self._lock:
            cursor = self._db.execute(sql, params)
            self._db.commit()
            self.version += 1
            return cursor.lastrowid

    def add_phrase(self, text: str) -> int:
        return self._write("INSERT INTO phrases (session, ts, text) VALUES (?, ?, ?)", (self.session, time.time(), text))

    def add_event(self, **fields) -> int:
        fields = {"session": self.session, "ts": time.time(), **fields}
        if not isinstance(fields.get("context", ""), str):
            fields["context"] = json.dumps(fields["context"], ensure_ascii=False)
        keys = list(fields)
        sql = f"INSERT INTO events ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})"
        return self._write(sql, [fields[key] for key in keys])

    def update_event(self, event_id: int, **fields) -> None:
        assignments = ", ".join(f"{key} = ?" for key in fields)
        self._write(f"UPDATE events SET {assignments} WHERE id = ?", [*fields.values(), event_id])

    def save_screenshot(self, event_id: int, jpeg: bytes) -> None:
        self._shots.mkdir(parents=True, exist_ok=True)
        (self._shots / f"{event_id}.jpg").write_bytes(jpeg)
        self.update_event(event_id, screenshot=1)

    def delete_event(self, event_id: int) -> None:
        self._write("DELETE FROM events WHERE id = ?", (event_id,))
        (self._shots / f"{event_id}.jpg").unlink(missing_ok=True)

    def clear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM events")
            self._db.execute("DELETE FROM phrases")
            self._db.commit()
            self.version += 1
        for shot in self._shots.glob("*.jpg"):
            shot.unlink(missing_ok=True)

    def prune(self, keep_days: int) -> None:
        """Удаляет записи и скриншоты старше keep_days дней."""
        cutoff = time.time() - keep_days * 86400
        for row in self._read("SELECT id FROM events WHERE ts < ? AND screenshot = 1", (cutoff,)):
            (self._shots / f"{row['id']}.jpg").unlink(missing_ok=True)
        with self._lock:
            self._db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self._db.execute("DELETE FROM phrases WHERE ts < ?", (cutoff,))
            self._db.commit()

    # ---------- чтение ----------

    def _read(self, sql: str, params=()) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._db.execute(sql, params)]

    @staticmethod
    def _decode(event: dict) -> dict:
        event["context"] = json.loads(event["context"] or "[]")
        return event

    def event(self, event_id: int) -> dict | None:
        rows = self._read("SELECT * FROM events WHERE id = ?", (event_id,))
        return self._decode(rows[0]) if rows else None

    def events(self, filter: str = "", query: str = "", day: str = "", limit: int = 50, offset: int = 0) -> list[dict]:
        where, params = [], []
        if filter in FILTERS:
            where.append(FILTERS[filter])
        if day:
            where.append(f"{LOCAL_DAY} = ?")
            params.append(day)
        if query:
            where.append("(ulower(heard) LIKE ? OR ulower(question) LIKE ? OR ulower(answer) LIKE ?)")
            params += [f"%{query.lower()}%"] * 3
        sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "")
        rows = self._read(sql + " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?", [*params, limit, offset])
        return [self._decode(row) for row in rows]

    def days(self) -> list[dict]:
        return self._read(
            f"""SELECT day, SUM(is_event) AS events, SUM(is_phrase) AS phrases FROM (
                    SELECT {LOCAL_DAY} AS day, 1 AS is_event, 0 AS is_phrase FROM events
                    UNION ALL SELECT {LOCAL_DAY}, 0, 1 FROM phrases
                ) GROUP BY day ORDER BY day DESC"""
        )

    def phrases(self, day: str) -> list[dict]:
        """Расшифровка за день; у фраз, на которые сработал детектор, — последнее событие по ним."""
        return self._read(
            f"""SELECT p.id, p.ts, p.text, e.id AS event_id, e.status AS event_status
                FROM phrases p
                LEFT JOIN events e ON e.id = (SELECT id FROM events WHERE phrase_id = p.id ORDER BY ts DESC LIMIT 1)
                WHERE date(p.ts, 'unixepoch', 'localtime') = ? ORDER BY p.ts""",
            (day,),
        )

    def phrase(self, phrase_id: int) -> dict | None:
        rows = self._read("SELECT * FROM phrases WHERE id = ?", (phrase_id,))
        return rows[0] if rows else None

    def context_before(self, phrase: dict, seconds: float) -> list[str]:
        """Реплики того же запуска программы за seconds до фразы включительно — контекст для повторного вопроса."""
        rows = self._read(
            "SELECT text FROM phrases WHERE session = ? AND ts BETWEEN ? AND ? ORDER BY ts",
            (phrase["session"], phrase["ts"] - seconds, phrase["ts"]),
        )
        return [row["text"] for row in rows]

    def screenshot(self, event_id: int) -> bytes | None:
        path = self._shots / f"{event_id}.jpg"
        return path.read_bytes() if path.exists() else None

    # ---------- выгрузка ----------

    def export(self, fmt: str) -> tuple[bytes, str, str]:
        """Вся история файлом: (содержимое, тип, имя файла)."""
        events = list(reversed(self.events(limit=1_000_000)))  # по времени, от старых к новым
        stamp = datetime.now().strftime("%Y-%m-%d")
        if fmt == "json":
            phrases = self._read("SELECT id, session, ts, text FROM phrases ORDER BY ts")
            body = json.dumps({"events": events, "phrases": phrases}, ensure_ascii=False, indent=2)
            return body.encode(), "application/json; charset=utf-8", f"smart-overlay-history-{stamp}.json"

        lines = [f"# {msg('web.history_title')}", ""]
        for event in events:
            when = datetime.fromtimestamp(event["ts"]).strftime("%Y-%m-%d %H:%M:%S")
            lines += [f"## {when} — {msg('web.status_' + event['status'])} · {msg('web.kind_' + event['kind'])}", ""]
            if event["heard"]:
                lines.append(f"- **{msg('web.heard')}:** {event['heard']}")
            if event["question"] and event["question"] != event["heard"]:
                lines.append(f"- **{msg('web.question')}:** {event['question']}")
            if event["preset"]:
                lines.append(f"- **{msg('web.preset')}:** {event['preset']}")
            if event["error"]:
                lines.append(f"- **{msg('web.status_error')}:** {event['error']}")
            if event["answer"]:
                lines += ["", event["answer"].strip()]
            if event["context"]:
                lines += ["", f"<details><summary>{msg('web.context')}</summary>", ""]
                lines += [f"> {line}" for line in event["context"]] + ["", "</details>"]
            lines.append("")
        return "\n".join(lines).encode(), "text/markdown; charset=utf-8", f"smart-overlay-history-{stamp}.md"
