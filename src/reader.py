"""
Парсинг книг с author.today — оглавление + текст глав.

Два слоя, потому что сайт отдаёт разный HTML:
  1. Оглавление и признаки платного доступа лежат в чистом HTML страницы /work/{id} (httpx).
     Закрытая глава отображается как <li> БЕЗ ссылки /reader/ с i.icon-lock и
     data-hint="Платный доступ" — это точный признак, доступный до запуска браузера.
  2. Сам текст главы рендерится только через Knockout.js, поэтому за текстом
     идёт headless Chrome (Selenium).

Загруженность глав различается статусами (см. ChapterStatus): ошибка парсинга одной
главы НЕ должна означать конец книги. Платная глава — означает.
"""
import logging
import random
import re
import time
from typing import Callable, Dict, List, Optional, Tuple

import httpx
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

try:
    from webdriver_manager.chrome import ChromeDriverManager
except ImportError:  # webdriver-manager необязателен: есть Selenium Manager внутри selenium>=4.6
    ChromeDriverManager = None

from src.models import BookInfo, Chapter, ChapterResult, ChapterStatus

logger = logging.getLogger(__name__)

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

# То, что глава закрыта платным доступом, в оглавлении выглядит так:
LOCK_HINT_RE = re.compile(r'платн|купить глав|приобрет|закрыт', re.I)
READER_HREF_RE = re.compile(r'/reader/(\d+)/(\d+)')

# Ниже этого порога считаем, что текста нет (нормальные главы на AT — тысячи символов).
MIN_CHAPTER_CHARS = 120

# Индикаторы замка в уже отрендеренной странице читалки (второй, резервный слой проверки).
RENDERED_LOCK_MARKERS = (
    'платный доступ', 'для продолжения чтения', 'закрытый контент',
    'чтобы продолжить чтение', 'оплатите доступ', 'приобретите доступ',
)
RENDERED_LOCK_SELECTOR = '.authorize-form, .buy-form, .content-lock, [class*="content-lock"]'

# Извлечение текста выполняется одним JS-вызовом: быстрее и устойчивее, чем
# последовательность find_element, и заодно даёт признаки замка в той же порции.
EXTRACT_JS = """
const container = document.querySelector('#text-container');
const out = {ready: document.readyState, pCount: 0, pLen: 0, pText: '', cText: '',
             bodyText: '', hasContainer: !!container, locks: [], lockHints: []};
if (container) {
    const ps = container.querySelectorAll('p');
    out.pCount = ps.length;
    const parts = [];
    let total = 0;
    for (const p of ps) {
        const t = (p.textContent || '').trim();
        if (t) { parts.push(t); total += t.length; }
    }
    out.pText = parts.join('\\n\\n');
    out.pLen = total;
    out.cText = (container.textContent || '').trim();
}
const lockEls = document.querySelectorAll(arguments[0] || '%s');
for (const el of lockEls) {
    const st = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    if (st.display === 'none' || st.visibility === 'hidden' || r.height <= 0) continue;
    out.locks.push(el.className || el.tagName);
    const txt = (el.textContent || '').toLowerCase();
    if (txt.length) out.lockHints.push(txt.slice(0, 300));
}
const hints = document.querySelectorAll('[data-hint]');
for (const el of hints) {
    const h = (el.getAttribute('data-hint') || '').toLowerCase();
    if (/платн|купить|приобрет/.test(h)) out.lockHints.push(h);
}
const body = document.body;
out.bodyText = body ? (body.innerText || '').slice(0, 15000) : '';
return out;
""" % RENDERED_LOCK_SELECTOR


class AuthorTodayReader:
    """Загрузка оглавления и текста глав с author.today"""

    def __init__(
        self,
        headless: bool = True,
        max_attempts: int = 3,
        render_wait: float = 12.0,
        page_load_timeout: float = 45.0,
        block_images: bool = False,
    ):
        self.headless = headless
        self.max_attempts = max(1, int(max_attempts))
        self.render_wait = render_wait
        self.page_load_timeout = page_load_timeout
        self.block_images = block_images

        self.client = httpx.Client(
            base_url='https://author.today',
            headers={
                'User-Agent': USER_AGENT,
                'Accept-Language': 'ru-RU,ru;q=0.9',
            },
            follow_redirects=True,
            timeout=30.0,
        )
        self._driver: Optional[WebDriver] = None
        self._session_cookies: Dict[str, str] = {}
        self.logged_in = False

    # ------------------------------------------------------------------
    # Сессия (авторизация)
    # ------------------------------------------------------------------
    def apply_session_cookies(self, cookies: Dict[str, str]) -> bool:
        """Переносит куки авторизованного httpx-клиента в Chrome.

        Возвращает False, если перенести не удалось — тогда парсинг продолжится
        анонимно, как и раньше.
        """
        if not cookies:
            return False
        self._session_cookies = dict(cookies)
        try:
            self._get_driver()
            ok = self._inject_cookies()
            self.logged_in = ok
            if ok:
                logger.info("Сессия перенесена в Chrome (%d куки)", len(cookies))
            return ok
        except Exception as e:
            logger.error("Не удалось перенести сессию в Chrome: %s", e)
            return False

    def _inject_cookies(self) -> bool:
        """Только что поднятый Chrome пуст — куки нужно положить заново."""
        if not self._driver or not self._session_cookies:
            return False
        try:
            self._driver.get('https://author.today/')
        except WebDriverException as e:
            logger.warning("Не удалось открыться для переноса куки: %s", str(e).splitlines()[0])
            return False
        for name, value in self._session_cookies.items():
            try:
                self._driver.add_cookie({'name': name, 'value': value,
                                         'domain': 'author.today', 'path': '/'})
            except WebDriverException as e:
                logger.warning("Куки %s не перенеслось: %s", name, e)
        return True

    # ------------------------------------------------------------------
    # Selenium — драйвер для рендеринга JS, с самовосстановлением
    # ------------------------------------------------------------------
    def _build_driver(self) -> WebDriver:
        options = self._chrome_options()
        errors = []

        for factory in self._driver_factories():
            try:
                driver = factory(options)
            except Exception as e:
                errors.append(f"{factory.__name__}: {e}")
                logger.warning("Драйвер не поднялся: %s", e)
                continue
            driver.set_page_load_timeout(self.page_load_timeout)
            driver.set_script_timeout(60)
            return driver

        raise RuntimeError("Не удалось запустить Chrome для парсинга: " + " | ".join(errors))

    @staticmethod
    def _driver_factories():
        """Сначала webdriver-manager (он скачан install.bat), потом Selenium Manager."""
        def via_wdm(options):
            service = Service(ChromeDriverManager().install())
            return webdriver.Chrome(service=service, options=options)

        def via_selenium_manager(options):
            return webdriver.Chrome(options=options)

        factories = []
        if ChromeDriverManager is not None:
            factories.append(via_wdm)
        factories.append(via_selenium_manager)
        return factories

    def _chrome_options(self) -> Options:
        options = Options()
        if self.headless:
            # headless=new — современный режим (Chrome 109+); в старых версиях его нет.
            options.add_argument('--headless=new')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--disable-extensions')
        options.add_argument('--no-first-run')
        options.add_argument('--no-default-browser-check')
        options.add_argument('--lang=ru-RU')
        # Ширина влияет на то, что сайт считает «видимым» — важно для проверки замка.
        options.add_argument('--window-size=1920,1080')
        options.add_argument(f'user-agent={USER_AGENT}')
        options.add_experimental_option('excludeSwitches', ['enable-logging'])
        options.add_experimental_option('excludeSwitches', ['enable-automation'])
        options.add_experimental_option('useAutomationExtension', False)
        prefs = {'credentials_enable_service': False,
                 'profile.password_manager_enabled': False}
        if self.block_images:
            prefs['profile.default_content_setting_values.images'] = 2
        options.add_experimental_option('prefs', prefs)
        return options

    def _get_driver(self) -> WebDriver:
        """Ленивое создание браузера."""
        if self._driver is None:
            logger.info("Запускаю Chrome (headless=%s)", self.headless)
            self._driver = self._build_driver()
        return self._driver

    def _restart_driver(self, reason: str) -> None:
        """Браузер после краша обычно уже не способен ничего отдать — поднимаем новый."""
        logger.warning("Перезапускаю Chrome: %s", reason)
        self.close_driver()
        try:
            self._driver = self._build_driver()
        except Exception as e:
            logger.error("Chrome не поднялся повторно: %s", e)
            self._driver = None
            self.logged_in = False
            return
        # Сессия живёт в куки конкретного браузера: без переноса авторизованный прогон
        # упал бы в анонимный и начал бы считать платными обычные главы аккаунта.
        if self._session_cookies and not self._inject_cookies():
            logger.warning("Сессию в новый Chrome перенести не удалось — главы могут показаться платными")
            self.logged_in = False

    def close_driver(self) -> None:
        """Закрытие браузера (вызывается после завершения проверки)."""
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception as e:
                logger.warning("Chrome закрылся с ошибкой: %s", e)
            self._driver = None

    # ------------------------------------------------------------------
    # Основной метод — парсинг книги
    # ------------------------------------------------------------------
    def parse_book(
        self,
        book_id: int,
        max_chapters: int = 20,
        delay: float = 2.0,
        progress_callback: Optional[Callable] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Tuple[str, Dict]:
        """
        Загружает оглавление со страницы книги, парсит главы через Selenium.

        Args:
            book_id: ID книги (из URL /work/{id})
            max_chapters: Максимум глав для парсинга
            delay: Пауза между запросами (сек)
            progress_callback: Функция(num, total, title, status) для логов
            should_stop: Функция, возвращающая True, если пользователь нажал «Стоп»

        Returns:
            (объединённый_текст, словарь_информации)
        """
        started = time.time()
        logger.info("Парсинг книги %s", book_id)

        info = _empty_chapters_info(book_id)

        html, fetch_error = self._fetch_work_page(book_id)
        if html is None:
            info['error'] = fetch_error
            return "", info

        book_info = _parse_book_info(html, book_id)
        chapters, paid_in_toc = _parse_toc(html, book_id)
        info['book_title'] = book_info.title
        info['book_author'] = book_info.author
        info['total_chapters_in_toc'] = len(chapters) + paid_in_toc
        info['free_in_toc'] = len(chapters)
        info['paid_in_toc'] = paid_in_toc

        if not chapters:
            if paid_in_toc:
                info['error'] = 'Все главы закрыты платным доступом'
                info['paid_chapter'] = 1
                info['paid_stopped'] = True
            else:
                info['error'] = 'Оглавление не найдено'
            return "", info

        logger.info("В оглавлении %d доступных глав, %d закрыто", len(chapters), paid_in_toc)

        to_parse = chapters[:max_chapters]
        total = len(to_parse)
        info['attempted'] = total

        blocks: List[str] = []
        parsed = 0
        total_chars = 0

        for i, chapter in enumerate(to_parse):
            num = i + 1

            if should_stop and should_stop():
                info['stopped_early'] = True
                _emit(progress_callback, num, total, chapter.title, "остановлено пользователем")
                break

            if i > 0 and delay > 0:
                time.sleep(delay + random.uniform(0, 1))

            _emit(progress_callback, num, total, chapter.title, f"парсю (до {self.max_attempts} попыток)...")
            res = self._load_chapter(chapter)
            res.elapsed = round(res.elapsed, 1)
            info['chapter_results'].append(_chapter_record(res, num))

            if res.status is ChapterStatus.OK:
                blocks.append(f"=== {chapter.title} ===\n\n{res.text}\n\n")
                parsed += 1
                total_chars += res.chars
                _emit(progress_callback, num, total, chapter.title,
                      f"OK ({res.chars} симв., {res.source}, попыток {res.attempts})")
                if res.source != 'container-p':
                    logger.warning("Глава %r получена из %s — возможен посторонний текст в анализе",
                                   chapter.title, res.source)
            elif res.status is ChapterStatus.PAID:
                info['paid_chapter'] = chapter.chapter_id or None
                info['paid_stopped'] = True
                _emit(progress_callback, num, total, chapter.title, "ПЛАТНАЯ — остановка")
                break
            else:
                info['failed'].append({'num': num, 'title': chapter.title,
                                       'reason': res.error or res.status.value,
                                       'attempts': res.attempts})
                _emit(progress_callback, num, total, chapter.title,
                      f"ПРОПУЩЕНА после {res.attempts} попыток: {res.error or res.status.value}")
                logger.warning("Глава %r пропущена (%s), продолжаю книгу", chapter.title,
                               res.status.value)

            info['sources'][res.source or res.status.value] = \
                info['sources'].get(res.source or res.status.value, 0) + 1

        info['parsed'] = parsed
        info['total_chars'] = total_chars
        info['elapsed'] = round(time.time() - started, 1)
        logger.info("Завершено: %d глав из %d попыток, %d символов, %0.1fс",
                    parsed, total, total_chars, info['elapsed'])
        return "\n".join(blocks), info

    # ------------------------------------------------------------------
    # Глава: попытки, рендер, извлечение
    # ------------------------------------------------------------------
    def _load_chapter(self, chapter: Chapter) -> ChapterResult:
        """Грузит одну главу с ретраями. PAID возвращается сразу, ERROR/EMPTY — повторяются."""
        t0 = time.time()
        last = ChapterResult(chapter=chapter, status=ChapterStatus.ERROR, source='none',
                             error='глава не парсилась')

        for attempt in range(1, self.max_attempts + 1):
            try:
                driver = self._get_driver()
                logger.debug("Открываю %s (попытка %d)", chapter.url, attempt)
                driver.get(chapter.url)
                last = self._extract_after_render(driver, chapter)
            except TimeoutException as e:
                last = ChapterResult(chapter=chapter, status=ChapterStatus.ERROR, source='none',
                                     error=f"таймаут загрузки ({type(e).__name__})")
            except WebDriverException as e:
                msg = str(e).splitlines()[0] if str(e) else type(e).__name__
                last = ChapterResult(chapter=chapter, status=ChapterStatus.ERROR, source='none',
                                     error=f"браузер: {msg}")
                # Умерший драйвер не «оживет» сам — поднимаем новый до следующей попытки.
                if attempt < self.max_attempts:
                    self._restart_driver(msg)

            last.attempts = attempt
            last.elapsed = time.time() - t0

            if last.status in (ChapterStatus.OK, ChapterStatus.PAID):
                return last

            if attempt < self.max_attempts:
                backoff = 1.5 * attempt + random.uniform(0, 0.5)
                logger.info("Попытка %d не удалась (%s), повтор через %.1fс",
                            attempt, last.error, backoff)
                time.sleep(backoff)

        return last

    def _extract_after_render(self, driver: WebDriver, chapter: Chapter) -> ChapterResult:
        """Ждёт JS-рендер и извлекает текст. Ждёт стабильности, а не фиксированные секунды."""
        try:
            WebDriverWait(driver, min(20, self.render_wait + 5)).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '#text-container')))
        except Exception:
            logger.debug("#text-container не появился за отведённое время: %s", chapter.url)

        snapshot = self._snapshot(driver)
        if snapshot is None:
            return ChapterResult(chapter=chapter, status=ChapterStatus.ERROR, source='none',
                                 error='страница не ответила (JS недоступен)')

        # Дожидаемся, пока объём текста перестанет расти: Knockout дописывает абзацы порциями.
        # Ждём именно стабильности, а не первого попавшегося куска текста, иначе глава
        # ушла бы в анализ недогрузившейся.
        deadline = time.time() + self.render_wait
        stable = 0
        prev_len = snapshot.get('pLen', 0)
        while time.time() < deadline:
            if prev_len >= MIN_CHAPTER_CHARS and stable >= 2:
                break
            time.sleep(0.4)
            current = self._snapshot(driver)
            if current is None:
                return ChapterResult(chapter=chapter, status=ChapterStatus.ERROR, source='none',
                                     error='страница отвалилась при рендере')
            length = current.get('pLen', 0)
            stable = stable + 1 if length == prev_len else 0
            prev_len = length
            snapshot = current
            if _looks_locked(current) and length < MIN_CHAPTER_CHARS:
                break

        locked = _looks_locked(snapshot)
        text, source = _pick_text(snapshot)

        if len(text) >= MIN_CHAPTER_CHARS:
            return ChapterResult(chapter=chapter, status=ChapterStatus.OK, text=text,
                                 source=source)
        if locked:
            return ChapterResult(chapter=chapter, status=ChapterStatus.PAID, source='none',
                                 error='подтверждённый платный доступ')
        if not snapshot.get('hasContainer'):
            return ChapterResult(chapter=chapter, status=ChapterStatus.ERROR, source='none',
                                 error='нет #text-container на странице')
        return ChapterResult(chapter=chapter, status=ChapterStatus.EMPTY, source='none',
                             text='', error='рендер не вернул текста')

    def _snapshot(self, driver: WebDriver) -> Optional[dict]:
        try:
            return driver.execute_script(EXTRACT_JS)
        except WebDriverException as e:
            logger.warning("JS-извлечение не сработало: %s", str(e).splitlines()[0])
            return None

    # ------------------------------------------------------------------
    # Страница книги (httpx, с ретраями)
    # ------------------------------------------------------------------
    def _fetch_work_page(self, book_id: int, attempts: int = 3) -> Tuple[Optional[str], str]:
        """Один запрос на книгу: из него берём и заголовок, и оглавление."""
        last_error = ''
        for attempt in range(1, attempts + 1):
            try:
                resp = self.client.get(f'/work/{book_id}')
                resp.raise_for_status()
                return resp.text, ''
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                last_error = f"страница книги ответила {code}"
                # 404/403/410 повторять бессмысленно
                if code in (401, 403, 404, 410):
                    return None, last_error
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
            logger.warning("Страница книги %s, попытка %d: %s", book_id, attempt, last_error)
            if attempt < attempts:
                time.sleep(2 * attempt + random.uniform(0, 1))
        return None, f"не удалось загрузить страницу книги: {last_error}"

    def close(self) -> None:
        """Закрывает и HTTP-клиент, и браузер."""
        try:
            if not self.client.is_closed:
                self.client.close()
        except Exception:
            pass
        self.close_driver()


# ==============================================================================
# Разбор HTML оглавления (без браузера)
# ==============================================================================
def _parse_book_info(html: str, book_id: int) -> BookInfo:
    soup = BeautifulSoup(html, 'lxml')
    h1 = soup.find('h1')
    title = h1.get_text(strip=True) if h1 else f"Book {book_id}"
    # Автор лежит в .book-authors; поиск по первой ссылке /u/ натыкается на раздел навигации.
    author = ''
    block = soup.find(class_='book-authors')
    if block:
        author = block.get_text(' ', strip=True)
    if not author:
        tag = soup.find('a', href=re.compile(r'^/u/'))
        author = tag.get_text(strip=True) if tag else ''
    return BookInfo(book_id=book_id, title=title,
                    url=f"https://author.today/work/{book_id}",
                    author=author)


def _parse_toc(html: str, book_id: int) -> Tuple[List[Chapter], int]:
    """Список доступных глав + число закрытых.

    Порядок — как в документе: сортировка по chapter_id ломает многотомники и
    ставила бы закрытые главы (у них нет id) в начало.
    """
    soup = BeautifulSoup(html, 'lxml')
    ul = soup.find('ul', class_='table-of-content')
    chapters: List[Chapter] = []
    paid = 0
    if not ul:
        return chapters, paid

    for li in ul.find_all('li'):
        link = li.find('a', href=READER_HREF_RE)
        title = (link.get_text(strip=True) if link else li.get_text(strip=True)).strip()
        # В строке мог быть лишний текст (дата, счётчик) — режем по ссылке, если она есть.
        if link is None and not title:
            continue

        if link is not None:
            m = READER_HREF_RE.search(link['href'])
            if not m:
                continue
            cid = int(m.group(2))
            url = f"https://author.today{link['href']}"
            # Ссылка есть, но рядом висит замок: так бывает у авторизованного без покупки.
            if _li_is_locked(li):
                paid += 1
                continue
            chapters.append(Chapter(chapter_id=cid, title=title or f"Глава {cid}", url=url,
                                    published_at=_published_at(li)))
        else:
            # Строки оглавления без ссылки — это либо закрытая глава, либо служебный элемент.
            # Терять их молча нельзя: иначе «распарсено 4 из 4» будет выглядеть полным
            # ответом для книги, где половина глав под замком.
            if _li_is_locked(li) or _has_publish_time(li):
                paid += 1
            # Служебную строку («показать все», заголовок тома) не считаем вовсе.

    return chapters, paid


def _li_is_locked(li) -> bool:
    if li.find('i', class_='icon-lock') or li.find('span', class_='icon-lock'):
        return True
    for el in li.find_all(attrs={'data-hint': True}):
        if LOCK_HINT_RE.search(el.get('data-hint', '')):
            return True
    return False


def _has_publish_time(li) -> bool:
    """Признак строки главы, а не служебного элемента оглавления.

    Взят с живого сайта: у любой главы (открытой и закрытой) есть дата публикации
    (data-time). У «показать все главы» и заголовков томов её нет.
    """
    return bool(li.find_all(attrs={'data-time': True}))


def _published_at(li) -> str:
    for el in li.find_all(attrs={'data-time': True}):
        return el['data-time'][:10]
    return ""


# ==============================================================================
# Разбор отрендеренной страницы главы
# ==============================================================================
def _looks_locked(snapshot: dict) -> bool:
    """Замок в рендере: видимый элемент блокировки с текстом-просьбой оплатить."""
    if not snapshot.get('locks'):
        return False
    for hint in snapshot.get('lockHints') or []:
        if any(marker in hint for marker in RENDERED_LOCK_MARKERS):
            return True
    return False


def _pick_text(snapshot: dict) -> Tuple[str, str]:
    """Цепочка источников: абзацы контейнера → текст контейнера → body (крайний случай)."""
    p_text = (snapshot.get('pText') or '').strip()
    if snapshot.get('pCount', 0) > 0 and len(p_text) >= MIN_CHAPTER_CHARS:
        return p_text, 'container-p'

    c_text = (snapshot.get('cText') or '').strip()
    if len(c_text) >= MIN_CHAPTER_CHARS:
        return c_text, 'container'

    body = (snapshot.get('bodyText') or '').strip()
    if len(body) >= MIN_CHAPTER_CHARS:
        return body, 'body'

    return (p_text or c_text or body), 'none'


def _empty_chapters_info(book_id: int) -> Dict:
    return {
        'book_id': book_id,
        'book_title': '',
        'book_author': '',
        'total_chapters_in_toc': 0,
        'free_in_toc': 0,
        'paid_in_toc': 0,
        'attempted': 0,
        'parsed': 0,
        'failed': [],
        'chapter_results': [],
        'sources': {},
        'paid_chapter': None,
        'paid_stopped': False,
        'stopped_early': False,
        'total_chars': 0,
        'elapsed': 0.0,
        'error': '',
    }


def _chapter_record(res: ChapterResult, num: int) -> Dict:
    return {
        'num': num,
        'title': res.chapter.title,
        'chapter_id': res.chapter.chapter_id,
        'status': res.status.value,
        'chars': res.chars,
        'source': res.source,
        'attempts': res.attempts,
        'error': res.error,
        'elapsed': res.elapsed,
    }


def _emit(callback: Optional[Callable], num: int, total: int, title: str, status: str) -> None:
    if callback:
        try:
            callback(num, total, title, status)
        except Exception:
            pass
