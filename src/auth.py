"""
Авторизация на author.today.

Логин портирован из соседнего проекта author-today-scout (httpx + CSRF + POST /account/login)
и проверен там же в бою. Нужен он затем, чтобы парсить не только бесплатные главы:
у аккаунта с покупками или подпиской закрытые главы открываются, и оглавление начинает
показывать их ссылками.

Учётные данные берутся из .env (AT_LOGIN / AT_PASSWORD). Файл в .gitignore — пароли
в репозиторий не попадают. Пароль не попадает и в логи.
"""
import logging
import os
import re
import time
from typing import Dict, Optional

import httpx
from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv обязателен только если нужен вход
    load_dotenv = None

logger = logging.getLogger(__name__)

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')


class TwoFactorRequiredError(Exception):
    """На аккаунте включена 2FA — кодом из письма этот инструмент не руководит."""


class LoginFailed(Exception):
    """Логин не прошёл: неверные данные, бан, недоступен сайт."""


class AuthorTodayAuth:
    """Клиент для входа на author.today через httpx."""

    def __init__(self, login: Optional[str] = None, password: Optional[str] = None,
                 base_url: str = 'https://author.today', env_file: Optional[str] = None):
        if load_dotenv:
            load_dotenv(env_file) if env_file else load_dotenv()

        self.base_url = os.getenv('AT_BASE_URL', base_url)
        self.user_login = login or os.getenv('AT_LOGIN') or ''
        self.password = password or os.getenv('AT_PASSWORD') or ''
        self.is_authenticated = False
        self.last_error = ''

        self.client = httpx.Client(
            base_url=self.base_url,
            headers={
                'User-Agent': USER_AGENT,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'ru-RU,ru;q=0.9',
            },
            follow_redirects=False,
            timeout=30.0,
        )

    @property
    def has_credentials(self) -> bool:
        return bool(self.user_login and self.password)

    # ------------------------------------------------------------------
    def get_cookies(self) -> Dict[str, str]:
        return {c.name: c.value for c in self.client.cookies.jar}

    def login(self, attempts: int = 3) -> bool:
        """Вход в аккаунт. True — сессия подтверждена закрытой страницей."""
        if not self.has_credentials:
            self.last_error = 'В .env нет AT_LOGIN / AT_PASSWORD'
            logger.info("Учётных данных нет — работаю анонимно")
            return False

        last_error = ''
        for attempt in range(1, attempts + 1):
            try:
                html = self._get_login_page()
                if html is None:
                    last_error = 'страница входа недоступна'
                elif self._submit_login(html):
                    return True
                else:
                    return False  # верные причины (неверный пароль/2FA) повторять смысла нет
            except TwoFactorRequiredError:
                raise
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                logger.warning("Попытка %d входа не удалась: %s", attempt, last_error)

            if attempt < attempts:
                time.sleep(3 * attempt)

        self.last_error = last_error or 'не удалось войти'
        logger.error("Вход не удался после %d попыток: %s", attempts, self.last_error)
        return False

    def ensure_login(self) -> bool:
        """Проверяет текущую сессию, при необходимости входит."""
        if self.is_authenticated and self.check_auth():
            return True
        try:
            return self.login()
        except TwoFactorRequiredError as e:
            self.last_error = str(e)
            logger.error("%s", e)
            return False

    def check_auth(self) -> bool:
        """Закрытая страница: редирект на /account/login значит «не авторизован»."""
        try:
            resp = self.client.get('/account/my-page')
            if resp.status_code in (301, 302, 303, 307):
                if '/login' in resp.headers.get('location', ''):
                    self.is_authenticated = False
                    return False
            if resp.status_code == 200:
                self.is_authenticated = True
                return True
            self.is_authenticated = False
            return False
        except Exception as e:
            logger.warning("Проверка сессии сорвалась: %s", e)
            self.is_authenticated = False
            return False

    def close(self) -> None:
        try:
            if not self.client.is_closed:
                self.client.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------
    def _get_login_page(self) -> Optional[str]:
        for attempt in range(3):
            try:
                resp = self.client.get('/account/login')
                if resp.status_code == 200:
                    return resp.text
                if resp.status_code in (502, 503, 504):
                    logger.warning("Страница входа: %s, повторяю", resp.status_code)
                    time.sleep(4 * (attempt + 1))
                    continue
                self.last_error = f"страница входа ответила {resp.status_code}"
                return None
            except Exception as e:
                logger.warning("Страница входа: %s", e)
                time.sleep(3 * (attempt + 1))
        return None

    def _submit_login(self, html: str) -> bool:
        token = self._extract_csrf(html)
        form_data = {
            'Login': self.user_login,
            'Password': self.password,
            'RememberMe': 'true',
            'SendEmailIfNeeded': 'false',
        }
        if token:
            form_data['__RequestVerificationToken'] = token

        resp = self.client.post(
            '/account/login',
            data=form_data,
            headers={'Referer': f'{self.base_url}/account/login', 'Origin': self.base_url},
        )
        logger.info("POST /account/login -> %s", resp.status_code)

        if resp.status_code in (301, 302, 303, 307):
            location = resp.headers.get('location', '')
            if '/login' in location:
                self.last_error = 'возврат на форму входа: проверь логин и пароль'
                logger.error("%s", self.last_error)
                return False
            return self._confirm_session()

        body = resp.text or ''
        if self._looks_like_2fa(body):
            raise TwoFactorRequiredError(
                'Аккаунт просит код подтверждения (2FA). Введи код вручную в браузере '
                'и отключи 2FA, либо работай без авторизации.')

        if _has_login_error(body):
            self.last_error = _has_login_error(body)
            logger.error("Отказ сайта при входе: %s", self.last_error)
            return False

        return self._confirm_session()

    def _confirm_session(self) -> bool:
        if self.check_auth():
            logger.info("Авторизация подтверждена, куки: %s", sorted(self.get_cookies()))
            return True
        self.last_error = 'после входа закрытая страница всё ещё требует логин'
        logger.error("%s", self.last_error)
        return False

    @staticmethod
    def _extract_csrf(html: str) -> Optional[str]:
        soup = BeautifulSoup(html, 'lxml')
        inp = soup.find('input', {'name': '__RequestVerificationToken'})
        value = inp.get('value') if inp else None
        return value or None

    @staticmethod
    def _looks_like_2fa(body: str) -> bool:
        low = body.lower()
        return ('двухфактор' in low or 'код подтвержд' in low or 'введите код' in low)


def _has_login_error(body: str) -> str:
    soup = BeautifulSoup(body, 'lxml')
    for el in soup.find_all(class_=re.compile(r'error|alert|danger|invalid', re.I)):
        text = el.get_text(' ', strip=True)
        if text and any(w in text.lower() for w in ('неверн', 'не найден', 'ошибк', 'заблокир')):
            return text[:200]
    return ""


def create_session(env_file: Optional[str] = None) -> Optional[AuthorTodayAuth]:
    """Вспомогательная функция: вошёл — вернула сессию, не вошёл — None."""
    auth = AuthorTodayAuth(env_file=env_file)
    try:
        return auth if auth.login() else None
    except TwoFactorRequiredError as e:
        logger.error("%s", e)
        auth.last_error = str(e)
        return None
    finally:
        if auth.is_authenticated is False:
            auth.close()
