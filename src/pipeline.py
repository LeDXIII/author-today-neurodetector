"""
Оркестрация проверки: ссылка книги -> парсинг -> NeuroDetector -> запись в историю.

Слой намеренно не знает про PyQt: интерфейс передаёт колбэки (логи, прогресс, «стоп»),
а вся последовательность шагов остаётся проверяемой без запущенного окна.
"""
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from src.detector import DEFAULT_CHUNK_CHARS, NeuroDetector
from src.reader import AuthorTodayReader
from src.storage import History

logger = logging.getLogger(__name__)

WORK_ID_RE = re.compile(r'(?:author\.today)?/?work/(\d+)', re.I)
BARE_ID_RE = re.compile(r'^(\d{3,})$')
READER_ID_RE = re.compile(r'/work/(\d+)|/reader/(\d+)/', re.I)


@dataclass
class Settings:
    """Настройки прогона. Значения по умолчанию — измеренные на живом сайте и API."""
    max_chapters: int = 20
    delay: float = 2.0
    headless: bool = True
    use_login: bool = True
    block_images: bool = False
    chapter_attempts: int = 3
    render_wait: float = 12.0
    page_load_timeout: float = 45.0
    chunk_chars: int = DEFAULT_CHUNK_CHARS
    detector_attempts: int = 4
    skip_checked_hours: float = 0.0   # >0: не пере-парсить книги, проверенные недавно
    save_text: bool = True            # кэш текста для «перепроверить без парсинга»

    @staticmethod
    def from_dict(data: Dict) -> 'Settings':
        known = {f for f in Settings.__dataclass_fields__}
        s = Settings()
        for key, value in (data or {}).items():
            if key not in known or value is None:
                continue
            current = getattr(s, key)
            try:
                if isinstance(current, bool):
                    setattr(s, key, bool(int(value)) if isinstance(value, str) else bool(value))
                elif isinstance(current, int) and not isinstance(current, bool):
                    setattr(s, key, int(float(value)))
                elif isinstance(current, float):
                    setattr(s, key, float(value))
                else:
                    setattr(s, key, value)
            except (TypeError, ValueError):
                logger.warning("Настройка %s=%r не разобрана, оставляю %r", key, value, current)
        return s


@dataclass
class Callbacks:
    """Точки обратного вызова. Все необязательные."""
    log: Callable[[str], None] = lambda msg: None
    book_started: Callable[[int, int, dict], None] = lambda idx, total, book: None
    chapter: Callable[[int, int, int, dict], None] = lambda book_idx, num, total, info: None
    detecting: Callable[[int, int, str], None] = lambda done, total, note: None
    book_done: Callable[[dict], None] = lambda record: None
    login: Callable[[str, str], None] = lambda mode, note: None

    def safe_log(self, message: str) -> None:
        try:
            self.log(message)
        except Exception:
            pass

    def safe_login(self, mode: str, note: str = '') -> None:
        try:
            self.login(mode, note)
        except Exception:
            pass


@dataclass
class BookTarget:
    book_id: int
    url: str = ''
    raw: str = ''

    def __post_init__(self):
        if not self.url:
            self.url = f"https://author.today/work/{self.book_id}"


def parse_targets(text: str) -> tuple[List[BookTarget], List[str]]:
    """Разбирает поле ввода: по строке на книгу. Молча дедуплицирует, мусор возвращает списком.

    Принимаются и `https://author.today/work/123`, и `/work/123`, и просто `123`,
    и ссылка читалки `/reader/123/456` (книга та же).
    """
    targets: List[BookTarget] = []
    invalid: List[str] = []
    seen = set()

    for line in (text or '').splitlines():
        raw = line.strip().strip('"\'')
        if not raw:
            continue

        book_id = None
        m = WORK_ID_RE.search(raw)
        if m:
            book_id = int(m.group(1))
        else:
            m = BARE_ID_RE.match(raw)
            if m:
                book_id = int(m.group(1))
            else:
                m = READER_ID_RE.search(raw)
                if m:
                    book_id = int(next(g for g in m.groups() if g))

        if book_id is None:
            invalid.append(raw)
            continue

        if book_id in seen:
            continue
        seen.add(book_id)
        targets.append(BookTarget(book_id=book_id, raw=raw))

    return targets, invalid


def check_one(
    reader: AuthorTodayReader,
    detector: NeuroDetector,
    book_id: int,
    settings: Settings,
    cb: Callbacks,
    url: str = '',
    should_stop: Optional[Callable[[], bool]] = None,
    reuse_cached_text: bool = False,
    history: Optional[History] = None,
) -> Dict:
    """Проверяет одну книгу. Никогда не бросает — ошибка превращается в статус записи."""
    started = time.time()
    record = _empty_record(book_id, url)

    text = ''
    info: Dict = {}

    cached_text = history.text_for(book_id) if (history and reuse_cached_text) else ''
    if cached_text:
        text = cached_text
        record['text_source'] = 'cache'
        cb.safe_log(f"  текст из кэша ({len(text):,} симв. — пере-парсинг не нужен)".replace(',', ' '))
        info = {'parsed': 0, 'total_chars': len(text), 'free_in_toc': 0,
                'paid_in_toc': 0, 'failed': [], 'chapter_results': []}
        record['chars'] = len(text)
    else:
        def chapter_cb(num, total, title, status_text):
            cb.chapter(book_id, num, total, {'title': title, 'status': status_text})

        try:
            text, info = reader.parse_book(
                book_id,
                max_chapters=settings.max_chapters,
                delay=settings.delay,
                progress_callback=chapter_cb,
                should_stop=should_stop,
            )
        except Exception as e:  # любая случайность наружу не выпускается
            logger.exception("Парсинг книги %s упал", book_id)
            record['status'] = 'parse_crash'
            record['error'] = f"{type(e).__name__}: {e}"
            record['elapsed'] = round(time.time() - started, 1)
            return record

    record['title'] = info.get('book_title', '') or record['title']
    record['author'] = info.get('book_author', '')
    record['chapters_total'] = info.get('free_in_toc', 0) or info.get('attempted', 0)
    record['chapters_parsed'] = info.get('parsed', 0)
    record['chapters_failed'] = len(info.get('failed') or [])
    record['chapters_locked'] = info.get('paid_in_toc', 0)
    record['paid_chapter'] = info.get('paid_chapter')
    record['chars'] = info.get('total_chars', len(text))
    record['stopped_early'] = bool(info.get('stopped_early'))

    if info.get('error'):
        record['error'] = info['error']

    if not text:
        record['status'] = 'no_text'
        record['error'] = record['error'] or 'текст не получен'
        record['detail'] = _detail(info)
        record['elapsed'] = round(time.time() - started, 1)
        return record

    if not record['chapters_parsed']:
        record['chapters_parsed'] = len(re.findall(r'^=== .+ ===$', text, re.M)) or 1

    # Отправка в детектор
    cb.detecting(0, 1, f"отправка {len(text)} симв.")
    try:
        result = detector.check_text(text)
    except Exception as e:
        logger.exception("NeuroDetector упал на книге %s", book_id)
        result = {'error': f"{type(e).__name__}: {e}"}

    record['detail'] = _detail(info, result)
    record['chunks_total'] = result.get('chunks_total', 0)
    record['chunks_failed'] = result.get('chunks_failed', 0)

    if result.get('error'):
        record['status'] = 'detect_failed'
        record['error'] = result['error']
        record['saved_text'] = text if settings.save_text else ''
        record['elapsed'] = round(time.time() - started, 1)
        return record

    record['status'] = 'partial' if result.get('partial') else 'ok'
    record['ai_percent'] = result.get('ai_percent')
    record['human_percent'] = result.get('human_percent')
    record['verdict'] = result.get('verdict', '')
    record['segments'] = result.get('total_segments', 0)
    if result.get('warning'):
        record['error'] = result['warning']
    record['saved_text'] = text if settings.save_text else ''
    record['elapsed'] = round(time.time() - started, 1)
    return record


def run_batch(
    targets: List[BookTarget],
    settings: Settings,
    cb: Optional[Callbacks] = None,
    history: Optional[History] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    reuse_cached_text: bool = False,
) -> List[Dict]:
    """Прогон пачки книг в текущем потоке. Браузер поднимается один раз на все книги."""
    cb = cb or Callbacks()
    should_stop = should_stop or (lambda: False)
    records: List[Dict] = []

    reader = AuthorTodayReader(
        headless=settings.headless,
        max_attempts=settings.chapter_attempts,
        render_wait=settings.render_wait,
        page_load_timeout=settings.page_load_timeout,
        block_images=settings.block_images,
    )
    auth = None

    try:
        if settings.use_login:
            auth = _try_login(reader, cb)

        detector = NeuroDetector(
            chunk_chars=settings.chunk_chars,
            max_attempts=settings.detector_attempts,
            on_progress=lambda done, total, note: cb.detecting(done, total, note),
        )
        try:
            for idx, target in enumerate(targets, 1):
                if should_stop():
                    cb.safe_log("\n[СТОП] прогон прерван пользователем")
                    break

                if _recently_checked(history, target.book_id, settings):
                    cb.safe_log(f"[{idx}/{len(targets)}] книга {target.book_id} уже проверена — пропускаю")
                    continue

                cb.safe_log("")
                cb.safe_log("=" * 62)
                cb.safe_log(f"[{idx}/{len(targets)}] {target.url}")
                cb.safe_log("=" * 62)
                cb.book_started(idx, len(targets), {'book_id': target.book_id, 'url': target.url})

                record = check_one(reader, detector, target.book_id, settings, cb,
                                   url=target.url, should_stop=should_stop,
                                   reuse_cached_text=reuse_cached_text, history=history)
                record['url'] = target.url
                records.append(record)

                if history is not None:
                    try:
                        history.save(record, text=record.get('saved_text') or '')
                    except Exception as e:
                        logger.error("История не записана для %s: %s", target.book_id, e)
                        record['history_error'] = str(e)

                cb.book_done(record)
                _log_record(cb, record)
        finally:
            detector.close()
    finally:
        reader.close()
        if auth is not None:
            auth.close()

    return records


def _try_login(reader: AuthorTodayReader, cb: Callbacks):
    """Вход по .env и перенос сессии в Chrome. Не удалось — честно продолжаем анонимно."""
    from src.auth import AuthorTodayAuth, TwoFactorRequiredError

    auth = AuthorTodayAuth()
    if not auth.has_credentials:
        cb.safe_log("  вход: в .env нет AT_LOGIN/AT_PASSWORD — работаю анонимно (только бесплатные главы)")
        cb.safe_login('anon', 'нет .env')
        return None
    try:
        cb.safe_log("  вход на author.today...")
        if auth.login():
            if reader.apply_session_cookies(auth.get_cookies()):
                cb.safe_log("  ✓ авторизован — доступные аккаунту главы парсятся целиком")
                cb.safe_login('auth', auth.user_login)
            else:
                cb.safe_log("  ⚠ войти удалось, но сессию в браузер перенести не вышло — анонимно")
                cb.safe_login('anon', 'сессию не перенесено')
        else:
            cb.safe_log(f"  ⚠ вход не удался ({auth.last_error}) — работаю анонимно")
            cb.safe_login('anon', auth.last_error)
    except TwoFactorRequiredError as e:
        cb.safe_log(f"  ⚠ {e} — работаю анонимно")
        cb.safe_login('2fa', str(e))
    except Exception as e:
        logger.exception("Вход упал")
        cb.safe_log(f"  ⚠ вход не удался ({type(e).__name__}: {e}) — работаю анонимно")
        cb.safe_login('error', str(e))
        auth.close()
        return None
    return auth


def _recently_checked(history: Optional[History], book_id: int, settings: Settings) -> bool:
    if history is None or settings.skip_checked_hours <= 0:
        return False
    latest = history.latest(book_id)
    if not latest or latest.get('status') != 'ok':
        return False
    return (time.time() - float(latest.get('created_at') or 0)) < settings.skip_checked_hours * 3600


def _empty_record(book_id: int, url: str = '') -> Dict:
    return {
        'book_id': book_id,
        'url': url,
        'title': '',
        'author': '',
        'created_at': time.time(),
        'status': 'pending',
        'ai_percent': None,
        'human_percent': None,
        'verdict': '',
        'chapters_parsed': 0,
        'chapters_total': 0,
        'chapters_failed': 0,
        'chapters_locked': 0,
        'paid_chapter': None,
        'chars': 0,
        'chunks_total': 0,
        'chunks_failed': 0,
        'segments': 0,
        'error': '',
        'detail': '',
        'source': 'python',
        'text_source': 'parsed',
        'elapsed': 0.0,
        'stopped_early': False,
    }


def _detail(info: Dict, result: Optional[Dict] = None) -> str:
    payload = {
        'chapter_results': (info or {}).get('chapter_results', [])[:400],
        'failed': (info or {}).get('failed', [])[:80],
        'sources': (info or {}).get('sources', {}),
    }
    if result:
        payload['chunks'] = result.get('chunks', [])[:80]
        payload['raw_stats'] = result.get('raw_stats', {})
    return json.dumps(payload, ensure_ascii=False)[:60000]


def _log_record(cb: Callbacks, record: Dict) -> None:
    status = record['status']
    if status == 'ok':
        cb.safe_log(f"  ✅ {record.get('verdict')} — ИИ {record.get('ai_percent')}% "
                    f"/ человек {record.get('human_percent')}%")
    elif status == 'partial':
        cb.safe_log(f"  ⚠️ частичный результат: {record.get('error')}")
    else:
        cb.safe_log(f"  ❌ {status}: {record.get('error')}")
    if record.get('chapters_locked'):
        cb.safe_log(f"  💰 глав закрыто платным доступом: {record['chapters_locked']}")
    if record.get('chapters_failed'):
        cb.safe_log(f"  ⚠️ глав пропущено после попыток: {record['chapters_failed']}")
