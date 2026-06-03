"""
author-today-neurodetector — Проверка книг с author.today на нейросетевой текст
GUI: PyQt6 | Парсинг: Selenium + httpx | Детектор: Yandex NeuroDetector API
"""
import sys
import os
import logging
import re
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QTextEdit, QLineEdit, QPushButton, QProgressBar,
    QGroupBox, QFrame, QMessageBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont

os.environ['PYTHONIOENCODING'] = 'utf-8'

from src.reader import AuthorTodayReader
from src.detector import NeuroDetector


# ==============================================================================
# Поток проверки — выполняет парсинг и детекцию в фоне, не блокируя GUI
# ==============================================================================
class CheckWorker(QThread):
    progress = pyqtSignal(int, int)   # current, total
    status = pyqtSignal(str)
    log = pyqtSignal(str)
    result = pyqtSignal(dict)
    finished = pyqtSignal()

    def __init__(self, urls: list[str], max_chapters: int, delay: float):
        super().__init__()
        self.urls = urls
        self.max_chapters = max_chapters
        self.delay = delay

    def run(self):
        reader = AuthorTodayReader()
        detector = NeuroDetector()

        try:
            total = len(self.urls)

            for i, url in enumerate(self.urls, 1):
                self.progress.emit(i - 1, total)
                self.status.emit(f"Обработка книги {i}/{total}...")

                self.log.emit(f"\n{'='*50}")
                self.log.emit(f"[{i}/{total}] Обработка: {url}")
                self.log.emit(f"{'='*50}")

                # Извлекаем book_id из URL вида https://author.today/work/123456
                m = re.search(r'/work/(\d+)', url)
                if not m:
                    self.log.emit(f"  [ERROR] Неверный формат URL: {url}")
                    continue
                book_id = int(m.group(1))

                # Callback для детального логирования по главам
                def chapter_progress(num, total_ch, title, status_text):
                    self.log.emit(f"    Глава {num}/{total_ch}: {title} — {status_text}")

                # Шаг 1: Парсинг книги через Selenium
                self.log.emit(f"  [1/3] Парсинг книги {book_id}...")
                self.status.emit(f"Парсинг книги {book_id}...")

                text, chapters_info = reader.parse_book(
                    book_id,
                    max_chapters=self.max_chapters,
                    delay=self.delay,
                    progress_callback=chapter_progress
                )

                if not text:
                    self.log.emit(f"  [WARN] Текст не получен")
                    if chapters_info.get('error'):
                        self.log.emit(f"  [ERROR] {chapters_info['error']}")
                    continue

                self.log.emit(f"  [OK] Распарсено {chapters_info['parsed']} из {chapters_info['total_chapters_in_toc']} глав")
                self.log.emit(f"  [OK] Символов: {chapters_info['total_chars']}")
                if chapters_info.get('paid_chapter'):
                    self.log.emit(f"  [INFO] Остановлено на платной главе #{chapters_info['paid_chapter']}")

                # Шаг 2: Отправка текста в Yandex NeuroDetector
                self.log.emit(f"  [2/3] Отправка в Yandex NeuroDetector...")
                self.status.emit("Проверка через NeuroDetector...")
                detector_result = detector.check_text(text)

                self.log.emit(f"  [3/3] Результат получен")

                # Передаём результат в GUI
                self.result.emit({
                    'book_id': book_id,
                    'chapters_info': chapters_info,
                    'detector_result': detector_result
                })

            self.progress.emit(total, total)
            self.status.emit("Проверка завершена!")
            self.log.emit("\n[ГОТОВО] Все книги проверены")

        except Exception as e:
            self.log.emit(f"[ERROR] {e}")
            import traceback
            self.log.emit(traceback.format_exc())
        finally:
            reader.close_driver()
            self.finished.emit()


# ==============================================================================
# Главное окно приложения
# ==============================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Author Today — NeuroDetector")
        self.resize(1050, 850)
        self.setMinimumSize(850, 650)

        self.worker = None
        self.results_text_list = []

        # Центральный виджет
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(12)

        # --- Заголовок ---
        title = QLabel("🔍 Проверка книг на нейросетевой текст")
        title.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        # --- Предупреждение о бесплатных фрагментах ---
        warning = QLabel(
            "⚠️ Парсятся только бесплатные фрагменты. Если книга содержит платные главы, "
            "анализ будет по части текста. Результат на полном произведении может отличаться."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet(
            "background-color: #3d2e00; color: #ffe082; padding: 8px 12px; "
            "border-radius: 5px; border: 1px solid #665200; font-size: 11px;"
        )
        layout.addWidget(warning)

        # --- Группа ввода URL ---
        url_group = QGroupBox("URL книг")
        url_group.setStyleSheet("QGroupBox { color: #90caf9; font-weight: bold; }")
        url_layout = QVBoxLayout(url_group)

        url_hint = QLabel("Одна строка — одна книга (ссылка на страницу произведения /work/...)")
        url_hint.setStyleSheet("color: #888; font-size: 11px;")
        url_layout.addWidget(url_hint)

        self.url_input = QTextEdit()
        self.url_input.setPlaceholderText("https://author.today/work/")
        self.url_input.setMinimumHeight(100)
        self.url_input.setFont(QFont("Consolas", 10))
        url_layout.addWidget(self.url_input)

        layout.addWidget(url_group)

        # --- Настройки ---
        settings_layout = QHBoxLayout()

        lbl_max = QLabel("Макс. глав:")
        settings_layout.addWidget(lbl_max)
        self.max_chapters_input = QLineEdit("20")
        self.max_chapters_input.setFixedWidth(60)
        settings_layout.addWidget(self.max_chapters_input)

        lbl_delay = QLabel("Задержка (сек):")
        settings_layout.addWidget(lbl_delay)
        self.delay_input = QLineEdit("2")
        self.delay_input.setFixedWidth(60)
        settings_layout.addWidget(self.delay_input)

        settings_layout.addStretch()
        layout.addLayout(settings_layout)

        # --- Кнопка запуска ---
        self.check_button = QPushButton("🚀 Начать проверку")
        self.check_button.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        self.check_button.setFixedHeight(42)
        self.check_button.clicked.connect(self.start_check)
        layout.addWidget(self.check_button)

        # --- Прогресс-бар (увеличенная высота для текста) ---
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(24)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p%")
        layout.addWidget(self.progress_bar)

        self.status_label = QLabel("Готово к работе")
        self.status_label.setStyleSheet("color: #aaa; font-size: 11px;")
        layout.addWidget(self.status_label)

        # --- Разделитель ---
        line1 = self._separator()
        layout.addWidget(line1)

        # --- Результаты ---
        results_header = QHBoxLayout()
        results_title = QLabel("Результаты проверки:")
        results_title.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        results_header.addWidget(results_title)
        results_header.addStretch()

        self.copy_button = QPushButton(" Копировать")
        self.copy_button.setFixedWidth(120)
        self.copy_button.clicked.connect(self.copy_results)
        results_header.addWidget(self.copy_button)

        layout.addLayout(results_header)

        self.results_output = QTextEdit()
        self.results_output.setReadOnly(True)
        self.results_output.setFont(QFont("Consolas", 10))
        self.results_output.setMinimumHeight(200)
        layout.addWidget(self.results_output)

        # --- Разделитель ---
        line2 = self._separator()
        layout.addWidget(line2)

        # --- Лог ---
        log_title = QLabel("Лог:")
        log_title.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        layout.addWidget(log_title)

        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setFont(QFont("Consolas", 9))
        self.log_output.setMinimumHeight(120)
        layout.addWidget(self.log_output)

        # Настройка логирования в файл
        self._setup_logging()

    @staticmethod
    def _separator():
        """Горизонтальный разделитель"""
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    # ------------------------------------------------------------------
    # Логирование
    # ------------------------------------------------------------------
    def _setup_logging(self):
        """Создаёт лог-файл в подпапке с текущей датой"""
        log_dir = os.path.join(os.path.dirname(__file__), 'logs', datetime.now().strftime('%Y-%m-%d'))
        os.makedirs(log_dir, exist_ok=True)

        session_time = datetime.now().strftime('%H-%M-%S')
        log_file = os.path.join(log_dir, f'neurodetector_{session_time}.log')

        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s — %(levelname)s — %(message)s'
        ))

        logging.basicConfig(level=logging.INFO, handlers=[file_handler])
        self.logger = logging.getLogger(__name__)
        self._log_to_ui(f"Лог сессии: {log_file}")

    def _log_to_ui(self, message: str):
        """Вывод строки в лог-виджет и в файл"""
        self.log_output.append(message)
        self.log_output.verticalScrollBar().setValue(
            self.log_output.verticalScrollBar().maximum()
        )
        self.logger.info(message)

    # ------------------------------------------------------------------
    # Запуск проверки
    # ------------------------------------------------------------------
    def start_check(self):
        """Валидация входных данных и запуск потока"""
        if self.worker and self.worker.isRunning():
            return

        urls_text = self.url_input.toPlainText().strip()
        if not urls_text:
            QMessageBox.warning(self, "Ошибка", "Введите хотя бы один URL")
            return

        urls = [url.strip() for url in urls_text.split('\n') if url.strip()]

        try:
            max_chapters = int(self.max_chapters_input.text())
            delay = float(self.delay_input.text())
        except ValueError:
            QMessageBox.warning(self, "Ошибка", "Неверные значения в настройках")
            return

        # Сброс UI
        self.check_button.setEnabled(False)
        self.results_output.clear()
        self.results_text_list = []
        self.progress_bar.setValue(0)

        # Запуск потока
        self.worker = CheckWorker(urls, max_chapters, delay)
        self.worker.progress.connect(self._update_progress)
        self.worker.status.connect(self.status_label.setText)
        self.worker.log.connect(self._log_to_ui)
        self.worker.result.connect(self._add_result)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()

    def _update_progress(self, current: int, total: int):
        """Обновление прогресс-бара"""
        if total > 0:
            self.progress_bar.setValue(int(current / total * 100))

    def _add_result(self, data: dict):
        """Добавление результата проверки в поле вывода"""
        book_id = data['book_id']
        chapters_info = data['chapters_info']
        result = data['detector_result']

        output = f"\n{'='*60}\n"
        output += f"📖 Книга: {book_id}"
        if chapters_info.get('book_title'):
            output += f" — {chapters_info['book_title']}"
        output += f"\n"
        output += f"📑 Глав обработано: {chapters_info['parsed']}\n"
        if chapters_info.get('paid_chapter'):
            output += f"💰 Платная глава: #{chapters_info['paid_chapter']}\n"
        output += f"📝 Символов: {chapters_info['total_chars']}\n\n"

        if result.get('error'):
            output += f"❌ Ошибка: {result['error']}\n"
        else:
            ai_pct = result.get('ai_percent', 0)
            human_pct = result.get('human_percent', 0)
            verdict = result.get('verdict', 'N/A')
            segments = result.get('total_segments', 0)

            icon = "🔴" if ai_pct >= 50 else ("🟡" if ai_pct >= 5 else "✅")

            output += f"🤖 Результат NeuroDetector:\n"
            output += f"   {icon} {verdict}\n"
            output += f"   AI (нейросетевой): {ai_pct}% ({result.get('ai_count', 0)} сегм.)\n"
            output += f"   Human (человеческий): {human_pct}% ({result.get('human_count', 0)} сегм.)\n"
            output += f"   Всего сегментов: {segments}\n"

        output += f"{'='*60}\n"

        self.results_output.append(output)
        self.results_text_list.append(output)
        self.results_output.verticalScrollBar().setValue(
            self.results_output.verticalScrollBar().maximum()
        )

    def _on_finished(self):
        """Разблокировка кнопки после завершения"""
        self.check_button.setEnabled(True)

    def copy_results(self):
        """Копирование всех результатов в буфер обмена"""
        if not self.results_text_list:
            return
        QApplication.clipboard().setText('\n'.join(self.results_text_list))
        self._log_to_ui("[INFO] Результаты скопированы в буфер обмена")


# ==============================================================================
# Точка входа
# ==============================================================================
if __name__ == '__main__':
    if sys.platform == 'win32':
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

    app = QApplication(sys.argv)

    # Тёмная тема приложения
    app.setStyleSheet("""
        QMainWindow { background-color: #1e1e1e; }
        QGroupBox {
            font-weight: bold; border: 1px solid #444;
            border-radius: 5px; margin-top: 10px; padding-top: 10px;
        }
        QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
        QTextEdit {
            background-color: #2d2d2d; color: #d4d4d4;
            border: 1px solid #444; border-radius: 5px; padding: 5px;
        }
        QLineEdit {
            background-color: #2d2d2d; color: #d4d4d4;
            border: 1px solid #444; border-radius: 3px; padding: 3px;
        }
        QPushButton {
            background-color: #0078d4; color: white;
            border: none; border-radius: 5px; padding: 5px; font-weight: bold;
        }
        QPushButton:hover { background-color: #106ebe; }
        QPushButton:disabled { background-color: #555; color: #888; }
        QProgressBar {
            border: 1px solid #444; border-radius: 4px;
            background-color: #2d2d2d; text-align: center;
            font-weight: bold; color: #d4d4d4;
        }
        QProgressBar::chunk { background-color: #0078d4; border-radius: 3px; }
        QLabel { color: #d4d4d4; }
    """)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())
