import re
import cv2
import numpy as np

def correct_vietnamese_plate_ocr(raw_text: str) -> str:
    """
    Chuẩn hóa chuỗi ký tự biển số xe Việt Nam theo quy chuẩn:
    Format xe máy/ô tô:
    - 2 chữ số đầu: Mã tỉnh/thành (VD: 29, 30, 51, 92, ...)
    - Ký tự thứ 3: Chữ cái series (A-Z) hoặc số + chữ cái (VD: 29A-12345, 51F-9999, 29-H1 123.45)
    - Nhóm số cuối: 4 hoặc 5 chữ số.
    Sửa lỗi đọc nhầm phổ biến:
      + Ở vị trí Cần là SỐ nhưng OCR ra CHỮ: 'B'->'8', 'D'->'0', 'I'->'1', 'O'->'0', 'Z'->'2', 'S'->'5', 'G'->'6'
      + Ở vị trí Cần là CHỮ nhưng OCR ra SỐ: '8'->'B', '0'->'D', '1'->'I'
    """
    if not raw_text:
        return ""

    raw_upper = raw_text.strip().upper()
    if any(k in raw_upper for k in ["UNKNOWN", "UNKN0WN", "API_ERROR", "NO_API_KEY", "IMG_ERR", "PENDING"]):
        return "UNKNOWN"
    
    # Loại bỏ ký tự đặc biệt, khoảng trắng, dấu gạch ngang
    cleaned = re.sub(r'[^A-Z0-9]', '', raw_upper)
    if len(cleaned) < 5:
        return cleaned

    # Chuyển đổi thành list để thay thế từng vị trí
    chars = list(cleaned)

    # 2 vị trí đầu tiên PHẢI là SỐ (mã tỉnh)
    digit_fix = {'B': '8', 'D': '0', 'O': '0', 'I': '1', 'Z': '2', 'S': '5', 'G': '6', 'Q': '0'}
    letter_fix = {'8': 'B', '0': 'D', '1': 'I', '5': 'S', '2': 'Z', '6': 'G'}

    for i in range(min(2, len(chars))):
        if chars[i] in digit_fix:
            chars[i] = digit_fix[chars[i]]

    # Ký tự thứ 3 thường là CHỮ (VD: 29A, 51F)
    if len(chars) > 2 and chars[2] in letter_fix:
        chars[2] = letter_fix[chars[2]]

    # Các ký tự còn lại từ vị trí thứ 4 trở đi (hoặc từ sau series) ưu tiên là SỐ
    start_num_idx = 3
    if len(chars) > 4 and chars[3].isalpha():  # VD: 29-H1 12345 (H1)
        if chars[3] in letter_fix and chars[3] != 'H':
            pass
        start_num_idx = 4

    for i in range(start_num_idx, len(chars)):
        if chars[i] in digit_fix:
            chars[i] = digit_fix[chars[i]]

    result = "".join(chars)
    return result

def perspective_transform_plate(image: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """
    Nắn thẳng hình ảnh biển số bị nghiêng góc (Perspective Transform)
    pts: 4 điểm góc [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]
    """
    if pts is None or len(pts) != 4:
        return image

    rect = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]      # Top-left
    rect[2] = pts[np.argmax(s)]      # Bottom-right

    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]   # Top-right
    rect[3] = pts[np.argmax(diff)]   # Bottom-left

    (tl, tr, br, bl) = rect

    # Tính chiều rộng biển mới
    width_A = np.sqrt(((br[0] - bl[0]) ** 2) + ((br[1] - bl[1]) ** 2))
    width_B = np.sqrt(((tr[0] - tl[0]) ** 2) + ((tr[1] - tl[1]) ** 2))
    maxWidth = max(int(width_A), int(width_B))

    # Tính chiều cao biển mới
    height_A = np.sqrt(((tr[0] - br[0]) ** 2) + ((tr[1] - br[1]) ** 2))
    height_B = np.sqrt(((tl[0] - bl[0]) ** 2) + ((tl[1] - bl[1]) ** 2))
    maxHeight = max(int(height_A), int(height_B))

    if maxWidth <= 0 or maxHeight <= 0:
        return image

    dst = np.array([
        [0, 0],
        [maxWidth - 1, 0],
        [maxWidth - 1, maxHeight - 1],
        [0, maxHeight - 1]
    ], dtype="float32")

    M = cv2.getPerspectiveTransform(rect, dst)
    warped = cv2.warpPerspective(image, M, (maxWidth, maxHeight))
    return warped

def is_two_line_plate(height: int, width: int) -> bool:
    """
    Phân biệt biển 1 dòng (dài, aspect ratio ~ 4:1 hoặc 5:1) và biển 2 dòng (vuông, aspect ratio ~ 1.3:1)
    """
    if width <= 0:
        return False
    aspect_ratio = height / float(width)
    return aspect_ratio > 0.45  # Chiều cao tương đối lớn so với chiều rộng -> Biển 2 dòng
