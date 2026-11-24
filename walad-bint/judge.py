"""AI judging for the game, using Claude.

The judge receives every non-empty answer that already passed the local
first-letter check and returns a verdict (valid + short Arabic reason) for each.
If the API is unavailable the caller marks the answers as unjudged and the room
decides them by vote instead of getting stuck.
"""

import json
import logging
import os

import anthropic

log = logging.getLogger("judge")

MODEL = "claude-opus-5-5"

CATEGORY_RULES = {
    "boy": "اسم علم لذكر (اسم ولد) مستخدم فعلًا في أي بلد عربي أو غير عربي",
    "girl": "اسم علم لأنثى (اسم بنت) مستخدم فعلًا",
    "animal": "حيوان حقيقي (ثدييات، طيور، أسماك، حشرات، زواحف...)",
    "thing": "جماد: شيء مادي غير حي (أداة، جهاز، أثاث، مادة...). لا تقبل النباتات أو الأطعمة أو الأماكن",
    "food": "طعام أو شراب أو طبق أو فاكهة أو خضار",
    "country": "دولة أو بلد معروف، أو مدينة معروفة",
}

SYSTEM_PROMPT = """أنت حَكَم لعبة "ولد بنت حيوان جماد" العربية.
لكل إجابة: قرر هل هي صحيحة للتصنيف المطلوب وتبدأ بالحرف المطلوب.

القواعد:
- كن متسامحًا مع الأخطاء الإملائية البسيطة، واختلاف كتابة الهمزات (أ/إ/آ/ا)، والتاء المربوطة/الهاء، والألف المقصورة/الياء، ووجود "ال" التعريف في البداية.
- اقبل الكلمات العامية والأسماء الأجنبية المكتوبة بالعربية إذا كانت معروفة.
- ارفض الكلمات المخترعة، أو التي لا تنتمي للتصنيف، أو الحروف العشوائية، أو الجمل.
- إذا كانت الإجابة اسمًا لتصنيف آخر (مثلًا اسم حيوان في خانة الجماد) فارفضها.
- السبب: جملة عربية قصيرة جدًا (أقل من 8 كلمات).
أعد حكمًا لكل معرّف (id) مُرسل، بلا استثناء."""

SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "valid": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "valid", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(timeout=90.0, max_retries=2)
    return _client


async def judge(letter, items, category_meta):
    """items: list of {id, category, answer}. category_meta: key -> {label, rule}.

    Returns {id: (valid, reason)}. Raises on failure (caller falls back to voting).
    Ids missing from the model's reply are simply absent from the result.
    """
    if not items:
        return {}

    if os.environ.get("WBHJ_FAKE_JUDGE") == "1":
        # Local testing only: accept anything with 2+ letters.
        return {i["id"]: (len(i["answer"]) >= 2, "حكم تجريبي") for i in items}

    lines = []
    for i in items:
        meta = category_meta[i["category"]]
        lines.append({
            "id": i["id"],
            "category": meta["label"],
            "category_rule": meta["rule"],
            "answer": i["answer"],
        })

    user = (
        f"الحرف المطلوب: {letter}\n"
        "الإجابات (JSON):\n"
        + json.dumps(lines, ensure_ascii=False)
    )

    resp = await _get_client().beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": SCHEMA},
        },
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
    )

    if resp.stop_reason == "refusal":
        raise RuntimeError("judge refused")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("judge output truncated")

    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise RuntimeError("judge returned no text")
    data = json.loads(text)
    wanted = {i["id"] for i in items}
    out = {}
    for v in data.get("verdicts", []):
        if v.get("id") in wanted:
            out[v["id"]] = (bool(v["valid"]), str(v.get("reason", ""))[:80])
    return out
