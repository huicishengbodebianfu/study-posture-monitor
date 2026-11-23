"""本地机密 / 隐私残留扫描。

用法:
    python .github/scripts/check_secrets.py

退出码:
    0 = 未检出敏感内容
    1 = 检出疑似敏感内容（列出路径，请清除后再提交）

扫描范围: 仓库内所有 UTF-8 文本文件（排除 .github/ 自身与本脚本，
          避免脚本内的正则模式误报）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

# 高置信度敏感模式。占位符（CHANGE_ME_* / YOUR_* / sk-xxxx）长度不足，不会命中。
PATTERNS = [
    ("真实设备密钥", r"239f9e115dded0864ff656d5e91dd128"),
    ("真实 API Key (sk-)", r"sk-[A-Za-z0-9]{20,}"),
    ("真实 WiFi / 热点凭据", r"cthy10121|9pimr84b|88888888|ChinaNet-7mfj|19983810807|IQOO12"),
    ("公网 / 云主机 IP", r"\b47\.108\.\d+\.\d+|\b13\.54\.\d+\.\d+|\b172\.19\.\d+\.\d+|\b172\.31\.\d+\.\d+"),
    ("本机绝对路径 (Windows)", r"[A-Za-z]:\\(AI学习伙伴|Users|DSH)|E:\\|sengz"),
    ("SSH 私钥 / 指纹", r"\.pem\b|PRIVATE KEY|SHA256:[A-Za-z0-9+/=]{20,}|dsh-posture-deploy|root-friend-access|swas-imported-key|id_rsa"),
    ("不安全登录写法", r"StrictHostKeyChecking"),
    ("个人手机号", r"\b1[3-9]\d{9}\b"),
]

# 允许的安全占位 / 示例写法（命中后先查是否在白名单里，避免误杀文档说明）
SAFE = [
    "CHANGE_ME",
    "YOUR_",
    "sk-xxxxxxxx",
    "<your-",
    "your-key.pem",
    "deploy_key",
]


def is_safe(line: str) -> bool:
    return any(s in line for s in SAFE)


def scan() -> list[tuple[str, str, str]]:
    findings: list[tuple[str, str, str]] = []
    # 根目录元数据文件用于描述"我们排除了什么"，其中会提及 .pem / SHA256 等字样，
    # 属于说明性文字而非凭据，因此排除。
    meta = {
        "README.md", "LICENSE", ".gitignore", "CONTRIBUTING.md",
        "数据脱敏与排除说明.md",
    }
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(".github/") or rel.startswith(".git/") or path.name in meta:
            continue
        # 二进制 / 大文件跳过
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for label, pattern in PATTERNS:
            for line_no, line in enumerate(text.splitlines(), start=1):
                if re.search(pattern, line) and not is_safe(line):
                    findings.append((rel, label, str(line_no)))
    return findings


def main() -> int:
    findings = scan()
    if not findings:
        print("✅ check_secrets: 未检出敏感内容")
        return 0

    print(f"❌ check_secrets: 发现 {len(findings)} 处疑似敏感内容，请清除后再提交")
    for rel, label, line_no in findings:
        print(f"  [{rel}:{line_no}] {label}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
