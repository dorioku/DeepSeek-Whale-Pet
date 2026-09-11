#!/usr/bin/env python3
"""DeepSeek 小鲸鱼桌宠 —— 一键构建 / 发布脚本。

用法：
    python release.py            仅构建，产物 dist/WhalePet.exe
    python release.py 0.3        构建 + 提交 + 打标签 + 推送 + 发布 GitHub Release v0.3
    python release.py 0.3 -y     跳过发布前的二次确认（自动化用）

前置条件：
    - Python 3.10+；构建依赖（requirements.txt / pyinstaller / pillow）由脚本自动安装
    - 发布需要 git 与 GitHub CLI（gh），且已执行过 gh auth login
    - 打包参数在 WhalePet.spec 中修改

说明：脚本用 Python 而非 .bat，是因为 cmd 读取 UTF-8 批处理时会在多字节行处
错位（中文说明文本会把行拆断），Python 的 UTF-8 处理与参数传递都更可靠。
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = ROOT / "WhalePet.spec"
EXE = ROOT / "dist" / "WhalePet.exe"

# 访问 GitHub 的代理；本机不需要代理时改成空字符串
DEFAULT_PROXY = "socks5://127.0.0.1:10808"

NOTES_TEMPLATE = """**DeepSeek 小鲸鱼桌宠 · 独立桌面版** —— {tag}

Windows 右下角的透明置顶小鲸鱼：直连 DeepSeek API 显示余额，并统计「今日已用」与「每轮对话消耗」。

{extra}## 下载

| 文件 | 说明 |
| --- | --- |
| `WhalePet.exe` | Windows 10 / 11 x64 单文件版（约 {size} MB），双击即可运行，无需安装 Python |

首次运行会弹出设置窗口，填入 **DeepSeek API Key** 后自动缓存到 `%APPDATA%\\WhalePet\\config.json`；
把 `config.json` 放在 exe 同目录即可作为便携版使用。

## 校验值

```
SHA256  {sha}
```

## 说明

- 未做代码签名，Windows SmartScreen 可能提示「未知发布者」，选择「仍要运行」即可。
- 由 `release.py` 构建发布（PyInstaller 单文件打包，配置见 `WhalePet.spec`）。
- 源码与更新日志：https://github.com/{repo}
- 美术素材、峰谷定价规则与气泡视觉参数来自 [DeepSeek-Balance-Whale-Widget](https://github.com/MeteorNOX/DeepSeek-Balance-Whale-Widget)（MIT License），详见仓库 README 的「致谢」。
"""

# 各版本的「本次更新」（键 = 版本号，如 "0.3"）；没有就留空
RELEASE_NOTES = {
    "0.3": """## 本次更新

- **全新界面风格**：右键菜单、托盘菜单与设置 / 账单 / 词典页统一成 Win11 风格 —— 圆角 + 半透明磨砂玻璃
  （Win11 上走 DWM 亚克力模糊，老系统自动降级为半透明卡片）、菜单项图标、悬停高亮、强调色主按钮。
- **台词词典编辑页**：不再直接丢进文本编辑器，改成和「设置」一样的编辑页面 ——
  左边台词组、右边权重 / 类型 / 样式 / 换行 / 音效与每一条台词，支持新建 · 复制 · 删除 · 排序 · 恢复内置默认；
  保存即生效（原子写入、不丢字段），页面里仍保留「用文本编辑器打开」给习惯改 JSON 的人。
- **「气泡」二级菜单**：换一条台词 / 收起气泡 / 启用台词气泡。
- **误差金额红绿显示**：气泡里「今日已用 ¥ x.xx (+0.12)」的括号按正负着绿 / 红，一眼看出偏差方向。
- **修复**：长台词（如「真当我是便宜货啊...」）首尾被裁掉一截；菜单第二次弹出时毛玻璃失效；
  标题栏图标发虚、标题字号字重偏大（改为 Win11 的 12px 常规字重）。

""",
}


def say(msg: str = "") -> None:
    print(msg, flush=True)


def fail(msg: str) -> None:
    say(f"[错误] {msg}")
    sys.exit(1)


def run(cmd: list[str], capture: bool = False) -> subprocess.CompletedProcess:
    """在项目根目录执行命令；capture=True 时返回输出文本。"""
    return subprocess.run(
        cmd,
        cwd=ROOT,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def find_gh() -> str:
    """定位 gh（刚装完可能还没进 PATH，回退到默认安装目录）。"""
    exe = shutil.which("gh")
    if exe:
        return exe
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "GitHub CLI" / "gh.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "GitHub CLI" / "gh.exe",
    ]
    for path in candidates:
        if path.is_file():
            return str(path)
    fail("未找到 GitHub CLI（gh），请先安装：winget install GitHub.cli")
    raise AssertionError  # 仅为让类型检查满意


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(total: int) -> None:
    say(f"=== [1/{total}] 安装构建依赖 ===")
    if run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt",
            "pyinstaller", "pillow", "-q"]).returncode != 0:
        fail("依赖安装失败")

    say(f"=== [2/{total}] PyInstaller 打包（单文件） ===")
    if run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            str(SPEC)]).returncode != 0:
        fail("打包失败，请检查上方输出")

    if not EXE.is_file():
        fail(f"未找到产物 {EXE}")


def publish(tag: str, version: str, size_mb: int, sha: str, assume_yes: bool) -> None:
    gh = find_gh()

    if run(["git", "rev-parse", "--git-dir"], capture=True).returncode != 0:
        fail("当前目录不是 git 仓库")
    if run(["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"],
           capture=True).returncode == 0:
        fail(f"标签 {tag} 已存在，请换一个版本号")

    if run([gh, "auth", "status"], capture=True).returncode != 0:
        fail("GitHub CLI 未登录，请先运行：gh auth login")

    repo = (run([gh, "repo", "view", "--json", "nameWithOwner",
                 "--jq", ".nameWithOwner"], capture=True).stdout or "").strip()

    dirty = bool((run(["git", "status", "--porcelain"], capture=True).stdout or "").strip())
    if dirty:
        say("   待提交改动：")
        say(run(["git", "status", "--short"], capture=True).stdout or "")

    if not assume_yes:
        answer = input(f"即将发布 {tag} 到 {repo}（推送/发布后不可撤销），确认？(y/N) ")
        if answer.strip().lower() not in ("y", "yes"):
            say("已取消，未做任何改动。")
            sys.exit(1)

    say(f"=== [4/5] 提交并打标签 {tag} ===")
    if dirty:
        if run(["git", "add", "-A"]).returncode != 0:
            fail("git add 失败")
        if run(["git", "commit", "-m", f"chore(release): {tag}"]).returncode != 0:
            fail("提交失败")
    if run(["git", "tag", "-a", tag, "-m", tag]).returncode != 0:
        fail("创建标签失败")
    if run(["git", "push", "origin", "HEAD"]).returncode != 0:
        fail("推送分支失败（标签已建在本地，修正后可重跑）")
    if run(["git", "push", "origin", tag]).returncode != 0:
        fail("推送标签失败")

    say("=== [5/5] 创建 GitHub Release ===")
    notes = Path(tempfile.gettempdir()) / f"whalepet-{tag}-notes.md"
    notes.write_text(NOTES_TEMPLATE.format(tag=tag, size=size_mb, sha=sha, repo=repo,
                                           extra=RELEASE_NOTES.get(version, "")),
                     encoding="utf-8")
    result = run([gh, "release", "create", tag, str(EXE), "--title", tag,
                  "--notes-file", str(notes), "--verify-tag"])
    if result.returncode != 0:
        fail(f"创建 Release 失败；标签已推送，修正后可重跑：\n"
             f'    gh release create {tag} "{EXE}" --notes-file "{notes}"')
    notes.unlink(missing_ok=True)

    say()
    say(f"[完成] https://github.com/{repo}/releases/tag/{tag}")


def main() -> None:
    args = [a for a in sys.argv[1:] if a not in ("-y", "--yes")]
    assume_yes = len(args) != len(sys.argv[1:])

    version = args[0].lstrip("vV") if args else ""
    if version and not re.fullmatch(r"\d+(\.\d+)*", version):
        fail("版本号格式不对，应形如：python release.py 0.3")

    if DEFAULT_PROXY:
        os.environ.setdefault("HTTPS_PROXY", DEFAULT_PROXY)
        os.environ.setdefault("HTTP_PROXY", DEFAULT_PROXY)

    tag = f"v{version}"
    total = 5 if version else 3

    build(total)

    size_mb = round(EXE.stat().st_size / 1048576)
    sha = sha256_of(EXE)
    say()
    say(f"   产物  : {EXE.relative_to(ROOT)}（约 {size_mb} MB）")
    say(f"   SHA256: {sha}")
    say()

    if not version:
        say(f"=== [3/{total}] 完成 ===")
        say(f"[完成] 仅构建完成，未发布。发布请运行：python release.py 0.3")
        return

    publish(tag, version, size_mb, sha, assume_yes)


if __name__ == "__main__":
    main()
