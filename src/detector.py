"""
Yandex NeuroDetector API — отправка текста и получение результата анализа.
Endpoint: POST https://yandex.ru/lab/neurodetector/api/analyze/text

Текст книги отправляется фрагментами. Причина измерена на живом API: сервер обрывает
соединение на ~30 секунде обработки (400k символов — 21с и 200 OK, 700k — обрыв на
30.1с), а книга из 20 глав легко весит полмегабайта. Фрагменты склеиваются по границам
глав, а статистика суммируется: классификация идёт по сегментам, поэтому сумма по
фрагментам даёт тот же вердикт, что и один большой запрос.
"""
import logging
import random
import re
import time
from typing import Callable, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

API_ENDPOINT = "https://yandex.ru/lab/neurodetector/api/analyze/text"
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

# ~90k символов обрабатывается порядка 5 секунд — четырёхкратный запас до 30-секундного обрыва.
DEFAULT_CHUNK_CHARS = 90_000
# Сколько раз резать фрагмент пополам, если API сам заявит, что текст слишком длинный.
MAX_SPLIT_DEPTH = 4
# Дальше этого размера делить уже нечего: сегменты детектора короче не становятся.
MIN_SPLITTABLE_CHUNK = 250
RETRYABLE_STATUS = (408, 409, 425, 429, 500, 502, 503, 504, 509)
TOO_LONG_STATUS = (413,)
CHAPTER_MARK_RE = re.compile(r'^=== .+ ===\s*$', re.M)


class DetectorUnavailable(Exception):
    """API недоступен после всех попыток."""


class NeuroDetector:
    """Клиент для Yandex NeuroDetector API с чанкованием и повторами."""

    def __init__(
        self,
        chunk_chars: int = DEFAULT_CHUNK_CHARS,
        max_attempts: int = 4,
        timeout: float = 120.0,
        base_delay: float = 3.0,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
    ):
        self.chunk_chars = max(1000, int(chunk_chars))
        self.max_attempts = max(1, int(max_attempts))
        self.base_delay = base_delay
        self.on_progress = on_progress
        self._saw_short_text = False
        self.client = httpx.Client(
            headers={
                'User-Agent': USER_AGENT,
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'Origin': 'https://yandex.ru',
                'Referer': 'https://yandex.ru/lab/neurodetector',
            },
            follow_redirects=True,
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # Публичный интерфейс
    # ------------------------------------------------------------------
    def check_text(self, text: str) -> Dict:
        """
        Отправляет текст на анализ, дробя его на фрагменты.

        Args:
            text: Текст для проверки (весь, без обрезки)

        Returns:
            Словарь с ai_percent, human_percent, verdict и др.; при провале — {'error': ...}.
        """
        text = (text or '').strip()
        if not text:
            return {'error': 'Нечего отправлять: текст пустой'}

        chunks = split_text(text, self.chunk_chars)
        self._saw_short_text = False
        logger.info("Отправка в NeuroDetector: %d символов, %d фрагмент(ов)", len(text), len(chunks))

        total_chunks = len(chunks)
        done = 0
        aggregated = _empty_stats()
        chunk_logs: List[Dict] = []
        errors: List[str] = []

        for idx, chunk in enumerate(chunks, 1):
            self._progress(done, total_chunks, f"фрагмент {idx}/{total_chunks} ({len(chunk)} симв.)")
            try:
                stats, note = self._analyse_with_split(chunk, depth=0)
            except DetectorUnavailable as e:
                errors.append(f"фрагмент {idx}/{total_chunks}: {e}")
                chunk_logs.append({'chunk': idx, 'chars': len(chunk), 'ok': False, 'error': str(e)})
                done += 1
                continue

            _merge_stats(aggregated, stats)
            chunk_ai, chunk_human, _ = classify(stats)
            chunk_logs.append({'chunk': idx, 'chars': len(chunk), 'ok': True,
                               'ai': chunk_ai, 'human': chunk_human,
                               'note': note})
            done += 1
            self._progress(done, total_chunks, f"готово {done}/{total_chunks}")

            if idx < total_chunks:
                # Не превращаем пакетную проверку в DDoS детектора.
                time.sleep(min(1.5, self.base_delay / 2))

        if aggregated['segments_count'] == 0:
            return {'error': "; ".join(errors) or 'Детектор не вернул ни одного сегмента',
                    'chunks_total': total_chunks, 'chunks_failed': len(errors)}

        result = build_result(aggregated)
        result['chunks_total'] = total_chunks
        result['chunks_failed'] = len(errors)
        result['partial'] = bool(errors)
        result['chunks'] = chunk_logs

        warnings = []
        if errors:
            warnings.append(f"Проверено {total_chunks - len(errors)} фрагментов из "
                            f"{total_chunks}: " + "; ".join(errors)[:300])
            logger.warning("Частичный результат: %s", warnings[-1])
        if self._saw_short_text:
            result['short_text_warning'] = True
            warnings.append("Яндекс предупреждает: фрагмент короткий, на малом объёме точность ниже.")
            logger.info("API сообщил о коротком тексте (short_text_warning)")
        if warnings:
            result['warning'] = " | ".join(warnings)
        return result

    def close(self) -> None:
        try:
            if not self.client.is_closed:
                self.client.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Один фрагмент: повторы и адаптивное деление
    # ------------------------------------------------------------------
    def _analyse_with_split(self, chunk: str, depth: int) -> Tuple[Dict, str]:
        """Слишком длинный для API кусок режем пополам и проверяем двумя запросами."""
        note = ''
        try:
            stats = self._post_once(chunk)
            return stats, note
        except HttpApiError as e:
            if (e.status in TOO_LONG_STATUS or e.looks_too_long) and depth < MAX_SPLIT_DEPTH \
                    and len(chunk) >= MIN_SPLITTABLE_CHUNK * 2:
                half = len(chunk) // 2
                logger.info("API счёл фрагмент (%d симв.) слишком длинным (%s) — делю пополам",
                            len(chunk), e)
                s1, _ = self._analyse_with_split(chunk[:half], depth + 1)
                s2, _ = self._analyse_with_split(chunk[half:], depth + 1)
                merged = _empty_stats()
                _merge_stats(merged, s1)
                _merge_stats(merged, s2)
                return merged, f'split@{depth + 1}'
            raise DetectorUnavailable(str(e)) from e

    def _post_once(self, chunk: str) -> Dict:
        """Один запрос с повторами. Возвращает сырую статистику по фрагменту."""
        last_error: Optional[Exception] = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.client.post(API_ENDPOINT, json={'text': chunk})
            except (httpx.TimeoutException, httpx.TransportError) as e:
                # RemoteProtocolError("Server disconnected...") — тот самый обрыв на 30с.
                last_error = e
                logger.warning("Попытка %d/%d: сетевой сбой %s: %s",
                               attempt, self.max_attempts, type(e).__name__, e)
                self._sleep_before_retry(attempt, None)
                continue

            status = response.status_code
            if status in RETRYABLE_STATUS:
                last_error = HttpApiError(f"HTTP {status}", status=status, body=response.text[:200])
                logger.warning("Попытка %d/%d: HTTP %s", attempt, self.max_attempts, status)
                self._sleep_before_retry(attempt, response.headers.get('Retry-After'))
                continue
            if status >= 400:
                raise HttpApiError(f"HTTP {status}: {response.text[:200]}", status=status,
                                   body=response.text[:500])

            try:
                data = response.json()
            except ValueError as e:
                last_error = e
                logger.warning("Попытка %d/%d: ответ не JSON (%s)", attempt, self.max_attempts, e)
                self._sleep_before_retry(attempt, None)
                continue

            if not data.get('ok'):
                message = _api_message(data)
                err = HttpApiError(message or 'API вернул ok=false', status=status, body=message)
                # Отказ по длине/нагрузке имеет смысл попробовать ещё раз, но не «вечно».
                if err.looks_too_long or attempt == self.max_attempts:
                    raise err
                last_error = err
                logger.warning("Попытка %d/%d: ok=false (%s)", attempt, self.max_attempts, message)
                self._sleep_before_retry(attempt, None)
                continue

            results = data.get('results') or {}
            if results.get('short_text_warning'):
                self._saw_short_text = True
            return stats_from_results(results)

        raise DetectorUnavailable(f"нет ответа после {self.max_attempts} попыток: "
                                  f"{last_error or 'неизвестная ошибка'}")

    def _sleep_before_retry(self, attempt: int, retry_after: Optional[str]) -> None:
        if attempt >= self.max_attempts:
            return
        delay = self.base_delay * (2 ** (attempt - 1))
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        delay = min(delay, 60.0) + random.uniform(0, 1)
        logger.info("Пауза перед повтором: %.1fс", delay)
        time.sleep(delay)

    def _progress(self, done: int, total: int, note: str) -> None:
        if self.on_progress:
            try:
                self.on_progress(done, total, note)
            except Exception:
                pass


class HttpApiError(Exception):
    """HTTP-ошибка детектора; status может быть None (например, ok=false в валидном JSON)."""

    TOO_LONG_PATTERNS = ('слишком длинн', 'too long', 'max', 'length', 'превышен', 'больш')

    def __init__(self, message: str, status: Optional[int] = None, body: str = ''):
        super().__init__(message)
        self.status = status
        self.body = body or ''

    @property
    def looks_too_long(self) -> bool:
        probe = (str(self) + ' ' + self.body).lower()
        if self.status in TOO_LONG_STATUS:
            return True
        return any(p in probe for p in self.TOO_LONG_PATTERNS)


# ==============================================================================
# Разбиение текста
# ==============================================================================
def split_text(text: str, chunk_chars: int) -> List[str]:
    """Режет текст на фрагменты не длиннее chunk_chars, стараясь не рвать главы.

    Сначала — по маркерам глав ('=== Название ==='), затем по абзацам, и только
    для одной слишком большой главы — жёстко по предложениям.
    """
    if len(text) <= chunk_chars:
        return [text]

    blocks = _split_into_blocks(text)
    chunks: List[str] = []
    current = ''

    for block in blocks:
        if len(block) > chunk_chars:
            if current:
                chunks.append(current)
                current = ''
            chunks.extend(_hard_split(block, chunk_chars))
            continue

        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > chunk_chars:
            chunks.append(current)
            current = block
        else:
            current = candidate

    if current:
        chunks.append(current)
    return chunks


def _split_into_blocks(text: str) -> List[str]:
    """Главы, помеченные сборщиком текста, — идеальный край фрагмента."""
    marks = list(CHAPTER_MARK_RE.finditer(text))
    if len(marks) >= 2:
        blocks = []
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            blocks.append(text[m.start():end].strip())
        prefix = text[:marks[0].start()].strip()
        if prefix:
            blocks.insert(0, prefix)
        return [b for b in blocks if b]

    paragraphs = [p for p in re.split(r'\n\s*\n', text) if p.strip()]
    if len(paragraphs) > 1:
        return paragraphs
    return [text]


def _hard_split(block: str, chunk_chars: int) -> List[str]:
    """Жёсткая нарезка одного переростка — по границам предложений, не по середине слова."""
    out: List[str] = []
    rest = block
    while len(rest) > chunk_chars:
        window = rest[:chunk_chars]
        cut = max(window.rfind('. '), window.rfind('! '), window.rfind('? '),
                  window.rfind('\n'), window.rfind(' '))
        if cut < chunk_chars * 0.5:
            # Разделителя нет (сплошной поток символов) — режем ровно по лимиту.
            end = chunk_chars
        else:
            end = cut + 1
        out.append(rest[:end].strip())
        rest = rest[end:]
    if rest.strip():
        out.append(rest.strip())
    return out


# ==============================================================================
# Статистика и вердикт
# ==============================================================================
def _api_message(data: Dict) -> str:
    err = data.get('error')
    if isinstance(err, dict):
        return str(err.get('message') or err.get('code') or err)[:300]
    return str(err or data.get('message') or '')[:300]


def _empty_stats() -> Dict:
    return {'segments_count': 0, 'AI_count': 0, 'LIKELY_AI_count': 0,
            'HUMAN_count': 0, 'LIKELY_HUMAN_count': 0}


def stats_from_results(results: Dict) -> Dict:
    """Достает счётчики сегментов из ответа API одного фрагмента."""
    stats = results.get('stats') or {}
    if not stats:
        # Ответ без stats, но с сегментами — считаем руками, чтобы не потерять результат.
        segments = results.get('segments') or []
        stats = _empty_stats()
        for seg in segments:
            label = str(seg.get('type') or seg.get('label') or seg.get('status') or '').upper()
            key = {'AI': 'AI_count', 'LIKELY_AI': 'LIKELY_AI_count',
                   'HUMAN': 'HUMAN_count', 'LIKELY_HUMAN': 'LIKELY_HUMAN_count'}.get(label)
            if key:
                stats[key] += 1
        stats['segments_count'] = len(segments)
        return stats

    out = _empty_stats()
    for key in out:
        value = stats.get(key)
        out[key] = int(value) if isinstance(value, (int, float)) else 0
    if not out['segments_count']:
        out['segments_count'] = out['AI_count'] + out['LIKELY_AI_count'] + \
            out['HUMAN_count'] + out['LIKELY_HUMAN_count']
    return out


def _merge_stats(total: Dict, part: Dict) -> None:
    for key in total:
        total[key] += int(part.get(key, 0) or 0)


def classify(stats: Dict) -> Tuple[int, int, float]:
    """(ai, human, ai_percent) по той же формуле, что и расширение.

    Знаменатель — классифицированные сегменты, а не segments_count: иначе несожжённые
    Яндексом сегменты тихо раздувают «человечность», и две программы дают разные verdict'ы.
    """
    ai = int(stats.get('AI_count', 0)) + int(stats.get('LIKELY_AI_count', 0))
    human = int(stats.get('HUMAN_count', 0)) + int(stats.get('LIKELY_HUMAN_count', 0))
    classified = ai + human
    ai_percent = round(ai / classified * 100, 1) if classified else 0.0
    return ai, human, ai_percent


def verdict_for(ai_percent: float) -> str:
    if ai_percent >= 50:
        return "Большая часть текста вероятно сгенерирована ИИ"
    if ai_percent >= 5:
        return "Часть текста вероятно сгенерирована ИИ"
    return "Текст скорее всего написан человеком"


def build_result(stats: Dict) -> Dict:
    """Собирает итоговый словарь из агрегированной статистики всех фрагментов."""
    ai, human, ai_pct = classify(stats)
    classified = ai + human
    human_pct = round(human / classified * 100, 1) if classified else 0.0
    return {
        'total_segments': int(stats.get('segments_count', 0) or 0),
        'classified_segments': classified,
        'ai_count': ai,
        'human_count': human,
        'ai_percent': ai_pct,
        'human_percent': human_pct,
        'verdict': verdict_for(ai_pct),
        'raw_stats': dict(stats),
    }
