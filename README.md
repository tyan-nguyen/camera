# ANPR Master Vision - Hệ Thống Giám Sát & Nhận Dạng Biển Số Xe (5 Luồng RTSP)

Hệ thống nhận dạng biển số xe tự động (ANPR / LPR) xử lý 5 luồng RTSP camera IP song song, tối ưu hóa suy luận trên GPU **NVIDIA RTX 3050 6GB** với **FastAPI**, **MySQL v8**, **TensorRT FP16**, **ByteTrack** và **Web UI Real-time Dashboard**.

---

## 🌟 Tính Năng Nổi Bật
1. **Quản lý Luồng RTSP Động**: Tự động ngắt/khởi tạo Thread camera ngay lập tức khi Thêm / Sửa / Bật / Tắt trên Web UI.
2. **Cơ chế Soft-Reconnect Watchdog**: Tự động thử lại mỗi 1 giây khi đứt cáp mạng camera mà không sập ứng dụng.
3. **Buffer-less Sampling & Latency Mitigation**: Lấy mẫu 5–8 FPS, luôn lấy frame mới nhất để triệt tiêu độ trễ tích lũy.
4. **ByteTrack & Best-Frame Selection**: Đánh giá độ nét bằng Laplacian Variance, chỉ kích hoạt OCR đúng 1 lần duy nhất trên frame tốt nhất của mỗi lượt xe.
5. **OCR Biển Số Việt Nam**: Perspective Transform nắn góc nghiêng, phân tách 1/2 dòng, tự động sửa lỗi nhầm lẫn OCR phổ biến (`8/B`, `0/D`, `1/I`).
6. **Async Disk Worker & Nén WebP**: Nén ảnh WebP (Quality 85%) và lưu bất đồng bộ theo cấu trúc thư mục ngày `YYYY/MM/DD/cam_{id}/`.
7. **Web UI Realtime**: Giao diện Dashboard xem live preview, thông báo xe phát hiện qua WebSocket.

---

## 🚀 Hướng Dẫn Khởi Chạy

### Cách 1: Chạy Trực Tiếp bằng Python
```bash
# 1. Cài đặt các thư viện cần thiết
pip install -r requirements.txt

# 2. Khởi chạy Uvicorn Server
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```
Truy cập Web UI tại: `http://localhost:8000`

### Cách 2: Triển Khai bằng Docker Compose
```bash
docker-compose up -d --build
```

---

## 📂 Cấu Trúc Thư Mục Lưu Trữ Ảnh
```text
storage/
└── captures/
    └── YYYY/MM/DD/cam_{id}/
        ├── {timestamp}_full_{plate}.webp   # Ảnh toàn cảnh
        └── {timestamp}_plate_{plate}.webp  # Ảnh crop biển số
```
