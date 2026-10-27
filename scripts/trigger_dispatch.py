#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
外部定时触发 WorkBuddy 每日签到 workflow（长期稳妥方案）。

为什么需要它：
  GitHub 自带 cron（schedule）在免费队列会被延迟/丢弃，实测连续多日 0 触发。
  改用外部定时服务（cron-job.org / UptimeRobot 等）每日 POST workflow_dispatch API，
  由外部服务保证按时触发，不再依赖 GitHub 调度。

用法：
  # 1) 仅打印将要发送的 HTTP 请求（不真正触发，便于核对，无需 token）
  python3 scripts/trigger_dispatch.py --dry-run

  # 2) 真正触发（需要一条对该仓库具备 Actions: Read and write 的 PAT）
  #    推荐用【独立】的细粒度令牌，仅授予此仓库 Actions: Read and write，
  #    与 WB_GH_PAT（用于令牌自续期，需 Contents/Secrets 写权限）分离，降低泄露面。
  set GH_DISPATCH_TOKEN=github_pat_xxx
  python3 scripts/trigger_dispatch.py

  # 3) 指定其它仓库 / 分支
  python3 scripts/trigger_dispatch.py --repo owner/name --ref main

环境变量：
  GH_DISPATCH_TOKEN  具备 Actions: Read and write 的 PAT（必填，非 dry-run 时）
  GH_DISPATCH_REPO   仓库 owner/name（也可用 --repo 指定；两者都无则报错）

外部服务（cron-job.org）等价配置：
  URL   : https://api.github.com/repos/simona20261008-ctrl/workbuddy/actions/workflows/daily-checkin.yml/dispatches
  Method: POST
  Headers:
    Authorization: Bearer <上面的独立 PAT>
    Accept: application/vnd.github+json
    Content-Type: application/json
    X-GitHub-Api-Version: 2022-11-28
  Body : {"ref":"main"}
  Schedule: 每日 08:30 (Asia/Shanghai) —— cron-job.org 选时区后填 30 8 * * *；用 UTC 则填 30 0 * * *
            （务必在服务端 UTC 日切/北京 08:00 之后触发，否则签到会记到前一天）
"""

import glob
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

# 不硬编码仓库，模板可迁移：优先 GH_DISPATCH_REPO 环境变量，否则需 --repo 指定
DEFAULT_REPO = os.environ.get("GH_DISPATCH_REPO", "").strip()
DEFAULT_REF = "main"
API_VERSION = "2022-11-28"


def find_ca_bundle():
    """定位备用 Mozilla CA 包；找不到返回 None。"""
    for env in ("GH_CA_BUNDLE", "SSL_CERT_FILE", "WB_CA_BUNDLE"):
        p = (os.environ.get(env) or "").strip()
        if p and os.path.isfile(p):
            return p
    home = os.path.expanduser("~")
    patterns = [
        os.path.join(home, ".workbuddy", "binaries", "PortableGit", "versions", "*",
                     "mingw64", "etc", "ssl", "certs", "ca-bundle.crt"),
        os.path.join(home, ".workbuddy", "binaries", "PortableGit", "versions", "*",
                     "mingw64", "ssl", "certs", "ca-bundle.crt"),
        "/etc/ssl/certs/ca-certificates.crt",
    ]
    for pat in patterns:
        for p in sorted(glob.glob(pat)):
            if os.path.isfile(p):
                return p
    return None


def build_opener():
    cafile = find_ca_bundle()
    if cafile:
        try:
            ctx = ssl.create_default_context(cafile=cafile)
            return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        except Exception:
            pass
    return None


def mask(tok):
    if not tok:
        return "<empty>"
    s = str(tok)
    return s[:6] + "..." + s[-4:] if len(s) > 12 else "***"


def main():
    argv = sys.argv[1:]
    dry_run = "--dry-run" in argv or "-n" in argv
    repo = DEFAULT_REPO
    ref = DEFAULT_REF
    if "--repo" in argv:
        try:
            repo = argv[argv.index("--repo") + 1]
        except (IndexError, ValueError):
            pass
    if "--ref" in argv:
        try:
            ref = argv[argv.index("--ref") + 1]
        except (IndexError, ValueError):
            pass

    if not repo:
        print("[error] 未指定仓库：请用 --repo owner/name 或设置环境变量 GH_DISPATCH_REPO。")
        return 1

    url = "https://api.github.com/repos/%s/actions/workflows/daily-checkin.yml/dispatches" % repo
    token = (os.environ.get("GH_DISPATCH_TOKEN") or "").strip()
    headers = {
        "Authorization": "Bearer %s" % token,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "workbuddy-dispatch-trigger/1.0",
    }
    body = json.dumps({"ref": ref}).encode("utf-8")

    if dry_run:
        print("[dry-run] 将发送以下请求（不真正触发）：")
        print("  POST %s" % url)
        print("  Headers:")
        for k, v in headers.items():
            if k == "Authorization":
                print("    Authorization: Bearer %s" % (mask(token) if token else "<GH_DISPATCH_TOKEN 未设置>"))
            else:
                print("    %s: %s" % (k, v))
        print("  Body: %s" % body.decode("utf-8"))
        print("\n说明：dry-run 不需要 token。真正触发请设置 GH_DISPATCH_TOKEN 后去掉 --dry-run。")
        return 0

    if not token:
        print("[error] 缺少 GH_DISPATCH_TOKEN（需对该仓库 Actions: Read and write 的 PAT）。")
        print("        先用 --dry-run 核对请求，再设置环境变量。")
        return 1

    req = urllib.request.Request(url, data=body, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)

    op = build_opener()
    try:
        if op is None:
            with urllib.request.urlopen(req, timeout=20) as resp:
                code = resp.getcode()
        else:
            with op.open(req, timeout=20) as resp:
                code = resp.getcode()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        print("[error] 触发失败 HTTP %s：%s" % (e.code, detail))
        if e.code in (401, 403):
            print("        排查：PAT 是否具备该仓库 Actions: Read and write；是否过期/被撤销。")
        elif e.code == 404:
            print("        排查：仓库名/分支是否正确；workflow 文件名是否为 daily-checkin.yml。")
        return 1
    except Exception as e:
        print("[error] 触发异常：%s" % e)
        return 1

    if code in (200, 204):
        print("[ok] 已成功触发 workflow_dispatch（仓库=%s, ref=%s）。" % (repo, ref))
        print("     稍后到 GitHub → Actions 查看运行；若仍失败，打开该次 run 复制 [preflight]/[info 刷新接口]/[error] 三行。")
        return 0
    print("[error] 触发返回非预期状态码 HTTP %s" % code)
    return 1


if __name__ == "__main__":
    sys.exit(main())
