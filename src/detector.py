"""
Yandex NeuroDetector API — отправка текста и получение результата анализа.
Endpoint: POST https://yandex.ru/lab/neurodetector/api/analyze/text
"""
import httpx
import logging
from typing import Dict

logger = logging.getLogger(__name__)


class NeuroDetector:
    """Клиент для Yandex NeuroDetector API"""

    def __init__(self):
        self.api_endpoint = "https://yandex.ru/lab/neurodetector/api/analyze/text"
        self.client = httpx.Client(
            headers={
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                ),
                'Content-Type': 'application/json',
            },
            follow_redirects=True,
            timeout=120.0,
        )

    def check_text(self, text: str) -> Dict:
        """
        Отправляет текст на анализ.

        Args:
            text: Текст для проверки (полный, без обрезки)

        Returns:
            Словарь с ai_percent, human_percent, verdict и др.
        """
        logger.info("Отправка в NeuroDetector (%d символов)", len(text))

        try:
            response = self.client.post(self.api_endpoint, json={'text': text})
            response.raise_for_status()

            data = response.json()
            if not data.get('ok'):
                return {'error': data.get('error', 'Unknown error'), 'raw_result': data}

            return self._parse_result(data.get('results', {}))

        except httpx.HTTPError as e:
            logger.error("HTTP ошибка: %s", e)
            return {'error': str(e)}
        except Exception as e:
            logger.error("Ошибка проверки: %s", e)
            return {'error': str(e)}

    @staticmethod
    def _parse_result(results: Dict) -> Dict:
        """Разбирает ответ API в удобный формат"""
        stats = results.get('stats', {})
        segments = results.get('segments', [])

        total = stats.get('segments_count', 0) or 0
        ai_count = (stats.get('AI_count', 0) or 0) + (stats.get('LIKELY_AI_count', 0) or 0)
        human_count = (stats.get('HUMAN_count', 0) or 0) + (stats.get('LIKELY_HUMAN_count', 0) or 0)

        ai_pct = round(ai_count / total * 100, 1) if total > 0 else 0
        human_pct = round(human_count / total * 100, 1) if total > 0 else 0

        if ai_pct >= 50:
            verdict = "Большая часть текста вероятно сгенерирована ИИ"
        elif ai_pct >= 5:
            verdict = "Часть текста вероятно сгенерирована ИИ"
        else:
            verdict = "Текст скорее всего написан человеком"

        return {
            'total_segments': total,
            'ai_count': ai_count,
            'human_count': human_count,
            'ai_percent': ai_pct,
            'human_percent': human_pct,
            'verdict': verdict,
            'segments': segments[:10],
            'raw_result': results,
        }
