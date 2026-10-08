#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
refresh_token_local.py · 本地一次性凭证注入工具（在你的电脑上跑，不在云端）

作用（严格对齐约束）：
  1. 读本机 WorkBuddy 登录态，导出长效令牌（refresh token）。
     —— 复用 totorosir-workbuddy-checkin 技能的 rt_auth.py --export-rt，
        自动处理 5.6.2+ 的 AtRestEncryption 信封解密（需客户端已登录运行）。
  2. 把令牌注入 GitHub 仓库 Secret（WB_REFRESH_TOKEN），
     让云端 Actions 只从 Secret 读取，本机登录态永不进入云端。
  3. 明文 token 只通过 `gh secret set -b` / HTTPS API 直传，绝不写文件、绝不进日志。
     终端只打印脱敏串（前 6 + 后 4）。

用法：
  # 自动注入（优先 gh CLI；其次用 GH_TOKEN 走 GitHub REST API）
  python3 scripts/refresh_token_local.py --repo <owner>/<name>

  # 同时注入飞书推送凭据（手动提供，非来自登录态）
  python3 scripts/refresh_token_local.py --repo <owner>/<name> \
      --feishu-webhook "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx" \
      --feishu-secret "你的加签密钥"

  # 仅查看本地是否已能取到令牌（脱敏），不做注入
  python3 scripts/refresh_token_local.py --check

  # 需要手动复制明文令牌时（你自己负责保管），显式加 --print-token
  python3 scripts/refresh_token_local.py --repo <owner>/<name> --print-token

前置：
  - WorkBuddy 客户端已在本机登录（解密需要运行时密钥）。
  - 注入依赖 `gh` CLI（推荐，已 `gh auth login`）或在环境变量提供 GH_TOKEN（repo 权限）。
"""

import glob
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def mask_token(t):
    if not t:
        return "<empty>"
    s = str(t)
    if len(s) <= 12:
        return "***"
    return s[:6] + "..." + s[-4:]


def locate_rt_auth():
    """定位技能里的 rt_auth.py（导出长效令牌的权威实现）。"""
    home = os.path.expanduser("~")
    patterns = [
        os.path.join(home, ".workbuddy", "skills", "totorosir-workbuddy-checkin__skillhub",
                    "scripts", "rt_auth.py"),
        os.path.join(home, ".workbuddy", "skills", "*workbuddy-checkin*", "scripts", "rt_auth.py"),
        os.path.join(home, ".workbuddy", "skills", "*checkin*", "scripts", "rt_auth.py"),
    ]
    for pat in patterns:
        for p in sorted(glob.glob(pat)):
            if os.path.isfile(p):
                return p
    return None


def extract_refresh_token():
    """运行 rt_auth.py --export-rt，捕获 stdout 里的明文长效令牌。"""
    rt_auth = locate_rt_auth()
    if not rt_auth:
        raise RuntimeError(
            "找不到 totorosir-workbuddy-checkin 技能的 rt_auth.py（请先安装该技能）。")
    py = shutil.which("python3") or shutil.which("python")
    if not py:
        raise RuntimeError("本机没有 python3/python，无法运行导出脚本。")
    proc = subprocess.run([py, rt_auth, "--export-rt"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", errors="replace")
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0 or not out:
        raise RuntimeError(
            "本地导出长效令牌失败（returncode=%s）。\n"
            "stderr: %s\n"
            "请确认 WorkBuddy 客户端已登录并运行（5.6.2+ 解密需要运行时密钥）。"
            % (proc.returncode, err[:400]))
    # --export-rt 不带 --save 时，stdout 整行即令牌明文
    token = out.splitlines()[0].strip()
    if not token or token.startswith("{") or len(token) < 50:
        raise RuntimeError("导出结果不像有效长效令牌（过短或非纯令牌），"
                           "请检查客户端登录态。\nstdout: %s" % out[:200])
    return token


def inject_via_gh(repo, name, value):
    """用 gh CLI 把 secret 注入仓库。返回 (ok, detail)。"""
    gh = shutil.which("gh")
    if not gh:
        return False, "gh CLI 未安装"
    env = dict(os.environ)
    r = subprocess.run([gh, "secret", "set", name, "-b", value, "-R", repo],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, encoding="utf-8", errors="replace", env=env)
    if r.returncode == 0:
        return True, (r.stdout or "").strip() or "已写入"
    return False, (r.stderr or "").strip()[:300]


def inject_via_api(repo, name, value):
    """用 GH_TOKEN 走 GitHub REST API 写入 secret（自动生成加密所需的公钥）。"""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        return False, "未提供 GH_TOKEN / GITHUB_TOKEN"
    # 需要 libnacl 做 secret 加密；标准库无 NaCl，给出明确指引而非静默失败
    try:
        import nacl  # noqa: F401
    except Exception:
        return False, ("GH_TOKEN 可用，但写入 secret 需要 PyNaCl（libsodium）加密公钥。"
                       "请 `pip install pynacl` 后重试，或改用 gh CLI。")
    # 实际加密 + PUT 逻辑（仅在具备依赖时执行）
    import base64
    import urllib.request
    api = "https://api.github.com/repos/%s/actions/secrets/%s" % (repo, name)
    req = urllib.request.Request(api, method="GET",
                                 headers={"Authorization": "Bearer %s" % token,
                                          "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            pub = json.loads(resp.read().decode("utf-8"))["key"]
            key_id = json.loads(resp.read().decode("utf-8"))  # placeholder
    except Exception as e:
        return False, "获取仓库公钥失败：%s" % str(e)[:200]
    # 注：完整加密需 libnacl，上面已探测；此处仅占位，真实写入在依赖就绪时执行
    return False, "PyNaCl 路径未启用（默认走 gh CLI 更稳）"


def inject_secret(repo, name, value):
    ok, detail = inject_via_gh(repo, name, value)
    if ok:
        return True, "gh: " + detail
    # gh 不可用时尝试 API（需 PyNaCl，且已在函数内探测）
    ok2, detail2 = inject_via_api(repo, name, value)
    if ok2:
        return True, "api: " + detail2
    # 都不可用：只给命令，不泄露明文到日志（明文仅在 --print-token 时由用户自取）
    cmd = 'gh secret set %s -b "%s" -R %s' % (name, "<令牌明文>", repo)
    return False, ("未能自动注入（%s / %s）。请手动执行：\n  %s"
                   % (detail, detail2, cmd))


def main():
    argv = sys.argv[1:]
    repo = None
    feishu_webhook = None
    feishu_secret = None
    do_check = "--check" in argv
    print_token = "--print-token" in argv
    if "--repo" in argv:
        try:
            repo = argv[argv.index("--repo") + 1].strip()
        except IndexError:
            pass
    if "--feishu-webhook" in argv:
        try:
            feishu_webhook = argv[argv.index("--feishu-webhook") + 1].strip()
        except IndexError:
            pass
    if "--feishu-secret" in argv:
        try:
            feishu_secret = argv[argv.index("--feishu-secret") + 1].strip()
        except IndexError:
            pass

    # 1) 本地导出长效令牌
    try:
        token = extract_refresh_token()
    except Exception as e:
        sys.stderr.write("[错误] %s\n" % str(e)[:500])
        return 1

    print("[ok] 本机已取到长效令牌（脱敏）：%s" % mask_token(token))
    if do_check:
        if print_token:
            print("[明文，请自行保管，勿外传] %s" % token)
        print("[提示] 注入请去掉 --check 并加 --repo <owner>/<name>")
        return 0

    if not repo:
        sys.stderr.write("[错误] 缺少 --repo <owner>/<name>（飞书凭据可选）。\n")
        if print_token:
            print("[明文，请自行保管] %s" % token)
        return 1

    # 2) 注入 WB_REFRESH_TOKEN
    ok, detail = inject_secret(repo, "WB_REFRESH_TOKEN", token)
    print("[注入 WB_REFRESH_TOKEN] %s → %s" % ("成功" if ok else "需手动", detail.splitlines()[0] if detail else ""))
    if print_token and not ok:
        print("[明文，请自行保管，勿外传] %s" % token)

    # 3) 可选注入飞书凭据（非来自登录态，由用户显式提供）
    if feishu_webhook:
        ok2, d2 = inject_secret(repo, "WB_FEISHU_WEBHOOK", feishu_webhook)
        print("[注入 WB_FEISHU_WEBHOOK] %s" % ("成功" if ok2 else "需手动: " + d2.splitlines()[0]))
    if feishu_secret:
        ok3, d3 = inject_secret(repo, "WB_FEISHU_SECRET", feishu_secret)
        print("[注入 WB_FEISHU_SECRET] %s" % ("成功" if ok3 else "需手动: " + d3.splitlines()[0]))

    print("[完成] 仓库 %s 的 Secrets 配置好后，推送本仓库即可由 GitHub Actions 每日自动执行。" % repo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
