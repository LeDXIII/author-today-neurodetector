"""
Хранение результатов: история проверок и кэш распарсенного текста.

Зачем:
  - «Перепроверить упавшие» без пере-парсинга: текст книги лежит в кэше, повторно
    летит только в NeuroDetector.
  - Массовые прогоны на тысячи книг: таблица истории читается из SQLite выборками,
    а не держится в виджете.
  - Экспорт отчёта (TXT/CSV/JSON) для пересылки.

Один файл БД (data/history.db, в .gitignore) плюс сжатый zlib текст в той же БД —
не плодит файлов на книгу и не требует возни с путями.
"""
import csv
import io
import json
import logging
import os
import sqlite3
import threading
import time
import zlib
from datetime import datetime
from typing import Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id      INTEGER NOT NULL,
    url          TEXT    NOT NULL DEFAULT '',
    title        TEXT    NOT NULL DEFAULT '',
    author       TEXT    NOT NULL DEFAULT '',
    created_at   REAL    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'ok',
    ai_percent   REAL,
    human_percent REAL,
    verdict      TEXT    NOT NULL DEFAULT '',
    chapters_parsed  INTEGER NOT NULL DEFAULT 0,
    chapters_total   INTEGER NOT NULL DEFAULT 0,
    chapters_failed  INTEGER NOT NULL DEFAULT 0,
    chapters_locked  INTEGER NOT NULL DEFAULT 0,
    chapters_blocked INTEGER NOT NULL DEFAULT 0,
    paid_chapter INTEGER,
    chars        INTEGER NOT NULL DEFAULT 0,
    chunks_total  INTEGER NOT NULL DEFAULT 0,
    chunks_failed INTEGER NOT NULL DEFAULT 0,
    error        TEXT    NOT NULL DEFAULT '',
    detail       TEXT    NOT NULL DEFAULT '',
    source       TEXT    NOT NULL DEFAULT 'python',
    text_gzip    BLOB,
    text_chars   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_checks_book   ON checks (book_id);
CREATE INDEX IF NOT EXISTS idx_checks_created ON checks (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_checks_status ON checks (status);
"""

# Колонки, которые отдаются в листингах (без тяжёлого text_gzip).
LIGHT_COLUMNS = (
    'id', 'book_id', 'url', 'title', 'author', 'created_at', 'status',
    'ai_percent', 'human_percent', 'verdict', 'chapters_parsed', 'chapters_total',
    'chapters_failed', 'chapters_locked', 'chapters_blocked', 'paid_chapter', 'chars',
    'chunks_total', 'chunks_failed', 'error', 'detail', 'source', 'text_chars',
)


class History:
    """Плоский слой над SQLite. Потокобезопасен через один lock и autocommit."""

    def __init__(self, db_path: Optional[str] = None):
        default = db_path is None
        if default:
            db_path = os.path.join(project_root(), 'data', 'history.db')
        try:
            self._open(db_path)
        except (OSError, sqlite3.Error) as e:
            if not default:
                raise
            # Папка программы может быть только для чтения (Program Files) —
            # тогда история уезжает в профиль пользователя, а не роняет приложение.
            fallback = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')),
                                    'AuthorTodayNeuroDetector', 'history.db')
            logger.warning("Историю не создать в %s (%s) — использую %s", db_path, e, fallback)
            self._open(fallback)
            db_path = fallback
        self.db_path = db_path
        logger.info("История: %s", self.db_path)

    def _open(self, db_path: str) -> None:
        os.makedirs(os.path.dirname(db_path) or '.', exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        # LIKE в SQLite не понимает регистр кириллицы — нормализуем своей функцией,
        # иначе поиск «мастер» по «Мастер Трав» ничего не найдёт.
        self._conn.create_function('norm', 1, _norm, deterministic=True)
        self._conn.execute('PRAGMA journal_mode=WAL')
        self._conn.execute('PRAGMA synchronous=NORMAL')
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """БД могла быть создана более ранней версией — добавляем недостающие колонки."""
        existing = {row[1] for row in self._conn.execute('PRAGMA table_info(checks)')}
        for column, ddl in (('chapters_locked', 'INTEGER NOT NULL DEFAULT 0'),
                            ('chapters_blocked', 'INTEGER NOT NULL DEFAULT 0')):
            if column not in existing:
                self._conn.execute(f'ALTER TABLE checks ADD COLUMN {column} {ddl}')
                logger.info("История: добавлена колонка %s", column)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Запись
    # ------------------------------------------------------------------
    def save(self, record: Dict, text: str = "") -> int:
        """Сохраняет итог проверки одной книги. Возвращает id строки."""
        created = record.get('created_at') or time.time()
        blob = zlib.compress(text.encode('utf-8')) if text else None
        detail = record.get('detail')
        if not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False)[:20000] if detail else ''

        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO checks (
                        book_id, url, title, author, created_at, status,
                        ai_percent, human_percent, verdict,
                        chapters_parsed, chapters_total, chapters_failed, chapters_locked,
                        chapters_blocked, paid_chapter, chars, chunks_total, chunks_failed,
                        error, detail, source, text_gzip, text_chars)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    int(record.get('book_id') or 0), record.get('url', ''),
                    record.get('title', ''), record.get('author', ''), created,
                    record.get('status', 'ok'),
                    record.get('ai_percent'), record.get('human_percent'),
                    record.get('verdict', ''),
                    int(record.get('chapters_parsed', 0) or 0),
                    int(record.get('chapters_total', 0) or 0),
                    int(record.get('chapters_failed', 0) or 0),
                    int(record.get('chapters_locked', 0) or 0),
                    int(record.get('chapters_blocked', 0) or 0),
                    record.get('paid_chapter'),
                    int(record.get('chars', 0) or 0),
                    int(record.get('chunks_total', 0) or 0),
                    int(record.get('chunks_failed', 0) or 0),
                    str(record.get('error', '') or '')[:2000], detail,
                    record.get('source', 'python'), blob, len(text or ''),
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    def purge_text(self, book_id: Optional[int] = None, older_than_days: Optional[float] = None) -> int:
        """Сбрасывает кэш текста (строки истории остаются). Возвращает число очищенных."""
        sql = 'UPDATE checks SET text_gzip = NULL WHERE text_gzip IS NOT NULL'
        args: List = []
        if book_id is not None:
            sql += ' AND book_id = ?'
            args.append(book_id)
        if older_than_days is not None:
            sql += ' AND created_at < ?'
            args.append(time.time() - older_than_days * 86400)
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
        return cur.rowcount

    def vacuum(self) -> None:
        with self._lock:
            self._conn.execute('VACUUM')
            self._conn.commit()

    # ------------------------------------------------------------------
    # Чтение
    # ------------------------------------------------------------------
    def latest(self, book_id: int) -> Optional[Dict]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {', '.join(LIGHT_COLUMNS)} FROM checks WHERE book_id = ? "
                "ORDER BY created_at DESC LIMIT 1", (book_id,)).fetchone()
        return dict(row) if row else None

    def rows(self, limit: int = 500, offset: int = 0, status: Optional[str] = None,
             search: str = '') -> List[Dict]:
        where, args = [], []
        if status == 'failed':
            where.append("status != 'ok'")
        elif status:
            where.append('status = ?')
            args.append(status)
        if search:
            where.append('(norm(title) LIKE norm(?) OR norm(author) LIKE norm(?) '
                         'OR CAST(book_id AS TEXT) = ?)')
            needle = f'%{search}%'
            args += [needle, needle, search.strip()]
        sql = f"SELECT {', '.join(LIGHT_COLUMNS)} FROM checks"
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        sql += ' ORDER BY created_at DESC LIMIT ? OFFSET ?'
        args += [int(limit), int(offset)]
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def count(self, status: Optional[str] = None) -> int:
        sql = 'SELECT COUNT(*) FROM checks'
        args: List = []
        if status == 'failed':
            sql += " WHERE status != 'ok'"
        elif status:
            sql += ' WHERE status = ?'
            args.append(status)
        with self._lock:
            return int(self._conn.execute(sql, args).fetchone()[0])

    def text_for(self, book_id: int) -> str:
        """Кэшированный текст последней проверки книги ('' — если кэша нет)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT text_gzip FROM checks WHERE book_id = ? AND text_gzip IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1", (book_id,)).fetchone()
        if not row or not row['text_gzip']:
            return ""
        try:
            return zlib.decompress(bytes(row['text_gzip'])).decode('utf-8', 'replace')
        except Exception as e:
            logger.warning("Кэш текста для %s не читается: %s", book_id, e)
            return ""

    def size_bytes(self) -> int:
        try:
            return os.path.getsize(self.db_path)
        except OSError:
            return 0

    # ------------------------------------------------------------------
    # Экспорт
    # ------------------------------------------------------------------
    def export(self, path: str, fmt: str = 'txt', rows: Optional[Iterable[Dict]] = None) -> str:
        """Выгружает историю (или переданные строки) в TXT / CSV / JSON."""
        if rows is None:
            rows = self.rows(limit=self.count() or 1)
        rows = list(rows)
        fmt = (fmt or 'txt').lower().lstrip('.')

        if fmt == 'json':
            payload = [{k: v for k, v in r.items() if k != 'detail'} for r in rows]
            _write(path, json.dumps(payload, ensure_ascii=False, indent=2))
        elif fmt == 'csv':
            buf = io.StringIO()
            cols = ['book_id', 'title', 'author', 'status', 'ai_percent', 'human_percent',
                    'verdict', 'chapters_parsed', 'chapters_total', 'chapters_failed',
                    'paid_chapter', 'chars', 'error', 'created_at']
            writer = csv.DictWriter(buf, fieldnames=cols, extrasaction='ignore', delimiter=';')
            writer.writeheader()
            for r in rows:
                r = dict(r)
                if r.get('created_at'):
                    r['created_at'] = fmt_dt(r['created_at'])
                writer.writerow(r)
            _write(path, buf.getvalue(), encoding='utf-8-sig')
        else:
            _write(path, render_report(rows))

        logger.info("Экспортировано %d записей -> %s (%s)", len(rows), path, fmt)
        return path


def project_root() -> str:
    """Корень установленного проекта (рядом с src/)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _norm(value) -> str:
    """Нормализация для поиска: регистр кириллицы SQLite не понимает сам."""
    return str(value if value is not None else '').casefold()


def render_report(rows: List[Dict]) -> str:
    """Человекочитаемый отчёт — то, что удобно вставить в чат/форум."""
    lines = [
        "Проверка книг author.today через Yandex NeuroDetector",
        f"Отчёт от {datetime.now().strftime('%Y-%m-%d %H:%M')} · записей: {len(rows)}",
        "=" * 72,
    ]
    for r in rows:
        lines.append("")
        lines.append(f"📖 {r.get('title') or 'Без названия'} (#{r.get('book_id')})")
        if r.get('author'):
            lines.append(f"   Автор: {r['author']}")
        if r.get('url'):
            lines.append(f"   {r['url']}")
        lines.append(f"   Проверено: {fmt_dt(r.get('created_at'))}")
        lines.append(f"   Глав: {r.get('chapters_parsed', 0)}"
                     f" из {r.get('chapters_total', 0)}"
                     + (f", пропущено: {r['chapters_failed']}" if r.get('chapters_failed') else '')
                     + (f", закрыто платным: {r['chapters_locked']}" if r.get('chapters_locked') else '')
                     + (f", заглушка: {r['chapters_blocked']}" if r.get('chapters_blocked') else '')
                     + (f", остановка на главе #{r['paid_chapter']}" if r.get('paid_chapter') else ''))
        lines.append(f"   Символов проанализировано: {r.get('chars', 0):,}".replace(',', ' '))
        if r.get('status') == 'ok' and r.get('ai_percent') is not None:
            lines.append(f"   ИИ: {r['ai_percent']}% · Человек: {r.get('human_percent')}%")
            lines.append(f"   Вердикт: {r.get('verdict')}")
        else:
            lines.append(f"   ⚠️ {r.get('error') or r.get('status') or 'нет результата'}")
    lines.append("")
    lines.append("=" * 72)
    return "\n".join(lines)


def fmt_dt(ts) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(float(ts)).strftime('%Y-%m-%d %H:%M')
    except (TypeError, ValueError, OSError):
        return str(ts)


def _write(path: str, content: str, encoding: str = 'utf-8') -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, 'w', encoding=encoding, newline='') as f:
        f.write(content)
