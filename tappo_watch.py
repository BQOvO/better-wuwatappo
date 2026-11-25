# -*- coding: utf-8 -*-
r"""Wuwa Tappo 宝箱助手 · 监督员（tappo_watch）

职责：看着桌宠进程——桌宠在，引擎就该在。
- 桌宠启动（从没有到有）→ 3s 后拉引擎（原边沿触发，保留）。
- 常态对账：每 30s 查一次，桌宠活着而引擎不在（且不是用户主动停的）→ 重新拉起。
  这是给「引擎中途无声蒸发」兜底的——只靠边沿触发，引擎挂了就永远没人管。
- engine.stopped 标记：面板上按「停止引擎」时引擎会写它；存在=尊重用户，不拉。
  桌宠退出时清掉（新桌宠会话=新意图，下次照常拉）。
- 防鬼畜：1 小时内最多正常拉 5 次；超了转慢速车道（每 10 分钟照试一次），绝不永久撒手。
- 引擎侧每 60s 反向保活监督员（谁活着都能救对方）。
- 自己用端口 9224 做单实例锁；循环内异常不退出，记日志继续值守。
- 日志与引擎同目录: %LOCALAPPDATA%\鸣潮宝箱助手CDP\logs\watch-YYYYMMDD.log

用法:
  pythonw tappo_watch.py          # 常驻（HKCU Run 开机自启）
"""
import os, socket, subprocess, sys, time, traceback

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "tappo_cdp.py")
LOG_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "鸣潮宝箱助手CDP", "logs")
STOP_FLAG = os.path.join(os.path.dirname(LOG_DIR), "engine.stopped")
LOCK_PORT = 9224
ENGINE_LOCK_PORT = 9223
PET_EXE = "WuwaTappo.exe"
POLL = 5.0
RECONCILE = 30.0     # 常态对账间隔
MAX_PULLS = 5        # 1 小时内正常拉引擎次数（防拉活鬼畜）
SLOW_RETRY = 600.0   # 超限后改慢速车道：每 10 分钟还是试一次，绝不永久撒手

_log_fp = None

def log(msg):
    global _log_fp
    line = time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg
    try:
        if _log_fp is None:
            os.makedirs(LOG_DIR, exist_ok=True)
            _log_fp = open(os.path.join(LOG_DIR, time.strftime("watch-%Y%m%d.log")),
                           "a", encoding="utf-8", buffering=1)
        if _log_fp.tell() > 512 * 1024:
            name = _log_fp.name
            _log_fp.close()
            _log_fp = None
            try: os.replace(name, name + ".1")
            except OSError: pass
            _log_fp = open(os.path.join(LOG_DIR, time.strftime("watch-%Y%m%d.log")),
                           "a", encoding="utf-8", buffering=1)
        _log_fp.write(line + "\n")
    except Exception:
        pass
    try:
        print(time.strftime("[%H:%M:%S] ") + msg, flush=True)
    except Exception:
        pass

def _drain_accepts(sock):
    """锁端口同时是「我还活着」的探针：引擎每 60s 连一次。
    不把连接收掉，listen 的待办队列一会儿就满，后面的 connect 会被拒 ——
    引擎就会误判「监督员不见了」，隔一分钟拉一个替身（替身随即退出）。
    所以这里起个线程一直 accept 再立刻关掉。"""
    while True:
        try:
            c, _ = sock.accept()
            try:
                c.close()
            except OSError:
                pass
        except OSError:
            time.sleep(0.2)

def acquire_lock():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(16)
    except OSError:
        return None
    import threading
    threading.Thread(target=_drain_accepts, args=(s,), daemon=True).start()
    return s

def pet_running():
    try:
        r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {PET_EXE}", "/FO", "CSV", "/NH"],
                           capture_output=True, timeout=10,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        return PET_EXE.encode() in r.stdout or PET_EXE in r.stdout.decode("gbk", "ignore")
    except Exception:
        return False

def engine_running():
    """引擎在跑 = 它的锁端口 9223 能被连上"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", ENGINE_LOCK_PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()

def engine_stopped():
    """用户在面板按过「停止引擎」（引擎退出前写的标记）"""
    return os.path.exists(STOP_FLAG)

def clear_stop_flag():
    try:
        os.remove(STOP_FLAG)
    except OSError:
        pass

def pythonw():
    exe = sys.executable
    base = os.path.basename(exe).lower()
    if base == "python.exe":
        cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(cand):
            return cand
    return exe

def spawn_engine():
    pw = pythonw()
    try:
        subprocess.Popen([pw, ENGINE, "--run", "--quiet"], cwd=HERE,
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        log(f"已拉起引擎: {pw} {ENGINE} --run")
    except Exception as e:
        log("拉起引擎失败: " + repr(e))

def main():
    lock = acquire_lock()
    if lock is None:
        log("监督员已在跑（端口 9224 被占），本次退出")
        return
    log("监督员启动: 每 %.0fs 看一眼 %s" % (POLL, PET_EXE))
    pet_was = False
    last_reconcile = time.time()  # 首次对账等一个完整周期，别跟边沿触发的 spawn 抢跑
    pulls = []       # 拉引擎的时间戳（滚动 1h 窗口）
    capped = False   # 是否已打过「慢速重试」日志
    slow_at = 0.0    # 慢速车道上一次尝试的时间
    try:
        while True:
            try:
                pet_now = pet_running()
                now = time.time()
                if pet_now and not pet_was:
                    log("检测到桌宠启动")
                    time.sleep(3.0)  # 等 WebView2 调试口就绪
                    if engine_running():
                        log("引擎已在跑（可能是旧实例），不重复拉起")
                    elif engine_stopped():
                        log("引擎被用户停过（engine.stopped），本次不拉")
                    else:
                        spawn_engine()
                        pulls.append(time.time())
                    last_reconcile = time.time()  # 刚拉过，对账等下个周期
                    capped = False
                    slow_at = 0.0
                elif not pet_now and pet_was:
                    log("桌宠已退出（引擎自行重连等待，不杀）")
                    clear_stop_flag()  # 新桌宠会话=新意图，下次桌宠起来照常拉
                # ---- 常态对账：桌宠活着，引擎就该活着 ----
                if pet_now and now - last_reconcile >= RECONCILE:
                    last_reconcile = now
                    pulls = [t for t in pulls if now - t < 3600]
                    if not engine_running() and not engine_stopped():
                        if len(pulls) < MAX_PULLS:
                            log("对账: 引擎不在了，重新拉起")
                            spawn_engine()
                            pulls.append(time.time())
                            capped = False
                            slow_at = 0.0
                        elif now - slow_at >= SLOW_RETRY:
                            # 以前这里直接撒手到「桌宠重启」为止，用户会长时间以为功能坏了。
                            # 改成慢速车道：每 10 分钟照样试一次，宁可多拉几次也不许躺着不动。
                            slow_at = now
                            if not capped:
                                capped = True
                                log("引擎反复挂（1h 内已拉 %d 次），转慢速重试（每 %.0f 分钟一次）"
                                    % (MAX_PULLS, SLOW_RETRY / 60))
                            log("慢速重试: 再拉一次引擎")
                            spawn_engine()
                pet_was = pet_now
            except Exception:
                # 循环内的意外不许带死监督员——记下来，继续值守
                log("监督员循环异常（继续值守）:\n" + traceback.format_exc())
            time.sleep(POLL)
    except KeyboardInterrupt:
        log("监督员手动停止")
    finally:
        lock.close()

if __name__ == "__main__":
    main()
