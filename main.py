"""
author-today-neurodetector — Проверка книг с author.today на нейросетевой текст
GUI: PyQt6 | Парсинг: Selenium + httpx | Детектор: Yandex NeuroDetector API

Окно рассчитано на массовые прогоны (сотни и тысячи ссылок): результаты лежат в
таблице (QTableView + модель, а не бесконечный текстовый вывод), лог ограничен по
объёму, обновления интерфейса пакуются таймером, а прожитый текст книг оседает в
SQLite-истории — оттуда же берётся повторная отправка без пере-парсинга.
"""
import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional

from PyQt6.QtCore import (
    QAbstractTableModel, QModelIndex, QSettings, QSortFilterProxyModel,
    Qt, QThread, QTimer, pyqtSignal,
)
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSpinBox,
    QSplitter, QTabWidget, QTableView, QVBoxLayout, QWidget,
)

from src.pipeline import BookTarget, Callbacks, Settings, parse_targets, run_batch
from src.storage import History, fmt_dt, render_report

os.environ['PYTHONIOENCODING'] = 'utf-8'

logger = logging.getLogger(__name__)

APP_ORG = 'AuthorTodayNeuroDetector'
MAX_LOG_BLOCKS = 3000

STATUS_LABELS = {
    'pending': '… в очереди',
    'ok': '✓ готово',
    'partial': '⚠ частично',
    'detect_failed': '✗ детектор',
    'no_text': '✗ нет текста',
    'parse_crash': '✗ сбой парсинга',
    'stopped': '⏸ остановлен',
    'skipped_cached': '⏭ уже была',
}

GREEN, ORANGE, RED, BLUE, DIM = '#4caf50', '#ffb74d', '#ef5350', '#64b5f6', '#9e9e9e'

COLUMNS = [
    ('#', 44),
    ('Книга', 330),
    ('Статус', 110),
    ('Глав', 78),
    ('Пропуск', 74),
    ('Замки', 66),
    ('Символов', 92),
    ('ИИ %', 68),
    ('Вердикт', 300),
    ('Время', 72),
    ('Ошибка', 260),
]


# ==============================================================================
# Модель таблицы результатов
# ==============================================================================
class BookTableModel(QAbstractTableModel):
    """Строка = одна книга. Данные — словари records из pipeline/History."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: List[Dict] = []

    # --- обязательные методы модели ---
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(COLUMNS)

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and 0 <= section < len(COLUMNS):
            return COLUMNS[section][0]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if row >= len(self._rows):
            return None
        rec = self._rows[row]
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            return self._cell(rec, col, row + 1)
        if role == Qt.ItemDataRole.ForegroundRole:
            color = self._color(rec, col)
            return QColor(color) if color else None
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in (0, 3, 4, 5, 6, 7, 9):
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(rec)
        return None

    # --- доступ извне ---
    def set_rows(self, records: List[Dict]) -> None:
        self.beginResetModel()
        self._rows = list(records)
        self.endResetModel()

    def upsert(self, rec: Dict) -> int:
        """Обновляет книгу по book_id или добавляет новую. Возвращает номер строки."""
        for i, existing in enumerate(self._rows):
            if existing.get('book_id') == rec.get('book_id'):
                self._rows[i] = rec
                top = self.index(i, 0)
                bottom = self.index(i, len(COLUMNS) - 1)
                self.dataChanged.emit(top, bottom)
                return i
        self.beginInsertRows(QModelIndex(), len(self._rows), len(self._rows))
        self._rows.append(rec)
        self.endInsertRows()
        return len(self._rows) - 1

    def record_at(self, source_row: int) -> Optional[Dict]:
        if 0 <= source_row < len(self._rows):
            return self._rows[source_row]
        return None

    def records(self) -> List[Dict]:
        return list(self._rows)

    def clear_rows(self) -> None:
        self.beginResetModel()
        self._rows = []
        self.endResetModel()

    def failed_ids(self) -> List[BookTarget]:
        return [BookTarget(book_id=r['book_id'], url=r.get('url', ''))
                for r in self._rows
                if r.get('status') not in ('ok', 'pending') and r.get('book_id')]

    def counts(self) -> Dict[str, int]:
        out = {'total': len(self._rows), 'ok': 0, 'failed': 0, 'high_ai': 0, 'chapters': 0}
        for r in self._rows:
            status = r.get('status')
            if status == 'ok':
                out['ok'] += 1
            elif status != 'pending':
                out['failed'] += 1
            if (r.get('ai_percent') or 0) >= 50:
                out['high_ai'] += 1
            out['chapters'] += int(r.get('chapters_parsed') or 0)
        return out

    # --- содержимое ячеек ---
    def _cell(self, rec: Dict, col: int, row_no: int):
        if col == 0:
            return row_no
        if col == 1:
            title = rec.get('title') or ''
            if not title:
                title = f"Книга #{rec.get('book_id')}"
            author = rec.get('author')
            return f"{title} — {author}" if author else title
        if col == 2:
            return STATUS_LABELS.get(rec.get('status', ''), rec.get('status', ''))
        if col == 3:
            parsed = int(rec.get('chapters_parsed') or 0)
            total = int(rec.get('chapters_total') or 0)
            return f"{parsed}/{total}" if total else str(parsed)
        if col == 4:
            return int(rec.get('chapters_failed') or 0) or ''
        if col == 5:
            locked = int(rec.get('chapters_locked') or 0)
            paid = rec.get('paid_chapter')
            if locked:
                return f"{locked}" + ('⛔' if paid else '')
            return ''
        if col == 6:
            chars = int(rec.get('chars') or 0)
            return f"{chars:,}".replace(',', ' ') if chars else ''
        if col == 7:
            ai = rec.get('ai_percent')
            return '' if ai is None else f"{ai:g}"
        if col == 8:
            return rec.get('verdict') or ''
        if col == 9:
            secs = rec.get('elapsed')
            if secs:
                return f"{float(secs):.0f}с"
            return fmt_dt(rec.get('created_at')).split(' ')[-1] if rec.get('created_at') else ''
        if col == 10:
            return (rec.get('error') or '')[:120]
        return None

    def _color(self, rec: Dict, col: int) -> str:
        status = rec.get('status')
        if col == 2:
            if status == 'ok':
                return GREEN
            if status == 'partial':
                return ORANGE
            if status == 'pending':
                return DIM
            return RED
        if col in (7, 8):
            ai = rec.get('ai_percent')
            if ai is None:
                return ''
            return RED if ai >= 50 else (ORANGE if ai >= 5 else GREEN)
        if col == 4 and int(rec.get('chapters_failed') or 0) > 0:
            return ORANGE
        if col == 5:
            return ORANGE
        if col == 10 and status != 'ok' and rec.get('error'):
            return RED
        return ''

    def _tooltip(self, rec: Dict) -> str:
        lines = [rec.get('url') or f"book {rec.get('book_id')}"]
        if rec.get('author'):
            lines.append(f"автор: {rec['author']}")
        if rec.get('segments'):
            lines.append(f"сегментов: {rec['segments']}")
        if rec.get('chunks_total'):
            lines.append(f"фрагментов API: {rec['chunks_total']}"
                         + (f", с ошибкой: {rec['chunks_failed']}" if rec.get('chunks_failed') else ""))
        if rec.get('paid_chapter'):
            lines.append(f"стоп на платной главе: {rec['paid_chapter']}")
        if rec.get('stopped_early'):
            lines.append("прогон остановлен на середине — вердикт по собранным главам")
        if rec.get('text_source') == 'cache':
            lines.append("текст взят из кэша (без пере-парсинга)")
        if rec.get('error'):
            lines.append(rec['error'])
        lines.append("двойной клик — подробности")
        return "\n".join(lines)


class BookFilterProxy(QSortFilterProxyModel):
    """Фильтр по статусу + поиск по названию/ID."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._status = 'all'
        self._search = ''

    def set_status(self, status: str) -> None:
        self._status = status or 'all'
        self.invalidateFilter()

    def set_search(self, text: str) -> None:
        self._search = (text or '').strip().lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        rec = model.record_at(source_row) if model else None
        if not rec:
            return False
        status = rec.get('status')
        if self._status == 'failed' and status == 'ok':
            return False
        if self._status == 'high' and (rec.get('ai_percent') or 0) < 50:
            return False
        if self._status == 'locked' and not (rec.get('chapters_locked') or rec.get('paid_chapter')):
            return False
        if self._search:
            haystack = f"{rec.get('title','')} {rec.get('author','')} {rec.get('book_id','')}".lower()
            if self._search not in haystack:
                return False
        return True


# ==============================================================================
# Поток проверки
# ==============================================================================
class CheckWorker(QThread):
    """Выполняет run_batch в фоне. Сигналы только складываются в буфер GUI-таймером."""

    log = pyqtSignal(str)
    book_started = pyqtSignal(int, int, int)
    chapter = pyqtSignal(int, int, int, str)
    detecting = pyqtSignal(int, int, str)
    book_done = pyqtSignal(dict)
    login_state = pyqtSignal(str, str)
    work_finished = pyqtSignal(int)   # сколько книг успели обработать

    def __init__(self, targets: List[BookTarget], settings: Settings,
                 history: History, reuse_cached: bool = False):
        super().__init__()
        self.targets = targets
        self.settings = settings
        self.history = history
        self.reuse_cached = reuse_cached
        self._stop = False
        self.processed = 0

    def request_stop(self) -> None:
        """Вежливый стоп: прерывание проверяется между главами и между книгами."""
        self._stop = True

    def should_stop(self) -> bool:
        return self._stop

    def run(self):
        cb = Callbacks(
            log=self.log.emit,
            book_started=lambda i, n, book: self.book_started.emit(i, n, int(book.get('book_id') or 0)),
            chapter=lambda book_id, num, total, info: self.chapter.emit(book_id, num, total,
                                                                        info.get('status', '')),
            detecting=self.detecting.emit,
            book_done=self._on_book_done,
            login=self.login_state.emit,
        )
        try:
            records = run_batch(self.targets, self.settings, cb, history=self.history,
                                should_stop=self.should_stop, reuse_cached_text=self.reuse_cached)
            self.processed = len(records)
        except Exception as e:
            logger.exception("Прогон упал")
            self.log.emit(f"[ERROR] сбой прогона: {type(e).__name__}: {e}")
        finally:
            self.work_finished.emit(self.processed)

    def _on_book_done(self, record: Dict) -> None:
        # Тяжёлые поля в GUI не передаём: таблицы хватает, а detail запросим из истории по клику.
        slim = {k: v for k, v in record.items() if k not in ('saved_text', 'detail')}
        self.book_done.emit(slim)


# ==============================================================================
# Диалог подробностей по книге
# ==============================================================================
class DetailDialog(QDialog):
    def __init__(self, rec: Dict, history: History, parent=None):
        super().__init__(parent)
        self.rec = rec
        book_id = rec.get('book_id')
        self.setWindowTitle(f"Книга {book_id} — подробности")
        self.resize(880, 620)

        layout = QVBoxLayout(self)
        head = QLabel(self._head_text(rec))
        head.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        head.setWordWrap(True)
        layout.addWidget(head)

        body = QPlainTextEdit()
        body.setReadOnly(True)
        body.setFont(QFont("Consolas", 9))
        body.setPlainText(self._detail_text(rec, history))
        layout.addWidget(body)

        row = QHBoxLayout()
        copy = QPushButton("Скопировать")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(body.toPlainText()))
        row.addWidget(copy)
        text_btn = QPushButton("Скопировать текст книги")
        text_btn.clicked.connect(lambda: self._copy_text(book_id, history))
        row.addWidget(text_btn)
        row.addStretch()
        close = QPushButton("Закрыть")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)

    @staticmethod
    def _head_text(rec: Dict) -> str:
        ai = rec.get('ai_percent')
        ai_text = '—' if ai is None else f"{ai:g}%"
        return (f"{rec.get('title') or 'Без названия'} · ИИ {ai_text} · "
                f"{STATUS_LABELS.get(rec.get('status',''), rec.get('status',''))}")

    @staticmethod
    def _detail_text(rec: Dict, history: History) -> str:
        parts = [
            f"URL: {rec.get('url') or ''}",
            f"ID книги: {rec.get('book_id')}",
            f"Автор: {rec.get('author') or '—'}",
            f"Проверка: {fmt_dt(rec.get('created_at'))} · занимает {rec.get('elapsed', '—')}с",
            f"Глав собрано: {rec.get('chapters_parsed')} (всего доступных {rec.get('chapters_total')},"
            f" пропущено {rec.get('chapters_failed')}, закрыто платным {rec.get('chapters_locked')})",
            f"Символов: {int(rec.get('chars') or 0):,}".replace(',', ' '),
            f"Фрагментов API: {rec.get('chunks_total') or '—'} (сбоев: {rec.get('chunks_failed') or 0})",
            f"Сегментов: {rec.get('segments') or '—'}",
            f"Вердикт: {rec.get('verdict') or '—'}",
            f"Ошибка: {rec.get('error') or 'нет'}",
            "",
            "— по главам —",
        ]
        detail = rec.get('detail')
        parsed = None
        if detail:
            try:
                parsed = json.loads(detail) if isinstance(detail, str) else detail
            except (ValueError, TypeError):
                parsed = None
        if parsed:
            for ch in parsed.get('chapter_results', []):
                err = f" — {ch.get('error')}" if ch.get('error') else ''
                parts.append(f"  {ch.get('num')}. {ch.get('title')} [{ch.get('status')}] "
                             f"{ch.get('chars')} симв. из {ch.get('source')}, "
                             f"попыток {ch.get('attempts')}, {ch.get('elapsed')}с{err}")
            chunks = parsed.get('chunks') or []
            if chunks:
                parts.append("")
                parts.append("— фрагменты детектора —")
                for c in chunks:
                    if c.get('ok'):
                        parts.append(f"  фрагмент {c.get('chunk')}: {c.get('chars')} симв. → "
                                     f"ИИ {c.get('ai')} / человек {c.get('human')}")
                    else:
                        parts.append(f"  фрагмент {c.get('chunk')}: ✗ {c.get('error')}")
        else:
            parts.append("  (поглавной отчёт не сохранён)")
        return "\n".join(parts)

    @staticmethod
    def _copy_text(book_id: int, history: History) -> None:
        text = history.text_for(book_id)
        if text:
            QApplication.clipboard().setText(text)


# ==============================================================================
# Диалог расширенных настроек
# ==============================================================================
class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки парсинга и детектора")
        self.settings = settings
        layout = QFormLayout(self)

        self.render_wait = QDoubleSpinBox()
        self.render_wait.setRange(2, 60)
        self.render_wait.setSuffix(' с')
        self.render_wait.setValue(settings.render_wait)

        self.page_timeout = QDoubleSpinBox()
        self.page_timeout.setRange(10, 180)
        self.page_timeout.setSuffix(' с')
        self.page_timeout.setValue(settings.page_load_timeout)

        self.chapter_attempts = QSpinBox()
        self.chapter_attempts.setRange(1, 8)
        self.chapter_attempts.setValue(settings.chapter_attempts)

        self.detector_attempts = QSpinBox()
        self.detector_attempts.setRange(1, 8)
        self.detector_attempts.setValue(settings.detector_attempts)

        self.chunk_chars = QSpinBox()
        self.chunk_chars.setRange(5000, 400000)
        self.chunk_chars.setSingleStep(5000)
        self.chunk_chars.setValue(settings.chunk_chars)

        self.headless = QCheckBox('без окна браузера (headless)')
        self.headless.setChecked(settings.headless)

        self.block_images = QCheckBox('не грузить картинки (быстрее)')
        self.block_images.setChecked(settings.block_images)

        self.save_text = QCheckBox('кэшировать текст книг для повтора без пере-парсинга')
        self.save_text.setChecked(settings.save_text)

        layout.addRow('Ждать JS-рендер:', self.render_wait)
        layout.addRow('Таймаут загрузки страницы:', self.page_timeout)
        layout.addRow('Попыток на главу:', self.chapter_attempts)
        layout.addRow('Попыток на фрагмент API:', self.detector_attempts)
        layout.addRow('Символов на запрос:', self.chunk_chars)
        layout.addRow('', self.headless)
        layout.addRow('', self.block_images)
        layout.addRow('', self.save_text)
        layout.addRow(QLabel('Меньше «символов на запрос» — надёжнее, но запросов больше.\n'
                             'Яндекс обрывает обработку примерно на 30 секундах.'))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def apply_to(self, target: Settings) -> Settings:
        target.render_wait = self.render_wait.value()
        target.page_load_timeout = self.page_timeout.value()
        target.chapter_attempts = self.chapter_attempts.value()
        target.detector_attempts = self.detector_attempts.value()
        target.chunk_chars = self.chunk_chars.value()
        target.headless = self.headless.isChecked()
        target.block_images = self.block_images.isChecked()
        target.save_text = self.save_text.isChecked()
        return target


# ==============================================================================
# Главное окно приложения
# ==============================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Author Today — NeuroDetector")
        self.resize(1220, 860)
        self.setMinimumSize(920, 640)

        self.history = History()
        self.worker: Optional[CheckWorker] = None
        self.settings = self._load_settings()
        self._closing = False
        self._run_started = 0.0

        self._pending_logs: List[str] = []
        self._log_timer = QTimer(self)
        self._log_timer.setInterval(150)
        self._log_timer.timeout.connect(self._flush_logs)
        self._log_timer.start()

        self._build_ui()
        self._setup_logging()
        self._apply_settings_to_ui()
        self._reload_history()

    # ------------------------------------------------------------------
    # Сборка интерфейса
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs)
        self.tabs.addTab(self._build_check_tab(), "Проверка")
        self.tabs.addTab(self._build_history_tab(), "История")

        # --- статусная строка ---
        self.login_label = QLabel("сессия: не проверялась")
        self.stats_label = QLabel("книг: 0")
        self.statusBar().addPermanentWidget(self.login_label)
        self.statusBar().addPermanentWidget(self.stats_label)
        self.statusBar().showMessage("Готово к работе")

    def _build_check_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(8)

        hint = QLabel("Ссылки на произведения — по одной в строке. Принимаются и "
                      "/work/123, и /reader/123/456, и просто ID.")
        hint.setStyleSheet(f"color: {DIM}; font-size: 11px;")
        layout.addWidget(hint)

        self.url_input = QPlainTextEdit()
        self.url_input.setPlaceholderText("https://author.today/work/618826")
        self.url_input.setFont(QFont("Consolas", 10))
        self.url_input.setFixedHeight(96)
        self.url_input.textChanged.connect(self._update_input_summary)
        layout.addWidget(self.url_input)

        # --- настройки в одну compact-строку ---
        form = QHBoxLayout()
        form.addWidget(QLabel("Макс. глав"))
        self.max_chapters = QSpinBox()
        self.max_chapters.setRange(1, 500)
        self.max_chapters.valueChanged.connect(self._update_input_summary)
        form.addWidget(self.max_chapters)

        form.addWidget(QLabel("Задержка, с"))
        self.delay = QDoubleSpinBox()
        self.delay.setRange(0, 30)
        self.delay.setSingleStep(0.5)
        form.addWidget(self.delay)

        self.login_check = QCheckBox("вход из .env")
        self.login_check.setToolTip("Если в .env есть AT_LOGIN/AT_PASSWORD — купленные "
                                    "или подписочные главы парсятся тоже.")
        form.addWidget(self.login_check)

        self.skip_check = QCheckBox("пропускать свежие проверки")
        self.skip_check.setToolTip("Не пере-парсить книгу, если она уже успешно проверена "
                                   "за последние 24 часа.")
        form.addWidget(self.skip_check)

        advanced = QPushButton("Ещё…")
        advanced.clicked.connect(self._open_settings)
        form.addWidget(advanced)
        form.addStretch()
        self.input_summary = QLabel("")
        self.input_summary.setStyleSheet(f"color: {DIM}; font-size: 11px;")
        form.addWidget(self.input_summary)
        layout.addLayout(form)

        # --- кнопки ---
        buttons = QHBoxLayout()
        self.start_button = QPushButton("🚀 Начать проверку")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self.start_check)
        buttons.addWidget(self.start_button)

        self.stop_button = QPushButton("⏹ Стоп")
        self.stop_button.setObjectName("danger")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.request_stop)
        buttons.addWidget(self.stop_button)

        self.retry_button = QPushButton("↻ Перепроверить упавшие")
        self.retry_button.clicked.connect(self.retry_failed)
        buttons.addWidget(self.retry_button)

        self.export_button = QPushButton("⤓ Экспорт")
        self.export_button.clicked.connect(self.export_results)
        buttons.addWidget(self.export_button)

        self.file_button = QPushButton("⤒ Список из файла")
        self.file_button.clicked.connect(self.import_urls)
        buttons.addWidget(self.file_button)

        self.copy_button = QPushButton("⧉ Копировать отчёт")
        self.copy_button.clicked.connect(self.copy_results)
        buttons.addWidget(self.copy_button)
        buttons.addStretch()
        layout.addLayout(buttons)

        # --- прогресс ---
        progress = QHBoxLayout()
        self.book_bar = QProgressBar()
        self.book_bar.setFixedHeight(20)
        self.book_bar.setTextVisible(True)
        self.book_bar.setFormat("книги %v/%m")
        progress.addWidget(self.book_bar, 3)

        self.chapter_bar = QProgressBar()
        self.chapter_bar.setFixedHeight(20)
        self.chapter_bar.setTextVisible(True)
        self.chapter_bar.setFormat("главы %v/%m")
        progress.addWidget(self.chapter_bar, 2)

        self.eta_label = QLabel("")
        self.eta_label.setMinimumWidth(150)
        self.eta_label.setStyleSheet(f"color: {DIM}; font-size: 11px;")
        progress.addWidget(self.eta_label)
        layout.addLayout(progress)

        # --- таблица + лог ---
        splitter = QSplitter()
        splitter.setOrientation(Qt.Orientation.Vertical)

        self.run_filter = QComboBox()
        self.run_filter.addItems(['все', 'только с ошибками', 'подозрение на ИИ', 'с платными главами'])
        self.run_filter.currentIndexChanged.connect(self._apply_run_filter)

        table_header = QHBoxLayout()
        table_header.addWidget(QLabel("Результаты:"))
        table_header.addWidget(self.run_filter)
        table_header.addStretch()
        wrapper = QWidget()
        wrapper_layout = QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.addLayout(table_header)

        self.run_model = BookTableModel()
        self.run_proxy = BookFilterProxy()
        self.run_proxy.setSourceModel(self.run_model)
        self.run_table = self._make_table(self.run_proxy)
        self.run_table.doubleClicked.connect(lambda idx: self._show_detail(idx, self.run_model))
        wrapper_layout.addWidget(self.run_table)
        splitter.addWidget(wrapper)

        log_box = QWidget()
        log_layout = QVBoxLayout(log_box)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.addWidget(QLabel("Лог:"))
        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setFont(QFont("Consolas", 9))
        self.log_output.setMaximumBlockCount(MAX_LOG_BLOCKS)
        log_layout.addWidget(self.log_output)
        splitter.addWidget(log_box)

        splitter.setSizes([520, 220])
        layout.addWidget(splitter, 4)
        return page

    def _build_history_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("поиск по названию, автору или ID")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._apply_history_filter)
        row.addWidget(self.search_input, 3)

        self.history_filter = QComboBox()
        self.history_filter.addItems(['все', 'только с ошибками', 'подозрение на ИИ',
                                      'с платными главами'])
        self.history_filter.currentIndexChanged.connect(self._apply_history_filter)
        row.addWidget(self.history_filter, 1)

        refresh = QPushButton("Обновить")
        refresh.clicked.connect(self._reload_history)
        row.addWidget(refresh)

        self.history_purge = QPushButton("Очистить кэш текста")
        self.history_purge.clicked.connect(self._purge_text)
        row.addWidget(self.history_purge)
        layout.addLayout(row)

        self.history_model = BookTableModel()
        self.history_proxy = BookFilterProxy()
        self.history_proxy.setSourceModel(self.history_model)
        self.history_table = self._make_table(self.history_proxy)
        self.history_table.doubleClicked.connect(lambda idx: self._show_detail(idx, self.history_model))
        layout.addWidget(self.history_table)

        self.history_info = QLabel("")
        self.history_info.setStyleSheet(f"color: {DIM}; font-size: 11px;")
        layout.addWidget(self.history_info)
        return page

    @staticmethod
    def _make_table(proxy) -> QTableView:
        table = QTableView()
        table.setModel(proxy)
        table.setSortingEnabled(True)
        table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        table.setWordWrap(False)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(22)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for i, (_, width) in enumerate(COLUMNS):
            table.setColumnWidth(i, width)
        header.setStretchLastSection(True)
        return table

    # ------------------------------------------------------------------
    # Настройки и логирование
    # ------------------------------------------------------------------
    def _load_settings(self) -> Settings:
        qs = QSettings(APP_ORG, 'NeuroDetector')
        data = {}
        for key in ('max_chapters', 'delay', 'use_login', 'block_images', 'headless',
                    'chapter_attempts', 'render_wait', 'page_load_timeout', 'chunk_chars',
                    'detector_attempts', 'skip_checked_hours', 'save_text'):
            value = qs.value(key, None)
            if value is not None:
                data[key] = value
        return Settings.from_dict(data)

    def _save_settings(self, s: Settings) -> None:
        qs = QSettings(APP_ORG, 'NeuroDetector')
        for key, value in s.__dict__.items():
            qs.setValue(key, int(value) if isinstance(value, bool) else value)

    def _apply_settings_to_ui(self) -> None:
        self.max_chapters.setValue(self.settings.max_chapters)
        self.delay.setValue(self.settings.delay)
        self.login_check.setChecked(self.settings.use_login)
        self.skip_check.setChecked(self.settings.skip_checked_hours > 0)

    def _collect_settings(self) -> Settings:
        s = self.settings
        s.max_chapters = self.max_chapters.value()
        s.delay = self.delay.value()
        s.use_login = self.login_check.isChecked()
        s.skip_checked_hours = 24.0 if self.skip_check.isChecked() else 0.0
        self.settings = s
        self._save_settings(s)
        return s

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self._collect_settings(), self)
        if dialog.exec():
            self.settings = dialog.apply_to(self.settings)
            self._save_settings(self.settings)
            self._log(f"[INFO] настройки обновлены: фрагмент {self.settings.chunk_chars} симв., "
                      f"попыток {self.settings.chapter_attempts}/{self.settings.detector_attempts}")

    def _setup_logging(self) -> None:
        """Файл лога в logs/<дата>/; если папка программы только для чтения — не роняем приложение."""
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
        stamp = datetime.now()
        name = f"neurodetector_{stamp.strftime('%H-%M-%S')}.log"
        try:
            log_dir = os.path.join(base, stamp.strftime('%Y-%m-%d'))
            os.makedirs(log_dir, exist_ok=True)
            self.log_file = os.path.join(log_dir, name)
            handler: logging.Handler = logging.FileHandler(self.log_file, encoding='utf-8')
        except OSError as e:
            fallback_dir = os.path.join(os.environ.get('TEMP', os.path.expanduser('~')),
                                        'author-today-neurodetector')
            os.makedirs(fallback_dir, exist_ok=True)
            self.log_file = os.path.join(fallback_dir, name)
            handler = logging.FileHandler(self.log_file, encoding='utf-8')
            logger.warning("Файл лога в папке программы недоступен (%s), пишу в %s", e, self.log_file)

        handler.setFormatter(logging.Formatter('%(asctime)s — %(levelname)s — %(message)s'))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
            root.addHandler(handler)
        self._log(f"Лог сессии: {self.log_file}")

    def _log(self, message: str) -> None:
        self._pending_logs.append(message)
        logger.info(message.replace('\n', ' | '))

    def _flush_logs(self) -> None:
        """Пачками раз в 150мс: на тысячах книг построчный append подвешивает UI."""
        if not self._pending_logs:
            return
        batch, self._pending_logs = self._pending_logs, []
        self.log_output.appendPlainText("\n".join(batch))

    # ------------------------------------------------------------------
    # Запуск / остановка
    # ------------------------------------------------------------------
    def _update_input_summary(self, *_) -> None:
        if self.worker and self.worker.isRunning():
            return
        targets, invalid = parse_targets(self.url_input.toPlainText())
        if not targets and not invalid:
            self.input_summary.setText("")
            return
        cap = self.max_chapters.value()
        worst = len(targets) * (cap * 6 + 20)
        text = f"книг: {len(targets)}"
        if invalid:
            text += f", нераспознано: {len(invalid)}"
        text += f", ≈{worst / 60:.0f} мин максимум"
        self.input_summary.setText(text)

    def start_check(self) -> None:
        if self.worker and self.worker.isRunning():
            return

        raw = self.url_input.toPlainText()
        targets, invalid = parse_targets(raw)
        if not targets:
            QMessageBox.warning(self, "Нет ссылок",
                                "Введите хотя бы одну ссылку вида https://author.today/work/123456")
            return
        if invalid:
            answer = QMessageBox.question(
                self, "Часть строк не похожа на книги",
                f"Не распознано строк: {len(invalid)}\nПервые: {', '.join(invalid[:3])}\n\n"
                f"Продолжить с остальными ({len(targets)})?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return

        settings = self._collect_settings()
        self.run_model.clear_rows()
        self._run_started = time.time()
        self._launch(targets, settings, reuse_cached=False)

    def retry_failed(self) -> None:
        """Повторяет только упавшие книги; если текст в кэше — сразу в детектор."""
        if self.worker and self.worker.isRunning():
            return
        targets = self.run_model.failed_ids()
        source = "текущего прогона"
        if not targets:
            rows = [r for r in self.history.rows(limit=2000) if r.get('status') != 'ok']
            targets = [BookTarget(book_id=r['book_id'], url=r.get('url', '')) for r in rows]
            source = "истории"
        if not targets:
            QMessageBox.information(self, "Нечего повторять", "Упавших книг нет.")
            return

        text = "\n".join(t.url for t in targets)
        self.url_input.setPlainText(text)
        reuse = any(self.history.text_for(t.book_id) for t in targets)
        self._log(f"\n[ПОВТОР] книг: {len(targets)} из {source}, "
                  f"текст {'из кэша' if reuse else 'пере-парсинг'}")
        self._launch(targets, self._collect_settings(), reuse_cached=reuse)

    def _launch(self, targets: List[BookTarget], settings: Settings, reuse_cached: bool) -> None:
        self._set_busy(True)
        self.book_bar.setMaximum(len(targets))
        self.book_bar.setValue(0)
        self.chapter_bar.setMaximum(1)
        self.chapter_bar.setValue(0)
        self.eta_label.setText("")
        self.statusBar().showMessage(f"Проверяю {len(targets)} книг...")

        self.worker = CheckWorker(targets, settings, self.history, reuse_cached=reuse_cached)
        self.worker.log.connect(self._log)
        self.worker.book_started.connect(self._on_book_started)
        self.worker.chapter.connect(self._on_chapter)
        self.worker.detecting.connect(self._on_detecting)
        self.worker.book_done.connect(self._on_book_done)
        self.worker.login_state.connect(self._on_login_state)
        self.worker.work_finished.connect(self._on_finished)
        self.worker.start()

    def request_stop(self) -> None:
        if not (self.worker and self.worker.isRunning()):
            return
        self.worker.request_stop()
        self.stop_button.setEnabled(False)
        self.statusBar().showMessage("Останавливаюсь: жду текущую главу...")
        self._log("[СТОП] запрошена остановка — прервусь на ближайшей главе/книге")

    def _set_busy(self, busy: bool) -> None:
        self.start_button.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        self.retry_button.setEnabled(not busy)
        self.file_button.setEnabled(not busy)
        self.url_input.setReadOnly(busy)
        for widget in (self.max_chapters, self.delay, self.login_check, self.skip_check):
            widget.setEnabled(not busy)

    # --- обработчики сигналов рабочего потока ---
    def _on_book_started(self, idx: int, total: int, book_id: int) -> None:
        self.book_bar.setMaximum(total)
        self.book_bar.setValue(idx - 1)
        self.statusBar().showMessage(f"[{idx}/{total}] книга {book_id}")

    def _on_chapter(self, book_id: int, num: int, total: int, status: str) -> None:
        self.chapter_bar.setMaximum(max(total, 1))
        self.chapter_bar.setValue(min(num, total))
        self.statusBar().showMessage(f"книга {book_id}: глава {num}/{total} — {status[:60]}")

    def _on_detecting(self, done: int, total: int, note: str) -> None:
        self.statusBar().showMessage(f"NeuroDetector: {note}")

    def _on_book_done(self, record: Dict) -> None:
        row = self.run_model.upsert(record)
        self.run_table.scrollTo(self.run_proxy.mapFromSource(self.run_model.index(row, 0)))
        self._update_counters()

    def _on_login_state(self, mode: str, note: str) -> None:
        labels = {'auth': 'авторизован', 'anon': 'анонимно', '2fa': '2FA — анонимно',
                  'error': 'ошибка входа'}
        color = GREEN if mode == 'auth' else (ORANGE if mode == 'anon' else RED)
        self.login_label.setText(f"сессия: {labels.get(mode, mode)}")
        self.login_label.setStyleSheet(f"color: {color};")
        if note and mode != 'auth':
            self.login_label.setToolTip(note)

    def _on_finished(self, processed: int) -> None:
        self.book_bar.setValue(self.book_bar.maximum())
        self._set_busy(False)
        self._update_counters()
        self.chapter_bar.setFormat("главы %v/%m")
        self.eta_label.setText("")
        if processed:
            avg = (time.time() - self._run_started) / processed
            self.statusBar().showMessage(f"Проверено книг: {processed} · в среднем {avg:.0f}с на книгу")
        else:
            self.statusBar().showMessage("Проверка не выполнена — смотри лог")
        self._reload_history()
        if self._closing:
            self.close()

    def _update_counters(self) -> None:
        c = self.run_model.counts()
        self.stats_label.setText(f"книг: {c['total']} · ок: {c['ok']} · ошибок: {c['failed']}"
                                 f" · ИИ≥50%: {c['high_ai']}")
        total = c['total']
        if total and self._run_started:
            done = c['ok'] + c['failed']
            if done:
                left = max(total - done, 0)
                sec_per = (time.time() - self._run_started) / done
                self.eta_label.setText(f"осталось ~{left * sec_per / 60:.1f} мин")

    # ------------------------------------------------------------------
    # История, экспорт, файлы
    # ------------------------------------------------------------------
    def _reload_history(self) -> None:
        rows = self.history.rows(limit=5000)
        self.history_model.set_rows(rows)
        size_kb = self.history.size_bytes() / 1024
        cached = sum(1 for r in rows if r.get('text_chars'))
        self.history_info.setText(f"записей: {self.history.count()} · с кэшем текста: {cached} · "
                                  f"размер БД: {size_kb:,.0f} КБ".replace(',', ' '))
        self._apply_history_filter()

    def _apply_history_filter(self) -> None:
        self.history_proxy.set_search(self.search_input.text())
        self.history_proxy.set_status(self._filter_value(self.history_filter.currentIndex()))

    def _apply_run_filter(self) -> None:
        self.run_proxy.set_status(self._filter_value(self.run_filter.currentIndex()))

    @staticmethod
    def _filter_value(index: int) -> str:
        return {0: 'all', 1: 'failed', 2: 'high', 3: 'locked'}.get(index, 'all')

    def _purge_text(self) -> None:
        answer = QMessageBox.question(
            self, "Очистить кэш текста",
            "Записи истории останутся, удалятся только кэшированные тексты книг "
            "(повторная отправка в детектор после этого потребует пере-парсинга).",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            n = self.history.purge_text()
            self._log(f"[INFO] кэш текста очищен: {n} записей")
            self.history.vacuum()
            self._reload_history()

    def export_results(self) -> None:
        rows = self.run_model.records() if self.tabs.currentIndex() == 0 else None
        path, selected = QFileDialog.getSaveFileName(
            self, "Экспорт результатов",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data',
                         f"neurodetector_{datetime.now().strftime('%Y-%m-%d_%H-%M')}"),
            "Текстовый отчёт (*.txt);;Таблица (*.csv);;JSON (*.json)")
        if not path:
            return
        fmt = 'json' if selected.startswith('JSON') else ('csv' if selected.startswith('Таблица') else 'txt')
        if not os.path.splitext(path)[1]:
            path += '.' + fmt
        try:
            self.history.export(path, fmt=fmt, rows=rows)
            self._log(f"[INFO] отчёт сохранён: {path}")
            self.statusBar().showMessage(f"Сохранено: {path}", 8000)
        except Exception as e:
            QMessageBox.critical(self, "Экспорт не удался", str(e))

    def import_urls(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Файл со ссылками", '',
                                              "Текст/списки (*.txt *.csv *.md);;Все файлы (*)")
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
        except OSError as e:
            QMessageBox.critical(self, "Не прочитать файл", str(e))
            return
        targets, invalid = parse_targets(content)
        existing, _ = parse_targets(self.url_input.toPlainText())
        merged = {t.book_id: t for t in existing}
        for t in targets:
            merged.setdefault(t.book_id, t)
        self.url_input.setPlainText("\n".join(t.url for t in merged.values()))
        self._log(f"[INFO] импортировано из файла: {len(targets)} книг "
                  f"(всего в списке {len(merged)}, мусора {len(invalid)})")

    def copy_results(self) -> None:
        rows = self.run_model.records() or self.history_model.records()
        if not rows:
            return
        QApplication.clipboard().setText(render_report(rows))
        self.statusBar().showMessage(f"Скопировано отчётов по {len(rows)} книгам", 5000)

    def _show_detail(self, index: QModelIndex, model: BookTableModel) -> None:
        source_row = self.run_proxy.mapToSource(index).row() \
            if model is self.run_model else self.history_proxy.mapToSource(index).row()
        rec = model.record_at(source_row)
        if not rec:
            return
        if not rec.get('detail'):
            latest = self.history.latest(rec.get('book_id'))
            if latest:
                rec = {**latest, **{k: v for k, v in rec.items() if v not in (None, '')}}
        DetailDialog(rec, self.history, self).exec()

    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:
        if self.worker and self.worker.isRunning():
            if not self._closing:
                self._closing = True
                self.request_stop()
                self.statusBar().showMessage("Останавливаюсь... закроюсь после текущей главы")
                event.ignore()
                return
            event.ignore()
            return
        self._flush_logs()
        self.history.close()
        event.accept()


# ==============================================================================
# Точка входа
# ==============================================================================
STYLE = """
QMainWindow, QWidget { background-color: #1e1e1e; color: #d4d4d4; }
QGroupBox { font-weight: bold; border: 1px solid #444; border-radius: 5px;
            margin-top: 10px; padding-top: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
QPlainTextEdit, QTextEdit, QLineEdit {
    background-color: #2d2d2d; color: #d4d4d4;
    border: 1px solid #444; border-radius: 5px; padding: 4px;
}
QPlainTextEdit[readOnly="true"] { background-color: #262626; }
QPushButton { background-color: #0078d4; color: white; border: none;
              border-radius: 5px; padding: 6px 10px; font-weight: bold; }
QPushButton:hover { background-color: #106ebe; }
QPushButton:disabled { background-color: #555; color: #888; }
QPushButton#danger { background-color: #a12626; }
QPushButton#danger:hover { background-color: #c62828; }
QPushButton#primary { background-color: #16803c; padding: 8px 14px; }
QPushButton#primary:hover { background-color: #1b9444; }
QProgressBar { border: 1px solid #444; border-radius: 4px; background-color: #2d2d2d;
               text-align: center; font-weight: bold; color: #d4d4d4; }
QProgressBar::chunk { background-color: #0078d4; border-radius: 3px; }
QTableView { background-color: #262626; alternate-background-color: #2a2a2a;
             gridline-color: #3a3a3a; border: 1px solid #444; border-radius: 4px; }
QTableView::item:selected { background-color: #094771; color: #ffffff; }
QHeaderView::section { background-color: #333; color: #cfcfcf; border: 1px solid #444;
                       padding: 4px; font-weight: bold; }
QTabBar::tab { background: #2d2d2d; color: #bbb; padding: 6px 14px;
               border: 1px solid #444; border-bottom: none; border-top-left-radius: 5px;
               border-top-right-radius: 5px; }
QTabBar::tab:selected { background: #1e1e1e; color: #ffffff; }
QSpinBox, QDoubleSpinBox { background-color: #2d2d2d; color: #d4d4d4;
                           border: 1px solid #444; border-radius: 3px; padding: 2px; }
QComboBox { background-color: #2d2d2d; color: #d4d4d4; border: 1px solid #444;
            border-radius: 3px; padding: 2px 6px; }
QLabel { color: #d4d4d4; }
QStatusBar { background-color: #262626; color: #c8c8c8; }
QSplitter::handle { background-color: #333; }
"""


def main() -> int:
    if sys.platform == 'win32':
        import io
        if hasattr(sys.stdout, 'buffer'):
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
