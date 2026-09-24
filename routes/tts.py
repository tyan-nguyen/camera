import logging
import urllib.parse
import urllib.request
from typing import Dict
from fastapi import APIRouter, Query, Response

router = APIRouter(prefix="/api/v1/tts", tags=["Text to Speech"])
logger = logging.getLogger(__name__)

# Bộ nhớ đệm âm thanh RAM để phát tức thì không cần tải lại nhiều lần
_tts_cache: Dict[str, bytes] = {}

@router.get("")
def generate_vietnamese_tts(text: str = Query(..., description="Nội dung cần đọc tiếng Việt")):
    """
    Sinh file âm thanh giọng đọc Tiếng Việt chuẩn HD (Google TTS Neural Voice)
    Cực kỳ rõ ràng, truyền cảm, âm lượng chuẩn và không bị giật rè như giọng offline mặc định của máy tính.
    """
    clean_text = text.strip()
    if not clean_text:
        return Response(content=b"", media_type="audio/mpeg")

    # Kiểm tra cache
    if clean_text in _tts_cache:
        return Response(
            content=_tts_cache[clean_text],
            media_type="audio/mpeg",
            headers={"Cache-Control": "public, max-age=86400"}
        )

    try:
        encoded_text = urllib.parse.quote(clean_text)
        url = f"https://translate.google.com/translate_tts?ie=UTF-8&q={encoded_text}&tl=vi&client=tw-ob"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://translate.google.com/"
            }
        )

        with urllib.request.urlopen(req, timeout=5) as resp:
            audio_data = resp.read()
            if len(_tts_cache) > 200:
                _tts_cache.clear()
            _tts_cache[clean_text] = audio_data

            return Response(
                content=audio_data,
                media_type="audio/mpeg",
                headers={"Cache-Control": "public, max-age=86400"}
            )
    except Exception as e:
        logger.error(f"Error generating Google TTS for [{clean_text}]: {e}")
        return Response(status_code=500, content=f"TTS Error: {e}")
