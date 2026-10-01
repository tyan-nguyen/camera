import json
import base64
import logging
import urllib.request
import urllib.error
import cv2
import numpy as np
from typing import Dict, Tuple
from ocr_utils import correct_vietnamese_plate_ocr

logger = logging.getLogger(__name__)

def encode_image_to_base64(cv2_img: np.ndarray) -> str:
    """Chuyển đổi numpy image (BGR) sang chuỗi base64 JPEG"""
    if cv2_img is None or cv2_img.size == 0:
        return ""
    success, buffer = cv2.imencode('.jpg', cv2_img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not success:
        return ""
    return base64.b64encode(buffer).decode('utf-8')

def parse_llm_json_response(raw_text: str) -> Dict:
    """Trích xuất JSON từ phản hồi LLM Vision API"""
    if not raw_text:
        return {"plate_number": "UNKNOWN", "vehicle_view": "unknown", "confidence_score": 0.0}

    # Bỏ Markdown code blocks ```json ... ```
    cleaned = raw_text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
        plate = correct_vietnamese_plate_ocr(data.get("plate_number", "UNKNOWN"))
        view = str(data.get("vehicle_view", "unknown")).lower()
        if view not in ["front", "rear", "unknown"]:
            view = "unknown"
        conf = float(data.get("confidence_score", 0.9))
        return {
            "plate_number": plate if plate else "UNKNOWN",
            "vehicle_view": view,
            "confidence_score": conf
        }
    except Exception as e:
        logger.error(f"Failed to parse LLM JSON response: {e}. Raw text: {raw_text}")
        return {"plate_number": "UNKNOWN", "vehicle_view": "unknown", "confidence_score": 0.5}

def call_openai_vision_api(cv2_img: np.ndarray, api_key: str, model_name: str = "gpt-4o-mini") -> Dict:
    """Gửi ảnh phương tiện lên OpenAI Vision API"""
    if not api_key:
        logger.error("OpenAI API Key is empty!")
        return {"plate_number": "NO_API_KEY", "vehicle_view": "unknown", "confidence_score": 0.0}

    base64_str = encode_image_to_base64(cv2_img)
    if not base64_str:
        return {"plate_number": "IMG_ERR", "vehicle_view": "unknown", "confidence_score": 0.0}

    prompt = (
        "Bạn là hệ thống AI chuyên gia nhận dạng biển số xe giao thông Việt Nam. "
        "Hãy quan sát kỹ hình ảnh toàn bộ phương tiện (ô tô, xe máy, xe tải, xe buýt...), tìm vị trí gắn biển số xe trên xe, "
        "đọc chính xác toàn bộ ký tự biển số xe và xác định hướng nhìn phương tiện.\n"
        "Quy tắc biển số xe Việt Nam:\n"
        "- Đọc liền các ký tự chữ và số, bỏ dấu chấm và gạch ngang (ví dụ: '51F-123.45' -> '51F12345', '29A-8888' -> '29A8888', '72A1-02345' -> '72A102345').\n"
        "- Nếu không thấy biển số, trả về 'UNKNOWN'.\n"
        "- Trả về DUY NHẤT một chuỗi JSON thuần túy:\n"
        '{"plate_number": "51F12345", "vehicle_view": "front", "confidence_score": 0.95}\n'
        "Lưu ý: vehicle_view chỉ nhận 1 trong 3 giá trị: 'front', 'rear', 'unknown'."
    )

    payload = {
        "model": model_name or "gpt-4o-mini",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_str}"}}
                ]
            }
        ],
        "max_tokens": 100,
        "temperature": 0.1
    }

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            res_body = json.loads(response.read().decode("utf-8"))
            content = res_body["choices"][0]["message"]["content"]
            return parse_llm_json_response(content)
    except Exception as e:
        logger.error(f"Error calling OpenAI Vision API: {e}")
        return {"plate_number": "API_ERROR", "vehicle_view": "unknown", "confidence_score": 0.0}

def call_lmstudio_vision_api(cv2_img: np.ndarray, base_url: str = "http://localhost:1234/v1", model_name: str = "default", api_key: str = "") -> Dict:
    """Gửi ảnh phương tiện lên LM Studio Local Vision API (OpenAI compatible)"""
    base64_str = encode_image_to_base64(cv2_img)
    if not base64_str:
        return {"plate_number": "IMG_ERR", "vehicle_view": "unknown", "confidence_score": 0.0}

    prompt = (
        "Bạn là hệ thống AI chuyên gia nhận dạng biển số xe giao thông Việt Nam. "
        "Hãy quan sát kỹ hình ảnh toàn bộ phương tiện (ô tô, xe máy, xe tải, xe buýt...), tìm vị trí gắn biển số xe trên xe, "
        "đọc chính xác toàn bộ ký tự biển số xe và xác định hướng nhìn phương tiện.\n"
        "Quy tắc biển số xe Việt Nam:\n"
        "- Đọc liền các ký tự chữ và số, bỏ dấu chấm và gạch ngang (ví dụ: '51F-123.45' -> '51F12345', '29A-8888' -> '29A8888', '72A1-02345' -> '72A102345').\n"
        "- Nếu không thấy biển số, trả về 'UNKNOWN'.\n"
        "- Trả về DUY NHẤT một chuỗi JSON thuần túy:\n"
        '{"plate_number": "51F12345", "vehicle_view": "front", "confidence_score": 0.95}\n'
        "Lưu ý: vehicle_view chỉ nhận 1 trong 3 giá trị: 'front', 'rear', 'unknown'."
    )

    clean_base = (base_url or "http://localhost:1234/v1").strip().rstrip('/')
    if not clean_base.endswith('/chat/completions'):
        if clean_base.endswith('/v1'):
            endpoint = f"{clean_base}/chat/completions"
        else:
            endpoint = f"{clean_base}/v1/chat/completions"
    else:
        endpoint = clean_base

    payload = {
        "model": model_name if (model_name and model_name.strip() and model_name != "default") else "default",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_str}"}}
                ]
            }
        ],
        "max_tokens": 120,
        "temperature": 0.1
    }

    headers = {
        "Content-Type": "application/json"
    }
    if api_key and api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    else:
        headers["Authorization"] = "Bearer not-needed"

    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            res_body = json.loads(response.read().decode("utf-8"))
            content = res_body["choices"][0]["message"]["content"]
            return parse_llm_json_response(content)
    except urllib.error.URLError as e:
        logger.error(f"Cannot connect to LM Studio at [{endpoint}]: {e.reason}")
        return {"plate_number": "LM_CONN_ERR", "vehicle_view": "unknown", "confidence_score": 0.0}
    except Exception as e:
        logger.error(f"Error calling LM Studio Vision API: {e}")
        return {"plate_number": "API_ERROR", "vehicle_view": "unknown", "confidence_score": 0.0}

def call_gemini_vision_api(cv2_img: np.ndarray, api_key: str, model_name: str = "gemini-2.0-flash") -> Dict:
    """Gửi ảnh phương tiện lên Google Gemini Vision API"""
    if not api_key:
        logger.error("Gemini API Key is empty!")
        return {"plate_number": "NO_API_KEY", "vehicle_view": "unknown", "confidence_score": 0.0}

    base64_str = encode_image_to_base64(cv2_img)
    if not base64_str:
        return {"plate_number": "IMG_ERR", "vehicle_view": "unknown", "confidence_score": 0.0}

    prompt = (
        "Bạn là hệ thống AI chuyên gia nhận dạng biển số xe giao thông Việt Nam. "
        "Hãy quan sát kỹ hình ảnh toàn bộ phương tiện (ô tô, xe máy, xe tải, xe buýt...), tìm vị trí gắn biển số xe trên xe, "
        "đọc chính xác toàn bộ ký tự biển số xe và xác định hướng nhìn phương tiện.\n"
        "Quy tắc biển số xe Việt Nam:\n"
        "- Đọc liền các ký tự chữ và số, bỏ dấu chấm và gạch ngang (ví dụ: '51F-123.45' -> '51F12345', '29A-8888' -> '29A8888', '72A1-02345' -> '72A102345').\n"
        "- Nếu không thấy biển số, trả về 'UNKNOWN'.\n"
        "- Trả về DUY NHẤT một chuỗi JSON thuần túy:\n"
        '{"plate_number": "51F12345", "vehicle_view": "front", "confidence_score": 0.95}\n'
        "Lưu ý: vehicle_view chỉ nhận 1 trong 3 giá trị: 'front' (đầu xe), 'rear' (đuôi xe), 'unknown'."
    )

    model = model_name or "gemini-2.0-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": base64_str
                        }
                    }
                ]
            }
        ]
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            res_body = json.loads(response.read().decode("utf-8"))
            content = res_body["candidates"][0]["content"]["parts"][0]["text"]
            return parse_llm_json_response(content)
    except Exception as e:
        logger.error(f"Error calling Gemini Vision API: {e}")
        return {"plate_number": "API_ERROR", "vehicle_view": "unknown", "confidence_score": 0.0}

def test_vision_connection(engine_type: str, base_url: str = "", model_name: str = "", api_key: str = "") -> Dict:
    """Thử nghiệm kết nối tới Vision Engine"""
    # Tạo 1 ảnh giả lập nhỏ 100x100 màu đen
    dummy_img = np.zeros((100, 100, 3), dtype=np.uint8)
    cv2.putText(dummy_img, "TEST", (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
    
    if engine_type == "lmstudio_api":
        clean_base = (base_url or "http://localhost:1234/v1").strip().rstrip('/')
        if not clean_base.endswith('/chat/completions'):
            if clean_base.endswith('/v1'):
                endpoint = f"{clean_base}/chat/completions"
            else:
                endpoint = f"{clean_base}/v1/chat/completions"
        else:
            endpoint = clean_base

        headers = {"Content-Type": "application/json"}
        if api_key and api_key.strip():
            headers["Authorization"] = f"Bearer {api_key.strip()}"
        else:
            headers["Authorization"] = "Bearer not-needed"

        payload = {
            "model": model_name if (model_name and model_name.strip() and model_name != "default") else "default",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 5
        }
        try:
            req = urllib.request.Request(endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return {
                    "success": True,
                    "message": f"Kết nối LM Studio thành công tại {endpoint}! Phản hồi: {data.get('model', 'OK')}"
                }
        except urllib.error.URLError as ue:
            return {"success": False, "message": f"Không thể kết nối đến LM Studio ({ue.reason}). Hãy kiểm tra xem LM Studio đã bật Server chưa."}
        except Exception as e:
            return {"success": False, "message": f"Lỗi phản hồi từ LM Studio: {str(e)}"}

    elif engine_type == "openai_api":
        if not api_key:
            return {"success": False, "message": "Vui lòng nhập OpenAI API Key!"}
        try:
            res = call_openai_vision_api(dummy_img, api_key=api_key, model_name=model_name or "gpt-4o-mini")
            if res.get("plate_number") in ["NO_API_KEY", "API_ERROR"]:
                return {"success": False, "message": "Kết nối OpenAI thất bại. Kiểm tra API Key và Model."}
            return {"success": True, "message": "Kết nối OpenAI Vision API thành công!"}
        except Exception as e:
            return {"success": False, "message": f"Lỗi OpenAI: {str(e)}"}

    elif engine_type == "gemini_api":
        if not api_key:
            return {"success": False, "message": "Vui lòng nhập Google Gemini API Key!"}
        try:
            res = call_gemini_vision_api(dummy_img, api_key=api_key, model_name=model_name or "gemini-2.0-flash")
            if res.get("plate_number") in ["NO_API_KEY", "API_ERROR"]:
                return {"success": False, "message": "Kết nối Gemini thất bại. Kiểm tra API Key và Model."}
            return {"success": True, "message": "Kết nối Google Gemini API thành công!"}
        except Exception as e:
            return {"success": False, "message": f"Lỗi Gemini: {str(e)}"}

    return {"success": True, "message": "Mô hình YOLOv8 Local chạy sẵn trên GPU máy chủ."}
