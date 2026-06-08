"""
Cross-bot trade lock — khóa LIÊN-TIẾN-TRÌNH dùng chung giữa arb-bot và trend-bot.

Bối cảnh: 2 bot chạy 2 process riêng (port 5000 / 5001) nhưng DÙNG CHUNG 1 tài
khoản OKX. Nếu cả hai cùng đọc số dư → tính sizing → đặt lệnh đồng thời, chúng
tranh margin & spot pool ⇒ lỗi 51008 / margin-không-đủ / vị thế phantom.

`threading.Lock` KHÔNG dùng được vì 2 process khác nhau. Module này dựng 1 file-lock
liên-tiến-trình:

  - Tạo file khóa atomic bằng `O_CREAT|O_EXCL` (chỉ 1 process tạo được).
  - Ghi PID + tên bot + timestamp để debug.
  - Tự PHÁ khóa cũ (stale) nếu process giữ khóa đã chết / crash giữa chừng
    (file cũ hơn `stale_after` giây) — chống deadlock vĩnh viễn.
  - `acquire()` có timeout; nếu hết timeout trả False (caller tự quyết bỏ lượt).

Cross-platform: chỉ dùng os.open/os.stat/os.unlink → chạy cả Windows (dev) lẫn
Linux (prod). KHÔNG cần fcntl/msvcrt.

Dùng:
    from cross_bot_lock import account_lock
    with account_lock('arb-open') as ok:
        if not ok:
            return   # không lấy được khóa → bỏ lượt này, thử lại sau
        ... đặt lệnh ...
"""
import os
import json
import time
import logging

log = logging.getLogger(__name__)

# Lock file đặt ở THƯ MỤC CHA (okx-bot/) để CẢ HAI bot trỏ tới CÙNG 1 file.
# Cho phép override bằng env (vd khi layout khác trên server).
_DEFAULT_LOCK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    '.okx_trade.lock',
)
LOCK_PATH = os.getenv('OKX_LOCK_PATH', _DEFAULT_LOCK)

# Tên bot (để biết process nào đang giữ khóa). Lấy từ BOT_NAME hoặc thư mục.
_BOT_NAME = os.getenv('BOT_NAME') or os.path.basename(os.path.dirname(os.path.abspath(__file__)))


class CrossBotLock:
    def __init__(self, label='', *, path=LOCK_PATH, stale_after=45.0,
                 timeout=30.0, poll=0.1):
        """
        label       : nhãn ngắn cho lượt khóa (debug log).
        stale_after : phá khóa nếu file cũ hơn ngần này giây (process giữ đã chết).
                      PHẢI > thời gian section dài nhất (open_position ~ vài giây).
        timeout     : tối đa chờ để giành khóa; hết → trả False.
        poll        : nhịp thử lại khi đang bị giữ.
        """
        self.label = label
        self.path = path
        self.stale_after = stale_after
        self.timeout = timeout
        self.poll = poll
        self._held = False

    def acquire(self):
        deadline = time.time() + self.timeout
        warned_stale = False
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, json.dumps({
                        'pid':   os.getpid(),
                        'bot':   _BOT_NAME,
                        'label': self.label,
                        'ts':    time.time(),
                    }).encode('utf-8'))
                finally:
                    os.close(fd)
                self._held = True
                return True
            except FileExistsError:
                # Đã có process khác giữ — kiểm tra có phải khóa "mồ côi" (stale) không.
                try:
                    age = time.time() - os.stat(self.path).st_mtime
                except FileNotFoundError:
                    continue  # vừa được nhả → thử giành lại ngay
                if age > self.stale_after:
                    if not warned_stale:
                        log.warning(f"[lock] phá khóa stale {self.path} (tuổi {age:.1f}s) — process giữ có thể đã chết")
                        warned_stale = True
                    try:
                        os.unlink(self.path)
                    except FileNotFoundError:
                        pass
                    continue
                if time.time() >= deadline:
                    log.warning(f"[lock] timeout giành khóa '{self.label}' sau {self.timeout}s — bỏ lượt")
                    return False
                time.sleep(self.poll)
            except Exception as e:
                # Lỗi bất ngờ (quyền ghi, đĩa đầy...) → KHÔNG khóa được; báo và cho qua
                # (an toàn hơn là treo bot — caller vẫn còn các guard khác).
                log.warning(f"[lock] lỗi khi giành khóa '{self.label}': {e}")
                return False

    def release(self):
        if not self._held:
            return
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning(f"[lock] lỗi khi nhả khóa '{self.label}': {e}")
        finally:
            self._held = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


def account_lock(label='', **kw):
    """Helper ngắn gọn: `with account_lock('arb-open') as ok:`"""
    return CrossBotLock(label, **kw)
