"""
Парсинг книг с author.today — оглавление + текст глав через Selenium.
Останавливается на первой платной главе.
"""
import httpx
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import logging
import re
import time
import random
from typing import Optional, List, Tuple, Dict, Callable

from src.models import Chapter, BookInfo

logger = logging.getLogger(__name__)


class AuthorTodayReader:
    """Загрузка оглавления и текста глав с author.today"""

    def __init__(self):
        self.client = httpx.Client(
            base_url='https://author.today',
            headers={
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                ),
                'Accept-Language': 'ru-RU,ru;q=0.9',
            },
            follow_redirects=True,
            timeout=30.0,
        )
        self._driver = None

    # ------------------------------------------------------------------
    # Selenium — драйвер для рендеринга JS
    # ------------------------------------------------------------------
    def _get_driver(self):
        """Ленивое создание headless Chrome"""
        if self._driver is None:
            options = Options()
            options.add_argument('--headless')
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            options.add_argument('--disable-gpu')
            options.add_argument(
                'user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            service = Service(ChromeDriverManager().install())
            self._driver = webdriver.Chrome(service=service, options=options)
            self._driver.set_page_load_timeout(30)
        return self._driver

    def close_driver(self):
        """Закрытие браузера (вызывается после завершения проверки)"""
        if self._driver:
            self._driver.quit()
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
    ) -> Tuple[str, Dict]:
        """
        Загружает оглавление со страницы книги, парсит главы через Selenium.

        Args:
            book_id: ID книги (из URL /work/{id})
            max_chapters: Максимум глав для парсинга
            delay: Пауза между запросами (сек)
            progress_callback: Функция(num, total, title, status) для логов

        Returns:
            (объединённый_текст, словарь_информации)
        """
        logger.info("Парсинг книги %s", book_id)

        book_info = self._get_book_info(book_id)
        if not book_info:
            return "", {"error": "Не удалось получить информацию о книге"}

        chapters = self._get_toc(book_id, delay)
        if not chapters:
            return "", {"error": "Оглавление не найдено"}

        logger.info("Найдено %d глав в оглавлении", len(chapters))

        combined_text = []
        parsed_count = 0
        total_chars = 0
        paid_chapter = None

        for i, chapter in enumerate(chapters[:max_chapters]):
            num = i + 1
            total = min(len(chapters), max_chapters)

            if progress_callback:
                progress_callback(num, total, chapter.title, "начинаю парсинг...")

            if i > 0:
                time.sleep(delay + random.uniform(0, 1))

            text = self._parse_chapter(chapter.url, delay)

            if text:
                combined_text.append(f"=== {chapter.title} ===\n\n{text}\n\n")
                parsed_count += 1
                total_chars += len(text)
                if progress_callback:
                    progress_callback(num, total, chapter.title, f"OK ({len(text)} симв.)")
            else:
                if self._is_paid_page(chapter.url):
                    paid_chapter = chapter.chapter_id
                    if progress_callback:
                        progress_callback(num, total, chapter.title, "ПЛАТНАЯ — остановка")
                    break
                else:
                    if progress_callback:
                        progress_callback(num, total, chapter.title, "ошибка получения текста")

        chapters_info = {
            "book_id": book_id,
            "book_title": book_info.title,
            "total_chapters_in_toc": len(chapters),
            "parsed": parsed_count,
            "paid_chapter": paid_chapter,
            "total_chars": total_chars,
        }
        logger.info("Завершено: %d глав, %d символов", parsed_count, total_chars)
        return "\n".join(combined_text), chapters_info

    # ------------------------------------------------------------------
    # Информация о книге (страница /work/{id})
    # ------------------------------------------------------------------
    def _get_book_info(self, book_id: int) -> Optional[BookInfo]:
        """Загружает заголовок и автора со страницы произведения"""
        try:
            resp = self.client.get(f'/work/{book_id}')
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, 'lxml')

            title_tag = soup.find('h1')
            title = title_tag.get_text(strip=True) if title_tag else f"Book {book_id}"

            author_tag = soup.find('a', href=re.compile(r'/u/'))
            author = author_tag.get_text(strip=True) if author_tag else ""

            return BookInfo(
                book_id=book_id,
                title=title,
                url=f"https://author.today/work/{book_id}",
                author=author,
            )
        except Exception as e:
            logger.error("Ошибка получения информации о книге %s: %s", book_id, e)
            return None

    # ------------------------------------------------------------------
    # Оглавление (ul.table-of-content на странице /work/{id})
    # ------------------------------------------------------------------
    def _get_toc(self, book_id: int, delay: float = 2.0) -> List[Chapter]:
        """
        Извлекает список глав из вкладки «Оглавление» на странице книги.
        """
        try:
            resp = self.client.get(f'/work/{book_id}')
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, 'lxml')

            chapters = []
            toc_ul = soup.find('ul', class_='table-of-content')
            if toc_ul:
                for li in toc_ul.find_all('li'):
                    link = li.find('a', href=re.compile(r'/reader/\d+/\d+'))
                    if not link:
                        continue
                    chapter_id = self._extract_chapter_id(link['href'])
                    title = link.get_text(strip=True)
                    if chapter_id and title:
                        href = link['href']
                        url = f"https://author.today{href}" if href.startswith('/') else href
                        chapters.append(Chapter(chapter_id=chapter_id, title=title, url=url))

            chapters.sort(key=lambda c: c.chapter_id)
            return chapters
        except Exception as e:
            logger.error("Ошибка оглавления книги %s: %s", book_id, e)
            return []

    # ------------------------------------------------------------------
    # Текст главы (Selenium + #text-container)
    # ------------------------------------------------------------------
    def _parse_chapter(self, url: str, delay: float = 2.0) -> Optional[str]:
        """Загружает страницу главы в Selenium, извлекает текст после JS-рендеринга"""
        try:
            driver = self._get_driver()
            logger.info("Открываю %s", url)
            driver.get(url)

            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, '#text-container'))
                )
                time.sleep(3)  # даём JS время на рендеринг
            except Exception:
                logger.warning("Таймаут ожидания текста: %s", url)

            soup = BeautifulSoup(driver.page_source, 'lxml')
            if self._check_paid_page(soup):
                return None

            return self._extract_text_selenium(driver)
        except Exception as e:
            logger.error("Ошибка парсинга главы %s: %s", url, e)
            return None

    def _extract_text_selenium(self, driver) -> Optional[str]:
        """Извлекает текст из #text-container > p"""
        try:
            container = driver.find_element(By.CSS_SELECTOR, '#text-container')
            paragraphs = container.find_elements(By.TAG_NAME, 'p')
            texts = [p.text for p in paragraphs if p.text.strip()]
            if texts:
                return '\n\n'.join(texts)

            # Fallback: весь текст body
            body = driver.find_element(By.TAG_NAME, 'body')
            return self._clean_text(body.text[:15000]) if body.text else None
        except Exception as e:
            logger.error("Ошибка извлечения текста: %s", e)
            return None

    # ------------------------------------------------------------------
    # Детекция платных страниц
    # ------------------------------------------------------------------
    def _check_paid_page(self, soup: BeautifulSoup) -> bool:
        """Ищет индикаторы платного доступа в ключевых контейнерах"""
        containers = soup.find_all(
            ['div', 'form'],
            class_=re.compile(r'authorize|buy|purchase|paid|content-lock', re.I)
        )
        containers.extend(
            soup.find_all(['div'], id=re.compile(r'content-lock|buy-content', re.I))
        )

        strict = [
            'платный доступ', 'для продолжения чтения', 'закрытый контент',
            'paid access', 'чтобы продолжить чтение', 'доступ к платным',
        ]

        for container in containers:
            text = container.get_text().lower()
            for indicator in strict:
                if indicator in text:
                    return True
            if container.find('button', class_=re.compile(r'buy|purchase|pay', re.I)):
                return True
        return False

    def _is_paid_page(self, url: str) -> bool:
        """Быстрая проверка URL на платную страницу через httpx"""
        try:
            path = url.replace('https://author.today', '')
            resp = self.client.get(path)
            return self._check_paid_page(BeautifulSoup(resp.text, 'lxml'))
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Утилиты
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_chapter_id(url: str) -> Optional[int]:
        """Извлекает chapter_id из /reader/559417/5296798"""
        m = re.search(r'/reader/\d+/(\d+)', url)
        return int(m.group(1)) if m else None

    @staticmethod
    def _clean_text(text: str) -> str:
        """Убирает лишние пустые строки"""
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        return '\n'.join(lines)
