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
  3. 明文 token 只通过 HTTPS API / gh CLI 直传，绝不写文件、绝不进日志。
     终端只打印脱敏串（前 6 + 后 4）。

注入路径（两路，自动择优）：
  A. gh CLI（`gh secret set`，若本机已 `gh auth login`）。
  B. GitHub REST API（本机未装 gh 时的主要路径）：
     取仓库公钥 → PyNaCl sealed-box 加密 → PUT 写回 Secret。
     —— 已内置 TLS 回退：本机系统根证书库若缺 Sectigo/USERTrust 根，
        会自动改用 PortableGit 自带的 Mozilla CA 包，避免
        CERTIFICATE_VERIFY_FAILED / SEC_E_UNTRUSTED_ROOT。

用法：
  # 【推荐】交互式注入：PAT / 飞书凭据在脚本内提示（getpass 不回显，
  #   完全不出现在命令行、PowerShell 历史、日志、仓库里）
  & $py scripts/refresh_token_local.py --repo <owner>/<name> -i

  # 环境变量传 PAT（也不入命令行）
  $env:WB_INJECT_GH_TOKEN="github_pat_xxx"
  & $py scripts/refresh_token_local.py --repo <owner>/<name>

  # 命令行显式传（仅在信任的临时终端用；注意 PS 历史会记录，勿用）
  python3 scripts/refresh_token_local.py --repo <owner>/<name> --gh-token "github_pat_xxx"

  # 随参数一并注入飞书凭据（非来自登录态）
  python3 scripts/refresh_token_local.py --repo <owner>/<name> \
      --feishu-webhook "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx" \
      --feishu-secret "你的加签密钥"

  # 仅查看本机是否已能取到令牌（脱敏），不做注入
  python3 scripts/refresh_token_local.py --check

  # 需要手动复制明文令牌时（你自己负责保管），显式加 --print-token
  python3 scripts/refresh_token_local.py --repo <owner>/<name> --print-token

前置：
  - WorkBuddy 客户端已在本机登录（解密需要运行时密钥）。
  - API 路径需要 PyNaCl（libsodium）：`pip install pynacl`；本机已装则自动复用。
  - 细粒度 PAT 需对该仓库具备：Contents 读/写、Workflows 读/写、Secrets 读/写。
"""

import base64
import getpass
import glob
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

# ----------------------------------------------------------------------------
# TLS 兼容层：部分 Windows 机器系统根证书库缺 Sectigo/USERTrust 根，
# 访问 api.github.com 会报 CERTIFICATE_VERIFY_FAILED / SEC_E_UNTRUSTED_ROOT。
# 策略：先走系统默认链（保留企业自签 CA 场景），仅在证书错误时回退到
#       Mozilla CA 包（PortableGit 自带 / certifi）重试一次。
# ----------------------------------------------------------------------------
_CA_OPENER = None


def find_ca_bundle():
    """定位备用 Mozilla CA 包；找不到返回 None。可用 WB_CA_BUNDLE / SSL_CERT_FILE 指定。"""
    for env in ("WB_CA_BUNDLE", "SSL_CERT_FILE"):
        p = (os.environ.get(env) or "").strip()
        if p and os.path.isfile(p):
            return p
    try:
        import certifi  # 可选依赖
        p = certifi.where()
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass
    home = os.path.expanduser("~")
    patterns = [
        os.path.join(home, ".workbuddy", "binaries", "PortableGit", "versions", "*",
                     "mingw64", "etc", "ssl", "certs", "ca-bundle.crt"),
        os.path.join(home, ".workbuddy", "binaries", "PortableGit", "versions", "*",
                     "mingw64", "ssl", "certs", "ca-bundle.crt"),
        r"C:\Program Files\Git\mingw64\etc\ssl\certs\ca-bundle.crt",
        r"C:\Program Files\Git\mingw64\ssl\certs\ca-bundle.crt",
        r"C:\Program Files (x86)\Git\mingw64\etc\ssl\certs\ca-bundle.crt",
        "/etc/ssl/certs/ca-certificates.crt",
    ]
    for pat in patterns:
        for p in sorted(glob.glob(pat)):
            if os.path.isfile(p):
                return p
    return None


def _bundle_opener():
    global _CA_OPENER
    if _CA_OPENER is None:
        cafile = find_ca_bundle()
        if not cafile:
            return None
        try:
            import ssl
            ctx = ssl.create_default_context(cafile=cafile)
            _CA_OPENER = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        except Exception:
            return None
    return _CA_OPENER


def _is_cert_error(e):
    s = ("%s %s" % (type(e).__name__, e)).lower()
    return any(k in s for k in (
        "certificate_verify_failed", "certificate verify failed",
        "unable to get local issuer", "sslcertverificationerror",
        "self signed certificate", "self-signed certificate",
        "untrusted root", "sec_e_untrusted_root", "cert_verify",
    ))


def urlopen_smart(req, timeout=15):
    """带证书链回退的 urlopen。HTTPError 原样抛出，由调用方处理状态码。"""
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError:
        raise
    except Exception as e:
        if not _is_cert_error(e):
            raise
        op = _bundle_opener()
        if op is None:
            raise
        return op.open(req, timeout=timeout)


def mask_token(t):
    if not t:
        return "<empty>"
    s = str(t)
    if len(s) <= 12:
        return "***"
    return s[:6] + "..." + s[-4:]


def _ask(prompt, secret=False):
    """交互读取凭据。secret=True 走 getpass，输入不回显、不入命令行/历史。"""
    try:
        v = getpass.getpass(prompt) if secret else input(prompt)
        return (v or "").strip()
    except Exception:
        return ""


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


def _python_candidates():
    """候选解释器：**优先 sys.executable**（当前运行本脚本的 Python，已知可用，
    且本方案下即你显式调用的 venv Python），再回退 PATH 上的 python3/python，
    最后兜底托管 Python 目录。自动跳过 Windows 商店空壳别名（运行返回 9009）。"""
    cands = []
    if sys.executable:
        cands.append(sys.executable)
    for name in ("python3", "python"):
        p = shutil.which(name)
        if p:
            cands.append(p)
    home = os.path.expanduser("~")
    cands += sorted(glob.glob(os.path.join(
        home, ".workbuddy", "binaries", "python", "versions", "*", "python.exe")))
    cands.append(os.path.join(home, ".workbuddy", "binaries", "python",
                              "envs", "default", "Scripts", "python.exe"))
    out, seen = [], set()
    for c in cands:
        if not c or c in seen:
            continue
        seen.add(c)
        if "windowsapps" in c.lower():
            continue  # Windows 商店空壳别名，执行恒返回 9009
        if os.path.isfile(c):
            out.append(c)
    return out


def _looks_like_rt(tok):
    return bool(tok) and not tok.startswith("{") and len(tok) >= 50


def extract_refresh_token():
    """运行 rt_auth.py --export-rt，捕获 stdout 里的明文长效令牌。

    逐个尝试候选解释器（优先当前脚本所用 Python），规避命中 Windows 商店空壳
    别名 python3 导致的 returncode=9009。
    """
    rt_auth = locate_rt_auth()
    if not rt_auth:
        raise RuntimeError(
            "找不到 totorosir-workbuddy-checkin 技能的 rt_auth.py（请先安装该技能）。")
    cands = _python_candidates()
    if not cands:
        raise RuntimeError("未找到可用 Python 解释器（sys.executable 为空且 PATH 无 python）。")
    errors = []
    for py in cands:
        try:
            proc = subprocess.run([py, rt_auth, "--export-rt"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8", errors="replace")
        except Exception as e:
            errors.append("%s → 启动失败: %s" % (py, str(e)[:120]))
            continue
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        token = out.splitlines()[0].strip() if out else ""
        if proc.returncode == 0 and _looks_like_rt(token):
            print("[info] 已用解释器导出令牌：%s" % py)
            return token
        reason = "returncode=%s" % proc.returncode
        if err:
            reason += " | %s" % err.splitlines()[0][:200]
        errors.append("%s → %s" % (py, reason))
    raise RuntimeError(
        "本地导出长效令牌失败，已尝试 %d 个解释器均不可用：\n  - %s\n"
        "请确认 WorkBuddy 客户端已登录并运行（5.6.2+ 解密需要运行时密钥）。"
        % (len(cands), "\n  - ".join(errors)))


# ----------------------------------------------------------------------------
# 注入路径 A：gh CLI（若已登录）
# ----------------------------------------------------------------------------
def inject_via_gh(repo, name, value):
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


# ----------------------------------------------------------------------------
# 注入路径 B：GitHub REST API（本机无 gh 时的主要路径）
#   取公钥 → PyNaCl sealed-box 加密 → PUT 写回 Secret
# ----------------------------------------------------------------------------
def _ensure_nacl():
    """导入 nacl；缺失则尝试一次性安装到用户 site，仍失败返回 None。"""
    try:
        import nacl  # noqa: F401
        return True
    except Exception:
        pass
    # 尝试 pip install（pip 自带 certifi，不受本机缺根证书影响）
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "pynacl"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
        import nacl  # noqa: F401
        return True
    except Exception:
        return False


def get_repo_public_key(repo, token):
    api = "https://api.github.com/repos/%s/actions/secrets/public-key" % repo
    req = urllib.request.Request(api, headers={
        "Authorization": "Bearer %s" % token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"})
    with urlopen_smart(req, 15) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["key"], body["key_id"]


def encrypt_secret_value(public_key_b64, value):
    """用仓库公钥做 sealed-box 加密，返回 base64 密文。"""
    from nacl import encoding, public
    pub = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
    box = public.SealedBox(pub)
    enc = box.encrypt(value.encode("utf-8"))
    return base64.b64encode(enc).decode("utf-8")


def put_secret(repo, name, value, token):
    key, key_id = get_repo_public_key(repo, token)
    enc = encrypt_secret_value(key, value)
    api = "https://api.github.com/repos/%s/actions/secrets/%s" % (repo, name)
    body = json.dumps({"encrypted_value": enc, "key_id": key_id}).encode("utf-8")
    req = urllib.request.Request(api, data=body, method="PUT", headers={
        "Authorization": "Bearer %s" % token,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28"})
    with urlopen_smart(req, 15) as resp:
        return resp.getcode() in (201, 204)


def inject_via_api(repo, name, value, token):
    """用细粒度 PAT 走 GitHub REST API 写入 secret。"""
    if not token:
        return False, "未提供 GitHub PAT（--gh-token / 环境变量 WB_INJECT_GH_TOKEN / GH_TOKEN）"
    if not _ensure_nacl():
        return False, ("写入 secret 需要 PyNaCl（libsodium）。请 `pip install pynacl` 后重试，"
                       "或改用 gh CLI（`gh auth login`）。")
    try:
        ok = put_secret(repo, name, value, token)
        if ok:
            return True, "已通过 API 写入"
        return False, "API 返回非 201/204（权限不足？需 Secrets 读/写）"
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("message", "")
        except Exception:
            pass
        return False, "API 写入失败 HTTP %s: %s" % (e.code, detail[:200])
    except Exception as e:
        return False, "API 写入异常: %s" % str(e)[:200]


def inject_secret(repo, name, value, gh_token=""):
    # 路径 A：gh CLI 优先（若本机已装且登录）
    ok, detail = inject_via_gh(repo, name, value)
    if ok:
        return True, "gh: " + detail
    # 路径 B：GitHub REST API（需 PAT）
    ok2, detail2 = inject_via_api(repo, name, value, gh_token)
    if ok2:
        return True, "api: " + detail2
    # 都不可用：只给命令，不泄露明文到日志
    cmd = 'gh secret set %s -b "<令牌明文>" -R %s' % (name, repo)
    return False, ("未能自动注入（gh: %s | api: %s）。请手动执行：\n  %s"
                   % (detail, detail2, cmd))


def main():
    argv = sys.argv[1:]
    repo = None
    feishu_webhook = None
    feishu_secret = None
    gh_token = (os.environ.get("WB_INJECT_GH_TOKEN")
                or os.environ.get("GH_TOKEN")
                or os.environ.get("GITHUB_TOKEN") or "").strip()
    do_check = "--check" in argv
    print_token = "--print-token" in argv
    interactive = "--interactive" in argv or "-i" in argv
    if "--repo" in argv:
        try:
            repo = argv[argv.index("--repo") + 1].strip()
        except IndexError:
            pass
    if "--gh-token" in argv:
        try:
            gh_token = argv[argv.index("--gh-token") + 1].strip() or gh_token
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
        print("[提示] 注入请去掉 --check 并加 --repo <owner>/<name>"
              "（API 路径可用 --gh-token 传 PAT）")
        return 0

    if not repo:
        sys.stderr.write("[错误] 缺少 --repo <owner>/<name>（飞书凭据可选）。\n")
        if print_token:
            print("[明文，请自行保管] %s" % token)
        return 1

    # 1.5) 交互式补齐凭据（仅 -i / --interactive 时触发，行为可预测、不会在管道里死锁）
    #   凭据在脚本内提示读取（getpass 不回显），完全不进命令行、历史、日志、仓库。
    if interactive:
        if not gh_token:
            gh_token = _ask("GitHub 细粒度 PAT（输入不回显，粘贴后回车）: ", secret=True)
        if not feishu_webhook:
            feishu_webhook = _ask("飞书 Webhook（可回车跳过）: ")
        if not feishu_secret:
            feishu_secret = _ask("飞书加签 Secret（可回车跳过，不回显）: ", secret=True)

    # 2) 注入 WB_REFRESH_TOKEN
    ok, detail = inject_secret(repo, "WB_REFRESH_TOKEN", token, gh_token=gh_token)
    print("[注入 WB_REFRESH_TOKEN] %s → %s" % ("成功" if ok else "需手动",
                                               detail.splitlines()[0] if detail else ""))
    if print_token and not ok:
        print("[明文，请自行保管，勿外传] %s" % token)

    # 3) 可选注入飞书凭据（非来自登录态，由用户显式提供）
    if feishu_webhook:
        ok2, d2 = inject_secret(repo, "WB_FEISHU_WEBHOOK", feishu_webhook, gh_token=gh_token)
        print("[注入 WB_FEISHU_WEBHOOK] %s" % ("成功" if ok2 else "需手动: " + d2.splitlines()[0]))
    if feishu_secret:
        ok3, d3 = inject_secret(repo, "WB_FEISHU_SECRET", feishu_secret, gh_token=gh_token)
        print("[注入 WB_FEISHU_SECRET] %s" % ("成功" if ok3 else "需手动: " + d3.splitlines()[0]))

    print("[完成] 仓库 %s 的 Secrets 配置好后，GitHub Actions 将每日自动执行（UTC 01:00 = 北京 09:00）。" % repo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
