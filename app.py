from __future__ import annotations

import os
import sys
from pathlib import Path

# These are set before importing application modules and before any file writes.
ROOT = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ["TEMP"] = os.environ["TMP"] = str(ROOT / 'tmp')
os.environ["PYTHONPYCACHEPREFIX"] = str(ROOT / 'tmp' / 'pycache')


def main():
    import argparse
    import json
    import logging
    from logging.handlers import RotatingFileHandler
    from ba_scout.settings import prepare_runtime_paths, f_path, read_path, load_settings
    prepare_runtime_paths()

    parser = argparse.ArgumentParser(description="断箭战前情报 0.7.4 便携分享版")
    parser.add_argument("--replay-log", help="手动回放指定历史日志；会明确显示历史测试标记")
    parser.add_argument("--self-test", action="store_true", help="界面自动测试，完成后退出")
    parser.add_argument("--test-output", default=str(ROOT / "work" / "ui-test.json"))
    parser.add_argument("--test-hold", type=float, default=0, help="自测完成后保留窗口供截图的秒数")
    parser.add_argument('--runtime-check', action='store_true', help='生成运行环境检查，不读取游戏或查询玩家')
    parser.add_argument('--check-network', action='store_true', help='运行环境检查时验证公开网站 HTTPS 连接')
    parser.add_argument('--check-ocr-image', help='运行环境检查时只读指定 PNG，检查内置姓名识别组件')
    args = parser.parse_args()
    state = ROOT / "data"
    state.mkdir(exist_ok=True)
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(state / "runtime.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    from ba_scout.windows import SingleInstance, enable_dpi
    f_path(args.test_output)
    if args.replay_log:
        read_path(args.replay_log)
    if args.runtime_check:
        import ssl
        import _tkinter
        import platform
        from ba_scout.settings import atomic_json
        result = dict(version='0.7.4', frozen=bool(getattr(sys, 'frozen', False)),
                      executable=sys.executable, root=str(ROOT), temp=os.environ['TEMP'],
                      python=sys.version, bitness=platform.architecture()[0],
                      ssl=ssl.OPENSSL_VERSION, tkinter=_tkinter.__file__,
                      internal=str(getattr(sys, '_MEIPASS', '')))
        if args.check_network:
            from urllib.request import Request, urlopen
            try:
                with urlopen(Request('https://dash.batrace.top/', headers={'User-Agent': 'BA-Scout/0.7.4 runtime-check'}), timeout=8) as response:
                    result['https'] = {'url': response.url, 'status': response.status, 'read_bytes': len(response.read(256))}
            except Exception as exc:
                result['https'] = {'error': str(exc)}
        if args.check_ocr_image:
            import time
            from ba_scout.screen_ocr import ScreenOCR
            from ba_scout.prep_roster import extract_prep_roster
            from ba_scout.settings import default_prep_regions
            image_path = read_path(args.check_ocr_image)
            regions = default_prep_regions()
            ocr = ScreenOCR()
            started = time.monotonic()
            try:
                frame = ocr.submit_extract(image_path, regions=regions, timeout=15).result(timeout=17)
                result['ocr'] = {'elapsed': time.monotonic() - started,
                                 'worker': str(ocr.worker_script), 'frame': frame}
                if frame.get('ok'):
                    result['ocr']['roster'] = extract_prep_roster(frame, regions)
            finally:
                ocr.close()
        atomic_json(f_path(args.test_output), result)
        return
    instance = SingleInstance()
    if instance.already_running:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, "分享版已经在运行。\nCtrl+Alt+B 展开／收起\nCtrl+Alt+M 切换鼠标交互", "断箭战前情报", 0x40)
        instance.close()
        return
    try:
        enable_dpi()
        config = load_settings()
        if args.self_test:
            steam = ROOT / 'work' / 'ui-test-steam' / 'steamapps'
            logs = steam / 'common' / 'broken_arrow' / 'GameLogs'
            logs.mkdir(parents=True, exist_ok=True)
            # The public distribution intentionally excludes extracted game data.
            # A synthetic build ID keeps the UI self-test independent of a game install.
            (steam / 'appmanifest_1604270.acf').write_text('"AppState" { "buildid" "0" }', encoding='utf-8')
            config.update(self_id=990000, log_dir=str(logs), click_through=True, x=None, y=80,
                          hotkey_toggle='CTRL+SHIFT+F11', hotkey_interact='CTRL+SHIFT+F12')
        else:
            from ba_scout.setup import ensure_configured
            config = ensure_configured(config)
            if config is None:
                return
        from ba_scout.ui import ScoutWindow
        window = ScoutWindow(config, args)
        window.root.mainloop()
    finally:
        instance.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        error = traceback.format_exc()
        try:
            (ROOT / "startup-error.txt").write_text(error, encoding="utf-8")
            detail = "详情已写入软件目录 startup-error.txt。"
        except OSError:
            detail = "软件目录不可写，请完整解压到自己有写入权限的文件夹。"
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, "启动失败。" + detail + "\n" + error[-700:], "断箭战前情报", 0x10)
        raise
