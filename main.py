"""
DeepSeek 小鲸鱼桌宠 —— 独立程序入口。

不再需要浏览器 / DSH Web 界面：
  - 桌宠直连 DeepSeek API 拉余额（小鲸鱼记账模式）
  - 内置 OpenAI 兼容中转服务（/v1/chat/completions），
    把客户端请求转发 DeepSeek 并统计每轮消耗 —— 类似 DSH 的会话监听
  - 首次启动弹设置对话框，填入 API Key 后缓存到本地配置文件

运行：python main.py
打包：build.bat（PyInstaller 单文件 exe）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from pet.config import is_first_run, load_config, save_config
from pet.proxy import ProxyServer
from pet.whale_widget import WhaleWidget


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("DeepSeek 小鲸鱼")
    app.setQuitOnLastWindowClosed(False)

    # 部分环境 Qt 枚举不到系统字体：显式加载中文字体，否则文字变豆腐块
    from pet.whale_widget import ensure_fonts
    ensure_fonts()

    cfg = load_config()

    # ---- 首次启动：设置对话框引导（可跳过；跳过则桌宠提示未配置） ----
    if is_first_run(cfg):
        from pet.ui.settings_dialog import SettingsDialog
        dlg = SettingsDialog(None, first_run=True)
        dlg.exec()
        cfg = load_config()

    widget = WhaleWidget()
    widget.show()

    # ---- 启动中转服务（独立 api 中转，类似 dsh 的监听） ----
    proxy = None
    if cfg.get("proxy_enabled", True):
        proxy = ProxyServer(
            api_key=cfg.get("api_key", ""),
            api_base=cfg.get("api_base", "https://api.deepseek.com"),
            host=cfg.get("proxy_host", "127.0.0.1"),
            port=int(cfg.get("proxy_port", 11434)),
            on_turn=lambda model, usage, cost, tokens:
                widget.turn_used.emit(model, usage, cost, tokens),
        )
        ok = proxy.start()
        if ok:
            print("[whale-pet] 中转服务已启动:", proxy.base_url)
        else:
            print("[whale-pet] 中转服务端口占用，跳过（可用设置修改端口）")
            proxy = None

    # ---- 托盘 ----
    try:
        widget.setup_tray()
    except Exception:
        pass

    # ---- 首次显示气泡 + 拉余额 ----
    QTimer.singleShot(300, lambda: (widget.show_bubble(), widget.refresh_balance(True)))
    if not cfg.get("api_key"):
        QTimer.singleShot(1200, lambda: QMessageBox.information(
            None, "小鲸鱼", "尚未配置 DeepSeek API Key。\n右键小鲸鱼 → 设置… 填入后可查看余额。"))

    # 置顶显示
    widget.raise_()
    widget.activateWindow()

    def shutdown():
        # 注意：aboutToQuit 内不要再调用 app.quit()，否则会再次触发
        # aboutToQuit 造成无限递归（PySide6 该版本有此行为）。
        try:
            if proxy:
                proxy.stop()
        except Exception:
            pass

    app.aboutToQuit.connect(shutdown)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
