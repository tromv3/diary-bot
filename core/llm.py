import json
import logging

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

EXTRACT_SYSTEM_PROMPT = (
    "Ты помощник для личного дневника. Пользователь присылает произвольный текст — "
    "мысли, планы, переживания за день. Выдели из текста короткие самостоятельные "
    "тезисы (главные мысли, планы, желания), каждый — одна лаконичная фраза на русском "
    "языке, без нумерации и лишних слов. Если в тексте нет ничего существенного (например, "
    "это просто приветствие), верни пустой список. Ответь строго в виде JSON-массива строк, "
    "без пояснений и без markdown-обрамления."
)

SUMMARIZE_SYSTEM_PROMPT = (
    "Тебе передан список тезисов, извлечённых из отдельных сообщений одного пользователя "
    "за один день — они могут повторяться или пересекаться по смыслу, так как пользователь "
    "писал их в разное время. Объедини их в единый список уникальных тезисов дня: убери "
    "повторы и дубликаты, близкие по смыслу формулировки слей в одну, но не теряй уникальную "
    "информацию. Каждый тезис — одна лаконичная фраза на русском языке. Ответь строго в виде "
    "JSON-массива строк, без пояснений и без markdown-обрамления."
)


async def _chat_json_list(system_prompt: str, user_content: str) -> list[str]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
            json={
                "model": settings.openrouter_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]

    try:
        items = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("Model returned non-JSON list, falling back to line split: %r", content)
        items = [line.strip("-* \t") for line in content.splitlines()]

    return [t.strip() for t in items if isinstance(t, str) and t.strip()]


async def extract_theses(text: str) -> list[str]:
    return await _chat_json_list(EXTRACT_SYSTEM_PROMPT, text)


async def summarize_theses(theses: list[str]) -> list[str]:
    joined = "\n".join(f"- {t}" for t in theses)
    return await _chat_json_list(SUMMARIZE_SYSTEM_PROMPT, joined)
