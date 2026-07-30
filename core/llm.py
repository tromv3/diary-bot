import json
import logging

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM_PROMPT = (
    "Ты помощник для личного дневника. Пользователь присылает произвольный текст — "
    "мысли, планы, переживания за день. Выдели из текста короткие самостоятельные "
    "тезисы (главные мысли, планы, желания), каждый — одна лаконичная фраза на русском "
    "языке, без нумерации и лишних слов. Если в тексте нет ничего существенного (например, "
    "это просто приветствие), верни пустой список. Ответь строго в виде JSON-массива строк, "
    "без пояснений и без markdown-обрамления."
)


async def extract_theses(text: str) -> list[str]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
            json={
                "model": settings.openrouter_model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]

    try:
        theses = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("Model returned non-JSON theses, falling back to line split: %r", content)
        theses = [line.strip("-* \t") for line in content.splitlines()]

    return [t.strip() for t in theses if isinstance(t, str) and t.strip()]
