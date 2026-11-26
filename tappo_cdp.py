# -*- coding: utf-8 -*-
"""Wuwa Tappo 注入式助手 · CDP 引擎（自动开箱 + 挂机刷点击）

用法:
  python tappo_cdp.py --status                 # 打印一次状态就退出
  python tappo_cdp.py --run                    # 自动开箱 + 刷点击(默认40次/分) + 按键气泡 + 面板设置
  python tappo_cdp.py --run --hud              # 额外在桌宠页面左上角挂只读状态牌（默认不挂，挡视野）
  python tappo_cdp.py --run --no-farm          # 只自动开箱
  python tappo_cdp.py --run --rate 20          # 刷点击降到 20 次/分钟
  python tappo_cdp.py --run --no-keybubble     # 不冒键帽气泡

界面（全部注入 Tappo 自身，不开新窗口）:
  - 桌宠头顶的按键气泡（键帽样式，默认开）
  - 桌宠页面左上角只读状态牌（余额/槽位/在线状态）—— 默认不挂，--hud 或面板开关才开
  - Tappo 功能面板的设置页里注入「宝箱助手」区块：开关、速率、立即开箱、停止引擎

配合 tappo_watch.py（监督员）: 桌宠进程出现时自动拉起本引擎。
前提: HKLM WebView2 策略已带 --remote-debugging-port=9222 且 Tappo 在跑。
"""
import asyncio, collections, ctypes, faulthandler, json, os, random, re, socket, subprocess, sys, threading, time, traceback

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HOST, PORT = "127.0.0.1", 9222
LOCK_PORT = 9223  # 单实例锁：占住即“已有引擎在跑”
WATCH_PORT = 9224  # 监督员锁端口（引擎反向保活用）
LOG_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "鸣潮宝箱助手CDP", "logs")
CONFIG_PATH = os.path.join(os.path.dirname(LOG_DIR), "config.json")
STOP_FLAG = os.path.join(os.path.dirname(LOG_DIR), "engine.stopped")  # 存在=用户主动停了，监督员别拉
WATCHER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tappo_watch.py")

def load_cfg(cfg):
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        for k in ("claim", "farm", "hud", "keybubble", "kbmouse"):
            if k in data:
                cfg[k] = bool(data[k])
        if data.get("rate"):
            cfg["rate"] = min(120.0, max(5.0, float(data["rate"])))
        if data.get("kbsize") in ("s", "m", "l"):
            cfg["kbsize"] = data["kbsize"]
    except Exception:
        pass
    return cfg

def save_cfg(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({k: cfg[k] for k in ("claim", "farm", "hud", "rate",
                                           "keybubble", "kbsize", "kbmouse")},
                      f, ensure_ascii=False)
    except Exception:
        pass

# ---------- 日志（pythonw 下无 stdout，文件是唯一线索） ----------
_log_fp = None

def _rotate():
    global _log_fp
    name = _log_fp.name
    _log_fp.close()
    for i in range(3, 1, -1):  # .2->.3, .1->.2
        src, dst = f"{name}.{i-1}", f"{name}.{i}"
        if os.path.exists(src):
            try: os.replace(src, dst)
            except OSError: pass
    if os.path.exists(name):  # 当前 -> .1
        try: os.replace(name, f"{name}.1")
        except OSError: pass
    _log_fp = open(name, "a", encoding="utf-8", buffering=1)

def log(msg):
    global _log_fp
    line = time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg
    try:
        if _log_fp is None:
            os.makedirs(LOG_DIR, exist_ok=True)
            _log_fp = open(os.path.join(LOG_DIR, time.strftime("cdp-%Y%m%d.log")),
                           "a", encoding="utf-8", buffering=1)
        if _log_fp.tell() > 2 * 1024 * 1024:
            _rotate()
        _log_fp.write(line + "\n")
    except Exception:
        pass
    if sys.stdout:
        print(time.strftime("[%H:%M:%S] ") + msg, flush=True)

# ---------- 崩溃留痕 ----------
# pythonw 没有 stderr：段错误 / 线程里漏出来的异常都是「无声蒸发」，
# 日志上只剩正常输出突然停住，根本查不出死因。这里把两样都落到 crash-*.log。
_CRASH_FP = None

def setup_crashlog():
    global _CRASH_FP
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        _CRASH_FP = open(os.path.join(LOG_DIR, time.strftime("crash-%Y%m%d.log")),
                         "a", encoding="utf-8", errors="replace", buffering=1)
        faulthandler.enable(file=_CRASH_FP, all_threads=True)
    except Exception:
        return False
    def _hook(a):
        try:
            _CRASH_FP.write("\n===== %s 线程未捕获异常 (%s) =====\n"
                            % (time.strftime("%Y-%m-%d %H:%M:%S"),
                               getattr(a.thread, "name", "?")))
            traceback.print_exception(a.exc_type, a.exc_value, a.exc_traceback, file=_CRASH_FP)
            _CRASH_FP.flush()
        except Exception:
            pass
        log("线程未捕获异常（已留痕 crash 日志）: " + repr(a.exc_value))
    try:
        threading.excepthook = _hook
    except Exception:
        pass
    return True

def acquire_lock():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(8)
    except OSError:
        return None
    # 必须把探测连接 accept 掉：listen 积压塞满后，监督员的 engine_running()
    # 会 connect 被拒 → 误判引擎死了 → 拉起注定撞锁的重复进程
    def _drain():
        while True:
            try:
                c, _ = s.accept()
                c.close()
            except OSError:
                return
    threading.Thread(target=_drain, daemon=True).start()
    return s

def alert_dup():
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, "引擎已经在跑了，别重复双击啦。", "鸣潮宝箱助手CDP", 0x10000)
    except Exception:
        pass

def _write_stop_flag():
    try:
        os.makedirs(os.path.dirname(STOP_FLAG), exist_ok=True)
        with open(STOP_FLAG, "w") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S\n"))
    except Exception:
        pass

def _clear_stop_flag():
    try:
        os.remove(STOP_FLAG)
    except OSError:
        pass

def pythonw():
    exe = sys.executable
    if os.path.basename(exe).lower() == "python.exe":
        cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(cand):
            return cand
    return exe

def watcher_alive():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", WATCH_PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()

def spawn_watcher():
    """反向保活：监督员死了，引擎把它拉回来（谁活着都能救对方）"""
    try:
        subprocess.Popen([pythonw(), WATCHER],
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        log("监督员不见了，已重新拉起（反向保活）")
    except Exception as e:
        log("拉起监督员失败: " + repr(e))

def _respawn():
    """崩溃自愈：拉起一个带着 --respawn 的新引擎替班，旧进程随后退场"""
    try:
        subprocess.Popen([pythonw(), os.path.abspath(__file__), "--run", "--respawn"],
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    except Exception as e:
        log("自我重启失败: " + repr(e))


# ---------- CDP HTTP ----------
def _http_get(path, timeout=4):
    s = socket.create_connection((HOST, PORT), timeout=timeout)
    s.settimeout(timeout)
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\nConnection: close\r\n\r\n".encode())
    data = b""
    try:
        while True:
            c = s.recv(65536)
            if not c:
                break
            data += c
    except socket.timeout:
        pass
    s.close()
    return data

def find_pages():
    raw = _http_get("/json/list")
    body = raw.partition(b"\r\n\r\n")[2]
    pages = [t for t in json.loads(body) if t.get("type") == "page"]
    def rank(t):
        u = t.get("url", "")
        if "tauri.localhost" in u and "index.html" not in u:
            return 0
        if "tauri.localhost" in u:
            return 1
        return 2
    pages.sort(key=rank)
    return pages

# ---------- 键盘采集（全局轮询 GetAsyncKeyState：不装钩子、不注入进程） ----------
_MOUSE_L, _MOUSE_R = 0x01, 0x02
_SHIFT_L, _SHIFT_R = 0xA0, 0xA1

def _vk_map():
    m = {}
    for i, ch in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        m[0x41 + i] = ch
    for i in range(10):
        m[0x30 + i] = str(i)
        m[0x60 + i] = "Num" + str(i)
    m.update({
        0x08: "BKSP", 0x09: "⇥", 0x0D: "RET", 0x1B: "Esc", 0x20: "SPACE", 0x14: "⇪",
        0x2E: "Del", 0x2D: "Ins", 0x24: "Home", 0x23: "End", 0x21: "PgUp", 0x22: "PgDn",
        0x25: "←", 0x26: "↑", 0x27: "→", 0x28: "↓",
        # 只认左右专用 VK：泛型 VK(0x10/0x11/0x12) 会跟专用键同时置位，留着会一次按键冒两个
        0xA0: "⇧", 0xA1: "⇧", 0xA2: "Ctrl", 0xA3: "Ctrl", 0xA4: "Alt", 0xA5: "Alt",
        0x5B: "⊞", 0x5C: "⊞", 0x5D: "⊞",
        0x91: "Scroll", 0x13: "Pause", 0x2C: "PrtSc",
        0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/", 0xC0: "`",
        0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
        0x6A: "Num*", 0x6B: "Num+", 0x6D: "Num-", 0x6E: "Num.", 0x6F: "Num/",
    })
    return m

def _shift_map():
    """键帽上印两个符号的键：按住 Shift 时该显示上档那个。
    字母不进来 —— 字母一律大写，不跟 Shift 变。"""
    return {
        0x30: ")", 0x31: "!", 0x32: "@", 0x33: "#", 0x34: "$",
        0x35: "%", 0x36: "^", 0x37: "&", 0x38: "*", 0x39: "(",
        0xBA: ":", 0xBB: "+", 0xBC: "<", 0xBD: "_", 0xBE: ">",
        0xBF: "?", 0xC0: "~", 0xDB: "{", 0xDC: "|", 0xDD: "}", 0xDE: '"',
    }

VK_LABEL = _vk_map()
SHIFT_LABEL = _shift_map()
POLL_VKS = tuple(sorted(set(VK_LABEL) | {_MOUSE_L, _MOUSE_R}))

_win_cache = []

def _find_tappo_window():
    """找 Wuwa Tappo 主窗口（class = Tauri Window）"""
    u = ctypes.windll.user32
    hits = []
    PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(h, lp):
        cls = ctypes.create_unicode_buffer(64)
        u.GetClassNameW(h, cls, 64)
        if cls.value == "Tauri Window":
            n = u.GetWindowTextLengthW(h)
            if n:
                b = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(h, b, n + 1)
                if "Tappo" in b.value:
                    hits.append(h)
                    return False
        return True

    try:
        u.EnumWindows(PROC(cb), 0)
    except Exception:
        pass
    return hits[0] if hits else None

def tappo_hwnd():
    """拿（并缓存）Tappo 主窗口句柄，窗口换了会自动重找"""
    u = ctypes.windll.user32
    h = _win_cache[0] if _win_cache else None
    if h and u.IsWindow(h):
        return h
    h = _find_tappo_window()
    if _win_cache:
        _win_cache[0] = h
    else:
        _win_cache.append(h)
    return h

def tappo_visible():
    """Tappo 隐藏 / 最小化时不采集也不冒"""
    u = ctypes.windll.user32
    h = tappo_hwnd()
    if not h:
        return False
    return bool(u.IsWindowVisible(h)) and not u.IsIconic(h)

def _rgn_funcs():
    """给 GDI 的区域函数补上显式类型。
    坑：ctypes 不声明时 HRGN 按 32 位 int 返回，64 位 Python 上句柄被截断，
    GetWindowRgn 拿到无效句柄永远返回 0 —— 于是"明明有区域"却判断成没有，
    RegionKeeper 一次都没清过（= 气泡只显示半截的真凶）。重复调用无害。"""
    u = ctypes.windll.user32
    g = ctypes.windll.gdi32
    g.CreateRectRgn.argtypes = [ctypes.c_int] * 4
    g.CreateRectRgn.restype = ctypes.c_void_p
    g.DeleteObject.argtypes = [ctypes.c_void_p]
    g.DeleteObject.restype = ctypes.c_int
    u.GetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    u.GetWindowRgn.restype = ctypes.c_int
    u.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
    u.SetWindowRgn.restype = ctypes.c_int
    return u, g

class RegionKeeper:
    """Tappo 自己用 SetWindowRgn 把主窗口裁成只剩猫那一小块，
    气泡飞到头顶就在区域外面，系统直接不画 —— 屏幕上什么都看不到（截页面截图却看得见，
    所以之前一直以为是"注入没生效"）。这里定期把区域清掉。
    窗口本身带 WS_EX_TRANSPARENT，铺满全屏也不会挡住任何鼠标点击。"""

    def __init__(self, interval=0.2):
        self.interval = interval
        self.cleared = 0
        self.probed = 0
        self.err = None
        self._stop = threading.Event()
        self._th = None

    def start(self):
        if self._th is not None:
            return
        self._stop.clear()
        self._th = threading.Thread(target=self._loop, name="wut-rgn", daemon=True)
        self._th.start()

    def stop(self):
        self._stop.set()
        self._th = None

    def _loop(self):
        u, g = _rgn_funcs()
        while not self._stop.is_set():
            try:
                h = tappo_hwnd()
                if h:
                    hr = g.CreateRectRgn(0, 0, 0, 0)
                    has = u.GetWindowRgn(h, hr)
                    g.DeleteObject(hr)
                    self.probed += 1
                    # 0=NULLREGION 没有区域；1/2/3 是有区域（3 也算，App 设的就是 3）
                    if has != 0:
                        u.SetWindowRgn(h, None, True)
                        self.cleared += 1
            except Exception as e:
                self.err = repr(e)
            time.sleep(self.interval)

class KeyWatcher:
    """全局键盘/鼠标轮询：只认 0->1 跳变，攒成一批交给页面。"""

    def __init__(self, enabled=True, mouse=True):
        self._q = collections.deque(maxlen=64)
        self._stop = threading.Event()
        self._th = None
        self.enabled = enabled
        self.mouse = mouse
        self._vis = True
        self.beats = 0     # 心跳：线程活着就在涨，看门狗/自愈靠它判断
        self.errs = 0
        self.err = None
        self.keys = 0      # 累计检测到的按键数（诊断用）
        self.pushed = 0    # 累计推给页面的批次数（诊断用）

    def alive(self):
        return self._th is not None and self._th.is_alive()

    def start(self):
        if self._th is not None:
            return
        self._stop.clear()
        self._th = threading.Thread(target=self._loop, name="wut-keys", daemon=True)
        self._th.start()

    def stop(self):
        self._stop.set()
        self._th = None
        self._q.clear()

    def set_enabled(self, on):
        self.enabled = bool(on)
        if not self.enabled:
            self._q.clear()

    def set_mouse(self, on):
        self.mouse = bool(on)

    def drain(self):
        out = []
        while self._q:
            out.append(self._q.popleft())
        return out

    def _loop(self):
        try:
            gaks = ctypes.windll.user32.GetAsyncKeyState
            gaks.argtypes = [ctypes.c_int]
            gaks.restype = ctypes.c_short
        except Exception:
            return
        prev = set()
        last = {}
        vis_at = 0.0
        while not self._stop.is_set():
            # 整个循环体必须兜住：这里任何一次异常都会让采集线程静默死亡，
            # 表现就是“引擎在线、日志正常、键盘却再也不冒气泡”（Tappo 重启后最容易踩到）。
            try:
                self.beats += 1
                now = time.monotonic()
                if now - vis_at >= 0.5:
                    vis_at = now
                    self._vis = tappo_visible()
                if not (self.enabled and self._vis):
                    prev.clear()  # 恢复采集时，别把“当时正按着”的键当成新按
                    self._q.clear()
                    time.sleep(0.12)
                    continue
                mouse_on = self.mouse
                cur = set()
                for vk in POLL_VKS:
                    if not mouse_on and (vk == _MOUSE_L or vk == _MOUSE_R):
                        continue
                    if gaks(vk) & 0x8000:
                        cur.add(vk)
                if cur != prev:
                    shift_on = (_SHIFT_L in cur) or (_SHIFT_R in cur)
                    for vk in cur - prev:
                        if vk == _MOUSE_L:
                            lb = "mouseL"
                        elif vk == _MOUSE_R:
                            lb = "mouseR"
                        elif shift_on and vk in SHIFT_LABEL:
                            lb = SHIFT_LABEL[vk]  # Shift+1 -> "!"，显示键帽上档那个
                        else:
                            lb = VK_LABEL.get(vk)
                        if not lb or now - last.get(lb, 0.0) < 0.04:
                            continue  # 40ms 内同键重复：手抖/系统重复，忽略
                        last[lb] = now
                        self._q.append(lb)
                        self.keys += 1
                    prev = cur
                time.sleep(0.008)
            except Exception as e:
                self.errs += 1
                self.err = repr(e)
                prev.clear()
                time.sleep(0.2)

# ---------- 注入 JS ----------
STORE = "document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('GlobalState')"

HUD_SETUP_JS = r"""
  if (!window.__wutHud) {
    window.__wutHud = function(o, extra) {
      let el = document.getElementById('wut-hud');
      if (!el) {
        el = document.createElement('div');
        el.id = 'wut-hud';
        el.style.cssText = 'position:fixed;left:14px;top:14px;z-index:2147483647;pointer-events:none;'
          + 'font:12px/1.7 "Segoe UI",system-ui,sans-serif;color:#d7ecff;background:rgba(8,18,32,.62);'
          + 'border:1px solid rgba(64,200,255,.4);border-radius:10px;padding:7px 12px;'
          + 'backdrop-filter:blur(4px);white-space:nowrap;text-shadow:0 1px 2px rgba(0,0,0,.5)';
        el.innerHTML = '<div><span id="wut-hud-dot" style="display:inline-block;width:8px;height:8px;'
          + 'border-radius:50%;background:#38e07b;box-shadow:0 0 6px #38e07b;margin-right:7px;vertical-align:middle"></span>'
          + '<span id="wut-hud-status" style="color:#9fe8b8;font-weight:600"></span></div>'
          + '<div id="wut-hud-bal" style="margin-top:1px"></div>'
          + '<div id="wut-hud-slots" style="margin-top:1px;color:#a8c6dd"></div>';
        (document.body || document.documentElement).appendChild(el);
      }
      el.dataset.ts = String(Date.now());
      const bal = document.getElementById('wut-hud-bal');
      if (bal) bal.textContent = '余额 ' + Number(o.balance).toLocaleString()
        + (extra && extra.farmed ? ' · 已刷 ' + extra.farmed : '');
      const sl = document.getElementById('wut-hud-slots');
      if (sl) {
        const parts = [];
        for (const side of ['left', 'right']) {
          const s = (o.slots || {})[side];
          if (!s) continue;
          if (s.mode === 'chest') parts.push((side === 'left' ? '左' : '右') + ' 可开箱');
          else if (s.mode === 'timer') parts.push((side === 'left' ? '左' : '右') + ' ' + s.text);
        }
        sl.textContent = parts.join(' · ');
      }
    };
    window.__wutWatch = setInterval(function () {
      const el = document.getElementById('wut-hud');
      if (!el) return;
      const age = Date.now() - parseFloat(el.dataset.ts || '0');
      const dot = document.getElementById('wut-hud-dot');
      const lab = document.getElementById('wut-hud-status');
      if (!dot || !lab) return;
      if (age > 8000) {
        dot.style.background = '#e05252'; dot.style.boxShadow = '0 0 6px #e05252';
        lab.textContent = '已断开 ' + Math.round(age / 1000) + 's'; lab.style.color = '#ffb3b3';
      } else {
        dot.style.background = '#38e07b'; dot.style.boxShadow = '0 0 6px #38e07b';
        lab.textContent = window.__wutRate === 0 ? '已暂停'
          : '引擎在线' + (window.__wutRate ? ' · ' + window.__wutRate + '/分' : '');
        lab.style.color = '#9fe8b8';
      }
    }, 2000);
  }
"""

def make_poll_js(farmed=0, rate=0, hud=None):
    base = r"""
(() => {
  const st = """ + STORE + r""";
  if (!st) return {ok: false, reason: 'no-store'};
  const out = {
    ok: true,
    local: st.localClickCount,
    serverTotal: st.serverClickTotal,
    consume: st.serverClickConsume,
    balance: st.totalClickCount,
    cumulative: st.cumulativeClickCount,
    slots: {}
  };
  for (const side of ['left', 'right']) {
    const a = document.querySelector('.reward-anchor--' + side);
    if (!a) { out.slots[side] = {mode: 'absent'}; continue; }
    const btn = a.querySelector('button');
    const cd = a.querySelector('.reward-countdown__text');
    if (btn) out.slots[side] = {mode: 'chest', disabled: !!btn.disabled};
    else if (cd && cd.textContent.trim()) out.slots[side] = {mode: 'timer', text: cd.textContent.trim()};
    else out.slots[side] = {mode: 'empty'};
  }
  out.hudPresent = !!document.getElementById('wut-hud');
"""
    if hud is True:
        extra = HUD_SETUP_JS + r"""
  if (window.__wutHud) { try { window.__wutHud(out, {farmed: %FARMED%}); window.__wutRate = %RATE%; } catch (e) {} }
"""
    elif hud is False:
        extra = r"""
  const _hud = document.getElementById('wut-hud');
  if (_hud) { try { _hud.remove(); } catch (e) {} }
"""
    else:
        extra = ""  # 只读探针（--status / connect 探测用），不动 HUD
    return (base + extra + r"""
  return out;
})()
""").replace("%FARMED%", str(int(farmed))).replace("%RATE%", str(int(rate)))

FARM_JS = r"""
(() => {
  const st = """ + STORE + r""";
  if (!st) return {ok: false, reason: 'no-store'};
  st.incrementLocalClickCount();
  return {ok: true, local: st.localClickCount};
})()
"""

def click_js(side):
    return r"""
(() => {
  const side = '%SIDE%';
  const a = document.querySelector('.reward-anchor--' + side);
  if (!a) return {ok: false, reason: 'no-anchor'};
  const btn = a.querySelector('button');
  if (!btn) return {ok: false, reason: 'no-button'};
  if (btn.disabled) return {ok: false, reason: 'disabled'};
  btn.click();
  return {ok: true};
})()
""".replace("%SIDE%", side)

KB_SETUP_JS = r"""
if (!window.__wutKeys) {
  var KB = window.__WUT_KB__ = window.__WUT_KB__ || {cfg: {on: true, size: 'm', mouse: true}, live: []};
  KB.live = KB.live || [];
  KB.MAX = 4;

  var ICON = {
    SPACE: '<svg viewBox="0 0 18 8" width="18" height="8" fill="none" style="width:1.16em;height:.515em">'
      + '<rect x="1.1" y="1.6" width="15.8" height="4.8" rx="2.4" stroke="currentColor" stroke-width="1.5"/></svg>',
    RET: '<svg viewBox="0 0 15 14" width="15" height="14" fill="none" style="width:.966em;height:.902em">'
      + '<path d="M12.4 1.6v7.2H4.1" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>'
      + '<path d="M6.9 5.3 3.4 8.8l3.5 3.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    BKSP: '<svg viewBox="0 0 16 13" width="16" height="13" fill="none" style="width:1.03em;height:.838em">'
      + '<path d="M5.2 1.2h9.6v8.2H5.2L1.2 6.6z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>'
      + '<path d="M7 4.3l3.1 3.2M10.1 4.3 7 7.5" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>',
    mouseL: '<svg viewBox="0 0 13 19" width="13" height="19" fill="none" style="width:.838em;height:1.225em">'
      + '<path d="M6.5 1.3C3.5 1.3 1.3 3.7 1.3 7.6v3.9c0 3.6 2.2 6.2 5.2 6.2s5.2-2.6 5.2-6.2V7.6c0-3.9-2.2-6.3-5.2-6.3z" stroke="currentColor" stroke-width="1.35" stroke-linejoin="round"/>'
      + '<path d="M6.4 1.4c-2.9 0-5 2.4-5 6.1h5z" fill="currentColor" fill-opacity=".55"/>'
      + '<path d="M1.4 7.6h10.2" stroke="currentColor" stroke-width="1.1"/></svg>',
    mouseR: '<svg viewBox="0 0 13 19" width="13" height="19" fill="none" style="width:.838em;height:1.225em">'
      + '<path d="M6.5 1.3C3.5 1.3 1.3 3.7 1.3 7.6v3.9c0 3.6 2.2 6.2 5.2 6.2s5.2-2.6 5.2-6.2V7.6c0-3.9-2.2-6.3-5.2-6.3z" stroke="currentColor" stroke-width="1.35" stroke-linejoin="round"/>'
      + '<path d="M6.6 1.4c2.9 0 5 2.4 5 6.1h-5z" fill="currentColor" fill-opacity=".55"/>'
      + '<path d="M1.4 7.6h10.2" stroke="currentColor" stroke-width="1.1"/></svg>'
  };

  KB.cssText = function () {
    var f = KB.cfg.size === 's' ? 16.6 : (KB.cfg.size === 'l' ? 22.9 : 19.4);
    KB.step = Math.round(f * 2.95);
    return ''
      + '#wut-keybubbles{position:absolute;left:50%;bottom:100%;width:0;height:0;margin-bottom:6px;'
      + 'z-index:20;pointer-events:none;font-size:' + f + 'px}'
      + '#wut-keybubbles .wut-slot{position:absolute;left:0;bottom:0;'
      + 'transition:transform .24s cubic-bezier(.2,.75,.3,1)}'
      + '#wut-keybubbles .wut-kb{display:inline-flex;align-items:center;justify-content:center;'
      + 'min-width:2.32em;height:2.32em;padding:0 .58em;border-radius:.644em;'
      + 'font:700 1em/1 "Microsoft YaHei","Segoe UI",sans-serif;color:#3c3a34;letter-spacing:.019em;'
      + 'white-space:nowrap;transform-origin:center bottom;'
      + 'background:linear-gradient(180deg,#FFFEFA 0%,#F9F3E6 46%,#EDE3CE 100%);'
      + 'border:.064em solid rgba(120,106,80,.34);'
      + 'box-shadow:inset 0 .064em 0 #fff,inset 0 -.322em 0 #DCD2BC,'
      + 'inset 0 -.386em .064em rgba(255,255,255,.55),'
      + '0 .451em 0 -.064em #BEB29A,0 .708em .966em rgba(0,0,0,.34);'
      + 'animation:wutKbRise var(--dur,1.05s) cubic-bezier(.22,.68,.24,1) forwards}'
      + '#wut-keybubbles svg{display:block}'
      + '@keyframes wutKbRise{'
      + '0%{opacity:0;transform:translate3d(0,5px,0) scale(.86) rotate(0deg)}'
      + '13%{opacity:1;transform:translate3d(0,0,0) scale(1) rotate(0deg)}'
      + '52%{opacity:.98;transform:translate3d(calc(var(--dx,0px)*.5),-14px,0) scale(1.015) rotate(var(--rot,.6deg))}'
      + '100%{opacity:0;transform:translate3d(var(--dx,0px),-31px,0) scale(1.03) rotate(calc(var(--rot,.6deg)*2))}}';
  };

  KB.ensure = function () {
    var box = document.querySelector('.cat-mainbox');
    if (!box) return null;
    var st = document.getElementById('wut-kb-style');
    if (!st) {
      st = document.createElement('style'); st.id = 'wut-kb-style';
      (document.head || document.documentElement).appendChild(st);
      st.dataset.tier = '';
    }
    if (st.dataset.tier !== KB.cfg.size) {
      st.textContent = KB.cssText();
      st.dataset.tier = KB.cfg.size;
    }
    var c = document.getElementById('wut-keybubbles');
    if (!c || c.parentNode !== box) {
      if (c && c.parentNode) c.parentNode.removeChild(c);
      c = document.createElement('div'); c.id = 'wut-keybubbles';
      box.appendChild(c);
      KB.live.length = 0;   // Vue 重渲染把旧气泡一起带走了
    }
    return c;
  };

  KB.restack = function () {
    for (var i = 0; i < KB.live.length; i++) {
      var o = KB.live[i];
      o.slot.style.transform = 'translate(-50%,' + (-(i * KB.step)) + 'px)';
      o.slot.style.zIndex = String(40 - i);
    }
  };

  KB.drop = function (o) {
    var i = KB.live.indexOf(o);
    if (i < 0) return;
    KB.live.splice(i, 1);
    clearTimeout(o.t);
    if (o.slot.parentNode) o.slot.parentNode.removeChild(o.slot);
  };

  KB.add = function (label) {
    var c = KB.ensure();
    if (!c) return false;
    var name = String(label);
    var slot = document.createElement('div');
    slot.className = 'wut-slot';
    slot.style.transform = 'translate(-50%,0)';
    var kb = document.createElement('div');
    kb.className = 'wut-kb';
    if (ICON[name]) kb.innerHTML = ICON[name];
    else kb.textContent = name;
    kb.style.marginLeft = (Math.random() * 6 - 3).toFixed(1) + 'px';  // 别叠成一根直线
    // 每个键自己的节奏：轻一点、飘一点，别像机械地往上顶
    var dur = 0.96 + Math.random() * 0.34;
    kb.style.setProperty('--dur', dur.toFixed(2) + 's');
    kb.style.setProperty('--dx', (Math.random() * 10 - 5).toFixed(1) + 'px');
    kb.style.setProperty('--rot', (Math.random() * 3.2 - 1.6).toFixed(1) + 'deg');
    slot.appendChild(kb);
    c.appendChild(slot);
    var o = {slot: slot, kb: kb, t: 0};
    KB.live.unshift(o);
    while (KB.live.length > KB.MAX) KB.drop(KB.live[KB.live.length - 1]);
    KB.restack();
    o.t = setTimeout(function () { KB.drop(o); }, dur * 1000 + 90);
    return true;
  };

  window.__wutKeys = function (list) {
    var n = 0;
    if (!KB.cfg.on) return 0;
    for (var i = 0; i < (list || []).length; i++) {
      if (list[i] && KB.add(list[i])) n++;
    }
    return n;
  };

  window.__wutKbCfg = function (c) {
    if (c) {
      if (typeof c.on === 'boolean') KB.cfg.on = c.on;
      if (c.size === 's' || c.size === 'm' || c.size === 'l') KB.cfg.size = c.size;
      if (typeof c.mouse === 'boolean') KB.cfg.mouse = c.mouse;
    }
    var box = document.getElementById('wut-keybubbles');
    if (box) KB.ensure();
    if (!KB.cfg.on) {
      for (var i = KB.live.length - 1; i >= 0; i--) KB.drop(KB.live[i]);
    }
    return {ok: true, cfg: KB.cfg};
  };

  window.__wutKbDemo = function () {
    var seq = [['Ctrl', 0], ['⇧', 110], ['S', 220], ['mouseL', 330]];
    for (var i = 0; i < seq.length; i++) {
      (function (nm, ms) {
        setTimeout(function () { KB.add(nm); }, ms);
      })(seq[i][0], seq[i][1]);
    }
    return {ok: true};
  };
}
"""

def make_keys_js(names):
    return ("(function(){try{"
            "if(!window.__wutKeys)return{ok:false,reason:'no-setup'};"
            "return{ok:true,n:window.__wutKeys(" + json.dumps(names, ensure_ascii=False) + ")};"
            "}catch(e){return{ok:false,reason:String(e)}}})()")

KB_ALIVE_JS = ("(function(){try{return{fn:typeof window.__wutKeys,"
               "box:!!document.querySelector('.cat-mainbox'),"
               "hit:!!document.getElementById('wut-keybubbles')};"
               "}catch(e){return{err:String(e)}}})()")

def make_kbcfg_js(cfg):
    return ("(function(){try{"
            "if(!window.__wutKbCfg)return{ok:false,reason:'no-setup'};"
            "return window.__wutKbCfg(" + json.dumps(
                {"on": bool(cfg.get("keybubble", True)),
                 "size": cfg.get("kbsize", "m"),
                 "mouse": bool(cfg.get("kbmouse", True))}, ensure_ascii=False) + ");"
            "}catch(e){return{ok:false,reason:String(e)}}})()")

KB_DEMO_JS = "(function(){try{return window.__wutKbDemo?window.__wutKbDemo():{ok:false,reason:'no-setup'}}catch(e){return{ok:false,reason:String(e)}}})()"

PANEL_SYNC_JS = r"""
(() => {
  try {
    let W = window.__WUT_PANEL__;
    // 版本号对不上 = 页面里留着旧引擎的闭包（build/render 都是老代码），整个换掉
    if (W && W.ver !== 'kb2') W = null;
    if (!W) {
      W = window.__WUT_PANEL__ = {ver: 'kb2', cmds: [], beat: 0, busy: false};
      W.push = function (c) { W.cmds.push(c); };
      W.build = function () {
        const css = `
          #wut-sec{margin:14px 0 4px;padding-top:12px;border-top:1px solid rgba(142,94,67,.28)}
          #wut-sec .wut-h{font-size:15px;font-weight:600;color:#8e5e43;letter-spacing:.5px}
          #wut-sec .wut-st{font-size:12px;color:#8e7b6a;margin:3px 0 10px}
          #wut-sec .wut-row{display:flex;align-items:center;justify-content:space-between;padding:7px 0}
          #wut-sec .wut-t{font-size:13px;color:#5c4a3d}
          #wut-sec .wut-s{font-size:11px;color:#a08b7a;margin-top:1px}
          #wut-sec .wut-sw{position:relative;width:40px;height:22px;flex:0 0 auto}
          #wut-sec .wut-sw input{display:none}
          #wut-sec .wut-sw span{position:absolute;inset:0;background:#cbb9a6;border-radius:11px;
            transition:background .18s;cursor:pointer}
          #wut-sec .wut-sw span::after{content:'';position:absolute;top:2px;left:2px;width:18px;height:18px;
            border-radius:50%;background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.25);transition:left .18s}
          #wut-sec .wut-sw input:checked+span{background:#4a9d6f}
          #wut-sec .wut-sw input:checked+span::after{left:20px}
          #wut-sec .wut-sw.off span{background:#d8c9b8;cursor:default}
          #wut-sec .wut-btns{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
          #wut-sec .wut-b{font-size:12px;color:#6b564a;background:rgba(142,94,67,.10);border:1px solid rgba(142,94,67,.35);
            border-radius:6px;padding:4px 12px;cursor:pointer}
          #wut-sec .wut-b:hover{background:rgba(142,94,67,.18)}
          #wut-sec .wut-b.on{background:#4a9d6f;color:#fff;border-color:#4a9d6f}
          #wut-sec .wut-b.warn{border-color:#c96a5a;color:#a84a3d}
          #wut-sec .wut-rate{display:flex;gap:6px;align-items:center}
        `;
        let st = document.getElementById('wut-style');
        if (!st) { st = document.createElement('style'); st.id = 'wut-style'; st.textContent = css; }
        const el = document.createElement('div'); el.id = 'wut-sec';
        el.innerHTML = `
          <div class="wut-h">宝箱助手 <span style="font-weight:400;font-size:11px;color:#a08b7a">CDP 注入引擎</span></div>
          <div class="wut-st" id="wut-state">正在连接后台引擎…</div>
          <div class="wut-row"><div><div class="wut-t">自动开箱</div><div class="wut-s">宝箱出现约 2 秒后自动点掉，每次消耗 1000 余额</div></div>
            <label class="wut-sw"><input type="checkbox" id="wut-claim"><span></span></label></div>
          <div class="wut-row"><div><div class="wut-t">挂机刷点击</div><div class="wut-s">后台攒余额，不动真鼠标</div></div>
            <label class="wut-sw"><input type="checkbox" id="wut-farm"><span></span></label></div>
          <div class="wut-row"><div><div class="wut-t">桌宠状态牌</div><div class="wut-s">在桌宠页面左上角挂只读状态</div></div>
            <label class="wut-sw"><input type="checkbox" id="wut-hud"><span></span></label></div>
          <div class="wut-row"><div><div class="wut-t">按键气泡</div><div class="wut-s">敲键盘时猫头顶冒小键帽，全局监听不挑窗口</div></div>
            <label class="wut-sw"><input type="checkbox" id="wut-kb"><span></span></label></div>
          <div class="wut-row"><div><div class="wut-t">键帽大小</div><div class="wut-s">上屏约 30 / 36 / 42 px</div></div>
            <div class="wut-rate">
              <button class="wut-b" data-kbsize="s">小</button>
              <button class="wut-b" data-kbsize="m">中</button>
              <button class="wut-b" data-kbsize="l">大</button>
            </div></div>
          <div class="wut-row"><div><div class="wut-t">鼠标左右键也冒</div><div class="wut-s">键帽里画只小鼠标，左/右键半压深，不会跟字母 L R 混</div></div>
            <label class="wut-sw"><input type="checkbox" id="wut-kbmouse"><span></span></label></div>
          <div class="wut-row"><div><div class="wut-t">刷点击速率</div><div class="wut-s">每分钟点击次数（带随机抖动）</div></div>
            <div class="wut-rate">
              <button class="wut-b" data-rate="20">20</button>
              <button class="wut-b" data-rate="40">40</button>
              <button class="wut-b" data-rate="60">60</button>
              <button class="wut-b" data-rate="120">120</button>
            </div></div>
          <div class="wut-btns">
            <button class="wut-b" id="wut-now">立即开箱</button>
            <button class="wut-b" id="wut-kbdemo">试一下按键气泡</button>
            <button class="wut-b warn" id="wut-stop">停止引擎</button>
          </div>`;
        const bind = (id, cmd) => {
          const b = el.querySelector('#' + id);
          b.addEventListener('change', () => W.push({cmd: cmd, value: b.checked}));
        };
        bind('wut-claim', 'claim'); bind('wut-farm', 'farm'); bind('wut-hud', 'hud');
        bind('wut-kb', 'keybubble'); bind('wut-kbmouse', 'kbmouse');
        el.querySelectorAll('.wut-b[data-rate]').forEach(b => b.addEventListener('click', () => {
          W.push({cmd: 'rate', value: parseInt(b.dataset.rate, 10)});
        }));
        el.querySelectorAll('.wut-b[data-kbsize]').forEach(b => b.addEventListener('click', () => {
          W.push({cmd: 'kbsize', value: b.dataset.kbsize});
        }));
        el.querySelector('#wut-kbdemo').addEventListener('click', () => W.push({cmd: 'kbdemo'}));
        el.querySelector('#wut-now').addEventListener('click', () => W.push({cmd: 'claim_now'}));
        el.querySelector('#wut-stop').addEventListener('click', () => W.push({cmd: 'stop'}));
        const container = document.querySelector('.settings-module .panel-content');
        if (!container) return false;
        const ref = container.querySelector('.btn-check-update');
        const anchor = ref ? ref.closest('.setting-item') : container.querySelector('.setting-btns');
        container.insertBefore(st, anchor || null);
        container.insertBefore(el, anchor || null);
        if (!W.watchdog) {
          W.watchdog = setInterval(function () {
            if (Date.now() - W.beat > 8000) {
              const s = document.getElementById('wut-state');
              if (s) s.textContent = '后台引擎已断开（等待重连…）';
              document.querySelectorAll('#wut-sec .wut-sw').forEach(function (x) { x.classList.add('off'); });
            }
          }, 3000);
        }
        return true;
      };
      W.render = function (s) {
        let el = document.getElementById('wut-sec');
        // 页面里的老区块（旧版引擎建的）不会带新行 —— 发现缺控件就拆了重建
        if (el && !el.querySelector('#wut-kb')) { el.remove(); el = null; }
        if (!el) {
          if (!W.build()) return;
          el = document.getElementById('wut-sec');
          if (!el) return;
        }
        const stl = el.querySelector('#wut-state');
        if (stl) stl.textContent = s.text;
        const set = (id, on) => {
          const b = el.querySelector('#' + id);
          if (b) { b.checked = !!on; b.parentElement.classList.toggle('off', s.offline); }
        };
        set('wut-claim', s.claim); set('wut-farm', s.farm); set('wut-hud', s.hud);
        set('wut-kb', s.keybubble); set('wut-kbmouse', s.kbmouse);
        el.querySelectorAll('.wut-b[data-rate]').forEach(b => {
          b.classList.toggle('on', parseInt(b.dataset.rate, 10) === s.rate);
        });
        el.querySelectorAll('.wut-b[data-kbsize]').forEach(b => {
          b.classList.toggle('on', b.dataset.kbsize === (s.kbsize || 'm'));
        });
      };
    }
    W.beat = Date.now();
    W.render(%STATE%);
    return {ok: true, cmds: W.cmds.splice(0)};
  } catch (e) { return {ok: false, reason: String(e)}; }
})()
"""

def make_panel_sync_js(state_json):
    return PANEL_SYNC_JS.replace("%STATE%", state_json)

# ---------- 引擎 ----------
class Tappo:
    def __init__(self):
        self.ws = None
        self._mid = 0
        self.page = None
        self._lock = asyncio.Lock()
        # 面板页（设置界面注入）第二条连接
        self.pws = None
        self.ppage = None
        self._pmid = 0
        self._plock = asyncio.Lock()

    async def connect(self):
        import websockets
        pages = find_pages()
        if not pages:
            raise RuntimeError("9222 上没有页面——Tappo 没开或调试口没生效")
        last = None
        for p in pages[:4]:
            try:
                ws = await websockets.connect(p["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024)
            except Exception as e:
                last = e
                continue
            self.ws, self.page = ws, p
            try:
                r = await self.eval(make_poll_js())  # hud=None: 只读探测，不动页面
                v = (r.get("result") or {}).get("result", {}).get("value") or {}
                if v.get("ok"):
                    return p
            except Exception as e:
                last = e
            try:
                await ws.close()
            except Exception:
                pass
            self.ws = self.page = None
        raise RuntimeError(f"没找到桌宠主页面（GlobalState 不在）: {last}")

    async def connect_panel(self):
        """面板页连接，可独立失败（面板窗口可能还没建）"""
        import websockets
        if self.pws is not None:
            return True
        try:
            pages = find_pages()
        except Exception:
            return False
        for p in pages:
            if "index.html#/panel" in p.get("url", ""):
                try:
                    self.pws = await websockets.connect(p["webSocketDebuggerUrl"], max_size=4 * 1024 * 1024)
                    self.ppage = p
                    return True
                except Exception:
                    self.pws = None
        return False

    async def close_panel(self):
        if self.pws is not None:
            try:
                await self.pws.close()
            except Exception:
                pass
            self.pws = self.ppage = None

    async def _eval_ws(self, ws, lock, js, timeout=10):
        async with lock:
            self._mid += 1
            mid = self._mid
            await ws.send(json.dumps({
                "id": mid, "method": "Runtime.evaluate",
                "params": {"expression": js, "returnByValue": True, "awaitPromise": True}}))
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("CDP evaluate 超时")
                resp = json.loads(await asyncio.wait_for(ws.recv(), left))
                if resp.get("id") == mid:
                    return resp

    async def eval(self, js, timeout=10):
        return await self._eval_ws(self.ws, self._lock, js, timeout)

    async def peval(self, js, timeout=10):
        """在面板页上执行；连接断开时抛异常，调用方负责 close_panel"""
        return await self._eval_ws(self.pws, self._plock, js, timeout)

    def _val(self, r):
        if (r.get("result") or {}).get("exceptionDetails"):
            return {"ok": False, "reason": "js-exception"}
        return (r.get("result") or {}).get("result", {}).get("value") or {"ok": False, "reason": "no-value"}

    async def poll(self, farmed=0, rate=0, hud=None):
        return self._val(await self.eval(make_poll_js(farmed, rate, hud)))

    async def click_chest(self, side):
        return self._val(await self.eval(click_js(side)))

    async def farm_click(self):
        return self._val(await self.eval(FARM_JS))

    async def psync(self, state):
        """同步面板 UI 并取走命令队列；面板断连抛异常"""
        js = make_panel_sync_js(json.dumps(state, ensure_ascii=False))
        return self._val(await self.peval(js))

# ---------- 工具 ----------
def parse_mmss(text):
    m = re.match(r"\s*(\d{1,2})[:：](\d{1,2})", text or "")
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))

# ---------- 主循环 ----------
async def run(claim=True, farm=True, rate=40.0, min_balance=1000, hud=False,
             overrides=None):
    t = Tappo()
    # 热配置：面板 UI 可随时改；config.json 持久化，CLI 显式参数优先并写回
    cfg = {"claim": claim, "farm": farm, "hud": hud, "rate": float(rate),
           "keybubble": True, "kbsize": "m", "kbmouse": True}
    load_cfg(cfg)
    if overrides:
        cfg.update(overrides)
        save_cfg(cfg)
    log("配置: 开箱{} 刷点击{}({:.0f}/分) 状态牌{} 按键气泡{}({})".format(
        "开" if cfg["claim"] else "关", "开" if cfg["farm"] else "关", cfg["rate"],
        "开" if cfg["hud"] else "关", "开" if cfg["keybubble"] else "关", cfg["kbsize"]))
    _clear_stop_flag()  # 引擎活过来了，之前的“主动停止”作废
    await t.connect()
    log(f"已连接桌宠主页面: {t.page['url']}")
    st = await t.poll(rate=(cfg["rate"] if cfg["farm"] else 0), hud=cfg["hud"])
    if not st.get("ok"):
        log("状态读取失败: " + json.dumps(st, ensure_ascii=False)); return
    log(f"余额 {st['balance']:,} | 累计 {st['cumulative']:,} | 已消耗 {st['consume']:,}")
    if cfg["hud"]:
        log("状态牌已挂到桌宠页面左上角")
    if await t.connect_panel():
        log("已连接功能面板页（宝箱助手设置已注入）")
    else:
        log("面板页暂未找到，设置界面将在面板出现后自动注入")

    # ---- 按键气泡：装页面侧脚本 + 起全局轮询线程 ----
    kb_ready = [False]

    async def kb_setup():
        """把 __wutKeys 那套注入桌宠页；断线重连后要和 hud 一起重来"""
        try:
            r = await t.eval(KB_SETUP_JS)
            kb_ready[0] = not (r.get("result") or {}).get("exceptionDetails")
        except Exception:
            kb_ready[0] = False
        if kb_ready[0]:
            try:
                await t.eval(make_kbcfg_js(cfg))
            except Exception:
                pass
        return kb_ready[0]

    await kb_setup()
    if cfg["keybubble"]:
        log("按键气泡已就绪（全局轮询，8ms 一次）")

    async def kb_alive():
        """实测页面里脚本还在不在：True 在 / False 没了 / None 问不出来"""
        try:
            v = t._val(await t.eval(KB_ALIVE_JS))
            if not isinstance(v, dict) or "fn" not in v:
                return None
            return v["fn"] == "function" and bool(v["box"])
        except Exception:
            return None

    watcher = KeyWatcher(enabled=cfg["keybubble"], mouse=cfg["kbmouse"])
    watcher.start()

    # 气泡必须能被画出来：App 把窗口裁成只剩猫那一块，头顶全是窗口外
    rgn = RegionKeeper()
    rgn.start()

    farm_count = [0]
    stop_flag = [False]

    async def farm_loop():
        while True:
            hb["farm"] = time.monotonic()
            try:
                if cfg["farm"] and cfg["rate"] > 0:
                    await asyncio.sleep((60.0 / cfg["rate"]) * random.uniform(0.7, 1.5))
                    r = await t.farm_click()
                else:
                    await asyncio.sleep(2.0)  # 暂停中空转，等面板改回来
                    continue
            except Exception as e:
                log("刷点击协程挂了: " + repr(e))
                return  # 主循环会发现并重建
            if r.get("ok"):
                farm_count[0] += 1
                if farm_count[0] % 100 == 0:
                    log(f"刷点击 {farm_count[0]} 次")
            else:
                log("刷点击失败: " + str(r))
                await asyncio.sleep(2)

    # ---- 按键气泡专用快循环：不等主循环那 1 秒，按下就推 ----
    kb_stat = [0, 0]   # [上次看到的 beats, 上次检查时间]
    _dbg = [0.0]
    _raw = {"prev": set(), "keys": 0, "peak": 0}   # 主线程自己扫一遍，用来对账

    async def kb_loop():
        while True:
            try:
                await asyncio.sleep(0.06)
                hb["kb"] = time.monotonic()

                # 诊断：主线程（asyncio 线程）也扫一次键盘。
                # 主线程看得到、采集线程看不到 → 采集线程的事；
                # 两边都看不到 → 这个进程根本读不到键盘状态。
                try:
                    _g = ctypes.windll.user32.GetAsyncKeyState
                    _g.argtypes = [ctypes.c_int]
                    _g.restype = ctypes.c_short
                    _cur = set(vk for vk in POLL_VKS
                               if (vk != _MOUSE_L and vk != _MOUSE_R) and (_g(vk) & 0x8000))
                except Exception:
                    _cur = set()
                if len(_cur) > _raw["peak"]:
                    _raw["peak"] = len(_cur)
                _raw["keys"] += len(_cur - _raw["prev"])
                _raw["prev"] = _cur

                if time.monotonic() - _dbg[0] >= 5.0:
                    _dbg[0] = time.monotonic()
                    log("KBDEBUG beats=%d keys=%d pushed=%d q=%d raw=%d/%d ready=%s on=%s/%s vis=%s errs=%d err=%s rgn=%d/%d"
                        % (watcher.beats, watcher.keys, watcher.pushed, len(watcher._q),
                           _raw["keys"], _raw["peak"], kb_ready[0],
                           cfg["keybubble"], watcher.enabled, watcher._vis,
                           watcher.errs, watcher.err, rgn.cleared, rgn.probed))
                    _raw["keys"] = 0
                    _raw["peak"] = 0

                # 采集线程自愈：线程死了 / 心跳不动，就重新拉起来。
                # （以前这里出事是静默的——日志照常刷余额，键盘却毫无反应）
                now = time.monotonic()
                if now - kb_stat[1] >= 2.0:
                    kb_stat[1] = now
                    if not watcher.alive():
                        log("键盘采集线程不在了，重新拉起")
                        watcher._th = None
                        watcher.start()
                    elif watcher.beats == kb_stat[0]:
                        log("键盘采集线程心跳不动（%d 次异常），重新拉起: %s"
                            % (watcher.errs, watcher.err))
                        watcher.stop()
                        watcher._th = None
                        watcher.start()
                    kb_stat[0] = watcher.beats

                if not kb_ready[0] or not cfg["keybubble"]:
                    watcher.drain()      # 开关关着/页面没就绪：攒着的键直接扔掉，别等着回放
                    continue
                batch = watcher.drain()
                if not batch:
                    continue
                watcher.pushed += 1
                v = t._val(await t.eval(make_keys_js(batch)))
                if not v.get("ok"):
                    kb_ready[0] = False   # 页面刷新/Vue 重渲染把脚本冲掉了，主循环会重装
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if conn_state[0] != repr(e):
                    log("按键气泡推送失败: " + repr(e))
                await asyncio.sleep(0.4)

    def dispatch(cmds):
        changed = False
        for c in cmds or []:
            k = c.get("cmd")
            if k == "claim" and "value" in c:
                cfg["claim"] = bool(c["value"]); log("面板: 自动开箱 -> " + ("开" if cfg["claim"] else "关")); changed = True
            elif k == "farm" and "value" in c:
                cfg["farm"] = bool(c["value"]); log("面板: 挂机刷点击 -> " + ("开" if cfg["farm"] else "关")); changed = True
            elif k == "hud" and "value" in c:
                cfg["hud"] = bool(c["value"]); log("面板: 桌宠状态牌 -> " + ("开" if cfg["hud"] else "关")); changed = True
            elif k == "rate" and c.get("value"):
                cfg["rate"] = min(120.0, max(5.0, float(c["value"])))
                log(f"面板: 刷点击速率 -> {cfg['rate']:.0f} 次/分钟"); changed = True
            elif k == "keybubble" and "value" in c:
                cfg["keybubble"] = bool(c["value"])
                watcher.set_enabled(cfg["keybubble"])
                log("面板: 按键气泡 -> " + ("开" if cfg["keybubble"] else "关")); changed = True
            elif k == "kbsize" and c.get("value") in ("s", "m", "l"):
                cfg["kbsize"] = c["value"]
                log("面板: 键帽大小 -> " + {"s": "小", "m": "中", "l": "大"}[cfg["kbsize"]]); changed = True
            elif k == "kbmouse" and "value" in c:
                cfg["kbmouse"] = bool(c["value"])
                watcher.set_mouse(cfg["kbmouse"])
                log("面板: 鼠标左右键 -> " + ("开" if cfg["kbmouse"] else "关")); changed = True
            elif k == "kbdemo":
                cfg["_demo"] = True
                log("面板: 试一下按键气泡")
            elif k == "claim_now":
                cfg["_force"] = time.monotonic()
                log("面板: 请求立即开箱")
            elif k == "stop":
                stop_flag[0] = True
                _write_stop_flag()  # 告诉监督员：这是用户主动停的，别拉我起来
                log("面板: 收到停止命令，引擎准备退出")
        if changed:
            save_cfg(cfg)

    def panel_state(bal=0, claimed=0):
        txt = (f"引擎在线 · 余额 {bal:,} · 已刷 {farm_count[0]} · 已开 {claimed} 箱"
               + ("" if cfg["farm"] else " · 刷点击已暂停"))
        return {"text": txt, "claim": cfg["claim"], "farm": cfg["farm"], "hud": cfg["hud"],
                "rate": int(cfg["rate"]), "keybubble": cfg["keybubble"],
                "kbsize": cfg["kbsize"], "kbmouse": cfg["kbmouse"], "offline": False}

    hb = {"loop": time.monotonic(), "farm": time.monotonic(), "kb": time.monotonic()}  # 心跳：看门狗判卡死用

    async def watchdog_loop():
        while True:
            await asyncio.sleep(15)
            stale = [k for k, v in hb.items() if time.monotonic() - v > 90]
            if stale:
                log(f"协程疑似卡死: {', '.join(stale)}（90s 无心跳），硬重启")
                _respawn()
                os._exit(1)  # 卡死状态下正常清理走不动，直接换血

    watchdog_task = asyncio.create_task(watchdog_loop())
    farm_task = asyncio.create_task(farm_loop())
    kb_task = asyncio.create_task(kb_loop())
    if cfg["farm"] and cfg["rate"] > 0:
        log(f"刷点击已启动: {cfg['rate']:.0f} 次/分钟（带随机抖动）")

    seen, tries, lasttry = {}, {}, {}
    claimed = 0
    last_report = time.monotonic()
    last_panel_try = 0.0
    last_watchdog = time.monotonic()
    reconnect_wait = 5.0
    conn_state = [None]  # None=已连, repr=最近一次异常（变化才打日志）
    last_kb_key = [None]

    try:
        while not stop_flag[0]:
            hb["loop"] = time.monotonic()
            try:
                st = await t.poll(farmed=farm_count[0],
                                  rate=(cfg["rate"] if cfg["farm"] and cfg["rate"] > 0 else 0),
                                  hud=cfg["hud"])
                if conn_state[0] is not None:
                    log("重连成功"); conn_state[0] = None; reconnect_wait = 5.0
            except Exception as e:
                if farm_task:
                    farm_task.cancel(); farm_task = None
                kb_ready[0] = False   # 换连接了，页面侧脚本得重装
                key = repr(e)
                if conn_state[0] != key:
                    log(f"连接断了，{reconnect_wait:.0f} 秒后重连: {e}")
                    conn_state[0] = key
                await asyncio.sleep(reconnect_wait)
                reconnect_wait = min(reconnect_wait * 2, 15.0)  # Tappo 没开时退避重试，最多 15s 一次
                try:
                    await t.close_panel()
                    await t.connect()
                except Exception as e2:
                    key2 = repr(e2)
                    if conn_state[0] != key2:
                        log(f"重连失败: {e2}")
                        conn_state[0] = key2
                continue

            if not st.get("ok"):
                log("poll 异常: " + json.dumps(st, ensure_ascii=False))
                await asyncio.sleep(3)
                continue

            # ---- 按键气泡：配置同步 + 试一下（按键推送在 kb_loop 里，走 60ms 快循环）----
            # 页面被刷新/Vue 重渲染会把 __wutKeys 冲掉，而 kb_ready 标志还是 true，
            # 结果就是"引擎以为装好了、页面里其实没有"。这里每轮实测一次再决定补不补。
            alive = await kb_alive()
            if alive is False:
                kb_ready[0] = False
            if not kb_ready[0]:
                await kb_setup()
            try:
                if cfg.pop("_demo", False) and kb_ready[0]:
                    await t.eval(KB_DEMO_JS)
                # 配置变化（面板改了大小/开关）要同步到页面侧
                kb_key = (cfg["keybubble"], cfg["kbsize"], cfg["kbmouse"])
                if kb_key != last_kb_key[0]:
                    last_kb_key[0] = kb_key
                    await t.eval(make_kbcfg_js(cfg))
            except Exception as e:
                if conn_state[0] != repr(e):
                    log("按键气泡配置同步失败: " + repr(e))

            # ---- 面板同步 ----
            now = time.monotonic()
            # ---- 监督员反向保活：每 60s 对一次账 ----
            if now - last_watchdog >= 60:
                last_watchdog = now
                if not watcher_alive():
                    spawn_watcher()
            if t.pws is None and now - last_panel_try >= 30:
                last_panel_try = now
                await t.connect_panel()
                if t.pws is not None:
                    log("已连接功能面板页（宝箱助手设置已注入）")
            if t.pws is not None:
                try:
                    r = await t.psync(panel_state(st.get("balance", 0), claimed))
                    if r.get("ok"):
                        dispatch(r.get("cmds"))
                    elif r.get("reason") and r["reason"] != "no-value":
                        log("面板同步异常: " + str(r.get("reason")))
                except Exception:
                    await t.close_panel()

            if farm_task is None or farm_task.done():
                farm_task = asyncio.create_task(farm_loop())
                log("刷点击协程已自动重启")

            bal = st["balance"]
            forced_ts = cfg.get("_force")
            forced = bool(forced_ts and (now - forced_ts) < 15)

            for side in ("left", "right"):
                slot = st["slots"].get(side) or {}
                mode = slot.get("mode")
                if mode == "chest" and cfg["claim"]:
                    if side not in seen:
                        seen[side] = now
                        tries[side] = 0
                        log(f"[{side}] 宝箱出现")
                    age = now - seen[side]
                    t_tried = tries.get(side, 0)
                    t_last = lasttry.get(side, -99)
                    limit = 9 if forced else 3
                    if ((forced or (age >= 2.0 and (now - t_last) >= 4.0)) and t_tried < limit
                            and bal >= min_balance and not slot.get("disabled")):
                        r = await t.click_chest(side)
                        tries[side] = t_tried + 1
                        lasttry[side] = now
                        log(f"[{side}] 点开箱按钮 -> {r}")
                        if forced:
                            cfg["_force"] = 0
                    elif t_tried >= limit and (now - t_last) > 30:
                        tries[side] = 0  # 半分钟后重新给机会
                elif mode != "chest" and side in seen:
                    claimed += 1
                    log(f"[{side}] 开箱成功 (累计 {claimed})")
                    seen.pop(side, None); tries.pop(side, None); lasttry.pop(side, None)

            if now - last_report >= 30:
                last_report = now
                bal_show = f"{bal:,}"
                farm_show = f"，已刷 {farm_count[0]}" if farm_count[0] else ""
                timers = []
                for side in ("left", "right"):
                    s = (st["slots"].get(side) or {})
                    if s.get("mode") == "timer":
                        timers.append(f"{side} {s.get('text','?')}")
                    elif s.get("mode") == "chest":
                        timers.append(f"{side} 待开")
                log(f"余额 {bal_show}{farm_show} | 槽位: " + (" / ".join(timers) if timers else "无"))

            await asyncio.sleep(1.0)
    except KeyboardInterrupt:
        log("手动停止")
    finally:
        watcher.stop()
        for task in (farm_task, watchdog_task, kb_task):
            if task:
                task.cancel()
        for ws in (t.ws, t.pws):
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
        log(f"收工：共开 {claimed} 箱，刷 {farm_count[0]} 次点击")

# ---------- CLI ----------
def main():
    args = sys.argv[1:]
    setup_crashlog()
    if "--status" in args:
        async def once():
            t = Tappo()
            await t.connect()
            st = await t.poll()
            print(json.dumps(st, ensure_ascii=False, indent=1))
            await t.ws.close()
        asyncio.run(once())
        return
    claim = "--no-claim" not in args
    farm = "--no-farm" not in args
    hud = "--hud" in args   # 状态牌默认不挂：它一直占着左上角，嫌挡视野就关掉，想开用 --hud 或面板开关
    rate = 40.0
    for i, a in enumerate(args):
        if a == "--rate" and i + 1 < len(args):
            rate = float(args[i + 1])
    overrides = {}
    if "--no-claim" in args: overrides["claim"] = False
    if "--no-farm" in args: overrides["farm"] = False
    if "--hud" in args: overrides["hud"] = True
    if "--no-hud" in args: overrides["hud"] = False
    if "--rate" in args: overrides["rate"] = rate
    if "--no-keybubble" in args: overrides["keybubble"] = False
    if "--no-kbmouse" in args: overrides["kbmouse"] = False
    for i, a in enumerate(args):
        if a == "--kbsize" and i + 1 < len(args) and args[i + 1] in ("s", "m", "l"):
            overrides["kbsize"] = args[i + 1]
    min_balance = 1000
    respawn = "--respawn" in args
    quiet = respawn or "--quiet" in args  # 监督员/自我换血拉的，撞锁别弹窗烦人
    lock = acquire_lock()
    if lock is None and respawn:
        # 换血场景：旧进程刚退出、锁还没释放，最多等 20s 接班
        for _ in range(10):
            time.sleep(2)
            lock = acquire_lock()
            if lock is not None:
                break
    if lock is None:
        log("已有引擎在跑（端口 9223 被占），本次启动退出")
        if not quiet:
            alert_dup()
        return
    try:
        asyncio.run(run(claim=claim, farm=farm, rate=rate, min_balance=min_balance, hud=hud,
                        overrides=overrides))
    except KeyboardInterrupt:
        log("手动停止")
    except SystemExit:
        raise
    except BaseException:
        # pythonw 没控制台，顶层异常不打出来就等于无声蒸发——先留遗书再换血
        log("引擎顶层异常，自动重启:\n" + traceback.format_exc())
        _respawn()
    finally:
        lock.close()

if __name__ == "__main__":
    main()
