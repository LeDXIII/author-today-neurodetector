"""
Модели данных для author-today-neurodetector
"""
from dataclasses import dataclass, field
from typing import Optional, List
from datetime import datetime


@dataclass
class Chapter:
    """Модель главы"""
    chapter_id: int
    title: str
    url: str
    text: str = ""
    is_paid: bool = False


@dataclass
class CheckResult:
    """Результат проверки"""
    book_id: int
    book_title: str = ""
    book_url: str = ""
    chapters_parsed: int = 0
    paid_chapter: Optional[int] = None
    total_chars: int = 0
    combined_text: str = ""
    
    # Результат NeuroDetector
    neural_percent: Optional[float] = None
    human_percent: Optional[float] = None
    raw_result: dict = field(default_factory=dict)
    
    # Метаданные
    check_date: Optional[datetime] = None
    error: Optional[str] = None


@dataclass 
class BookInfo:
    """Информация о книге"""
    book_id: int
    title: str
    url: str
    author: str = ""
    annotation: str = ""
    chapters_url: str = ""  # URL читалки
