#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 每日签到 + 派猫猫旅行 · 云端运行脚本（GitHub Actions 专用）

设计要点（严格对齐用户约束）：
  1. 云端无本机登录态：凭证只从 GitHub Secret 读取（WB_REFRESH_TOKEN），
     绝不读本地登录态、绝不打印明文 token、绝不写日志泄露。
  2. 长效令牌换接口令牌走官方插件网关刷新接口（通道 C 实测接口名）。
  3. 一次跑完「签到 + 派猫猫」：先领掉已到家的旅行积分，再判断能否派新一趟；
     今天已派过（traveling / 每日上限）就跳过派遣。
  4. 结果推送到飞书，签到与猫猫状态分两段写清楚。
  5. 猫猫部分失败不影响签到结论：签到成功即视为整体成功（退出码 0）。
  6. 接口名以实际请求为准（取自 totorosir-workbuddy-checkin 技能的实测契约），
     不用网上流传的旧接口名 / 旧域名。

环境变量（均由 GitHub Secret 注入）：
  WB_REFRESH_TOKEN   长效令牌（必填）
  WB_FEISHU_WEBHOOK  飞书群机器人 webhook（必填，用于推送）
  WB_FEISHU_SECRET   飞书机器人加签密钥（可选）

本地验证（无需真实凭据，不联网）：
  python3 scripts/run_checkin.py --dry-run
"""

import base64
import glob
import hashlib
import hmac
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

VERSION = "1.0.0"

# ----------------------------------------------------------------------------
# 实际接口（取自 totorosir-workbuddy-checkin 技能实测契约，禁止改成旧域名/旧路径）
# ----------------------------------------------------------------------------
PLUGIN_API = "https://copilot.tencent.com"
REFRESH_PATH = "/v2/plugin/auth/token/refresh"          # 长效令牌 → 接口令牌

# 签到域：刷新所得接口令牌实测可被该域接受（api-spec 8.3）
BILLING_BASE = "https://www.codebuddy.cn/v2"
CHECKIN_STATUS_PATH = "/billing/meter/checkin-activity-status"   # POST 状态
CHECKIN_CLAIM_PATH = "/billing/meter/daily-checkin"              # POST 领取

# 旅行域：与签到不是同一域名，且路径不带 /v2 前缀（api-spec 3）
TRAVEL_BASE = "https://www.workbuddy.cn"
TRAVEL_STATUS_PATH = "/activity/growth/buddy/travel/status"      # GET 状态
TRAVEL_CLAIM_PATH = "/activity/growth/buddy/travel/claim"        # POST 领取
TRAVEL_DEPART_PATH = "/activity/growth/buddy/travel/depart"      # POST 派遣
TRAVEL_CONFIG_PATH = "/activity/growth/buddy/travel/config"      # GET 可选地点

MAX_RETRY = 2
HTTP_TIMEOUT = 15

TRAVEL_STATE_TEXT = {
    "idle": "空闲",
    "traveling": "旅行中",
    "arrived": "已到达待领取",
}

LOCATION_NAMES = {1: "咖啡馆", 2: "商场店铺", 3: "健身房", 4: "古镇客栈"}


# ----------------------------------------------------------------------------
# 脱敏：任何输出里的 token 一律打码，绝不回显明文
# ----------------------------------------------------------------------------
def mask_token(t):
    if not t:
        return "<empty>"
    s = str(t)
    if len(s) <= 12:
        return "***"
    return s[:6] + "..." + s[-4:]


def mask_webhook(u):
    if not u:
        return "<unset>"
    try:
        # 只保留 host 与 key 尾段，绝不回显完整 webhook
        from urllib.parse import urlparse
        p = urlparse(u)
        q = p.query
        key = ""
        for kv in q.split("&"):
            if kv.lower().startswith("key="):
                key = kv.split("=", 1)[1]
        if key:
            return "%s://%s/...?key=%s" % (p.scheme, p.netloc, mask_token(key))
        return "%s://%s/..." % (p.scheme, p.netloc)
    except Exception:
        return "<webhook>"


def _deep_mask(obj):
    """递归把响应里任何 token 字段打码（防止刷新应答里的 accessToken/refreshToken 泄露）。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and ("token" in k.lower() or k.lower() in ("at", "rt", "jwt")):
                out[k] = mask_token(v)
            else:
                out[k] = _deep_mask(v)
        return out
    if isinstance(obj, list):
        return [_deep_mask(x) for x in obj]
    return obj


# ----------------------------------------------------------------------------
# TLS 兼容层：部分 Windows 机器的系统根证书库缺 Sectigo/USERTrust 根，
# 会报 SSL: CERTIFICATE_VERIFY_FAILED / schannel SEC_E_UNTRUSTED_ROOT。
# 策略：先走系统默认证书链（保留企业自签 CA 场景），仅在证书验证失败时
#       回退到 Mozilla CA 包重试一次（git/PortableGit 自带，或 certifi）。
# Linux runner 上通常首次即成功，不会走到回退分支。
# ----------------------------------------------------------------------------
_CA_OPENER = None


def find_ca_bundle():
    """定位备用 Mozilla CA 包；找不到返回 None。可用 WB_CA_BUNDLE 显式指定。"""
    for env in ("WB_CA_BUNDLE", "SSL_CERT_FILE"):
        p = (os.environ.get(env) or "").strip()
        if p and os.path.isfile(p):
            return p
    try:
        import certifi  # 可选依赖，不强制
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
    """用备用 CA 包构建 opener（惰性、缓存）。无可用 CA 包时返回 None。"""
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


def urlopen_smart(req, timeout=HTTP_TIMEOUT):
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


# ----------------------------------------------------------------------------
# HTTP 封装（标准库，零依赖）
# ----------------------------------------------------------------------------
def api_call(base, path, token, method="POST", payload=None, raw=False):
    """统一请求。返回 (http_code, body)。body 为解析后的 dict（raw=True 时返回原始文本）。"""
    url = base + path
    data = None
    headers = {
        "Authorization": "Bearer %s" % token,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "workbuddy-actions-checkin/%s" % VERSION,
    }
    if payload is not None and method != "GET":
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urlopen_smart(req, HTTP_TIMEOUT) as resp:
            code = resp.getcode()
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            text = e.read().decode("utf-8", "replace")
        except Exception:
            text = ""
    except Exception as e:
        # 网络级错误：返回非 0 码 + 错误信息，由上层判定
        return -1, {"raw": str(e)}
    if raw:
        return code, text
    try:
        return code, json.loads(text)
    except Exception:
        return code, {"raw": text[:500]}


# ----------------------------------------------------------------------------
# 长效令牌刷新（通道 C 实测接口）
# ----------------------------------------------------------------------------
def refresh_token(rt):
    """用长效令牌换新接口令牌。返回 (http_code, 原始应答 dict)。"""
    url = PLUGIN_API + REFRESH_PATH
    req = urllib.request.Request(url, data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Refresh-Token", rt)
    req.add_header("X-Auth-Refresh-Source", "plugin")
    req.add_header("X-Domain", "copilot.tencent.com")
    req.add_header("User-Agent", "workbuddy-actions-checkin/%s" % VERSION)
    try:
        with urlopen_smart(req, HTTP_TIMEOUT) as resp:
            return resp.getcode(), json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, {"code": -1, "msg": "HTTP %s" % e.code}
    except Exception as e:
        return -1, {"code": -1, "msg": str(e)[:200]}


# ----------------------------------------------------------------------------
# 签到（状态查询 → 必要时领取），逻辑对齐技能 _do_checkin
# ----------------------------------------------------------------------------
def do_checkin(token):
    result = {"status": "unknown", "action": None, "points": None,
              "balance": None, "msg": "", "detail": {}}
    raw_log = []  # 脱敏后的原始返回，用于验收展示
    attempt = 0
    while attempt <= MAX_RETRY:
        attempt += 1
        try:
            # 1) 状态查询
            st_code, st_body = api_call(BILLING_BASE, CHECKIN_STATUS_PATH, token)
            raw_log.append(("GET/POST checkin-activity-status", st_code, _deep_mask(st_body)))
            result["detail"]["status_http"] = st_code
            result["balance"] = _extract_balance(st_body)

            today_signed = False
            if isinstance(st_body, dict):
                if st_body.get("today_checked_in") is True:
                    today_signed = True
                elif (st_body.get("data") or {}).get("today_checked_in") is True:
                    today_signed = True
                elif str(st_body.get("code")) == "10001":
                    today_signed = True

            if today_signed:
                bal = ("，当前积分余额 %s" % result["balance"]) if result.get("balance") is not None else ""
                result.update(status="ok", action="skip_already_signed",
                              msg="今日已签到，无需重复领取" + bal)
                result["detail"]["raw"] = raw_log
                return result

            # 2) 领取
            ck_code, ck_body = api_call(BILLING_BASE, CHECKIN_CLAIM_PATH, token)
            raw_log.append(("POST daily-checkin", ck_code, _deep_mask(ck_body)))
            result["detail"]["checkin_http"] = ck_code

            if isinstance(ck_body, dict):
                code = str(ck_body.get("code", ""))
                msg = ck_body.get("msg") or ck_body.get("message") or ""
                data = ck_body.get("data") if isinstance(ck_body.get("data"), dict) else {}
                if code == "10001" or "已签到" in msg or "今天已签到" in msg:
                    result.update(status="ok", action="skip_already_signed",
                                  msg="今日已签到（接口返回 code=10001）")
                    result["detail"]["raw"] = raw_log
                    return result
                success_code = code in ("", "0", "200")
                if 200 <= ck_code < 300 and success_code:
                    credit = (ck_body.get("credit") or data.get("credit")
                              or data.get("daily_credit") or data.get("today_credit"))
                    streak = ck_body.get("streak_days") or data.get("streak_days")
                    bbal = _extract_balance(ck_body)
                    if bbal is not None:
                        result["balance"] = bbal
                    bal = ("，当前积分余额 %s" % result["balance"]) if result.get("balance") is not None else ""
                    result.update(status="ok", action="clicked", points=credit,
                                  msg="领取成功"
                                      + (("，+%s 积分" % credit) if credit else "")
                                      + (("，连续第 %s 天" % streak) if streak else "")
                                      + bal)
                    result["detail"]["streak_days"] = streak
                    result["detail"]["raw"] = raw_log
                    return result
                result.update(status="error", action="failed",
                              msg=msg or ("HTTP %s（业务码 %s）" % (ck_code, code)))
                result["detail"]["raw"] = raw_log
                return result
            result.update(status="error", action="failed",
                          msg="领取接口返回非 JSON")
            result["detail"]["raw"] = raw_log
            return result

        except urllib.error.HTTPError as e:
            last = "HTTP %s: %s" % (e.code, e.reason)
        except urllib.error.URLError as e:
            last = "网络错误: %s" % e.reason
        except Exception as e:
            last = "异常: %s" % e
        if attempt <= MAX_RETRY:
            time.sleep(2)

    result.update(status="error", msg="重试 %d 次后仍失败: %s" % (MAX_RETRY, last))
    result["detail"]["raw"] = raw_log
    return result


# ----------------------------------------------------------------------------
# 派猫猫旅行（先领后派），与技能 travel_auto 同逻辑；失败静默降级
# ----------------------------------------------------------------------------
def _travel_get(token, path):
    try:
        st, body = api_call(TRAVEL_BASE, path, token, method="GET")
        if st != 200 or not isinstance(body, dict) or body.get("code") != 0:
            return None
        return body.get("data")
    except Exception:
        return None


def run_travel(token, location_id=None):
    """返回结构化旅行结果；任何失败都标记 available=False 但不上抛。"""
    out = {"available": False, "state": None, "state_text": None,
           "auto_log": [], "claim": None, "depart": None, "final_state": None}
    raw_log = []

    status = _travel_get(token, TRAVEL_STATUS_PATH)
    if status is None:
        out["auto_log"].append("查询旅行状态失败（接口不可达或鉴权失效），已跳过旅行写操作")
        out["raw"] = raw_log
        return out
    raw_log.append(("GET travel/status", 200, _deep_mask({"code": 0, "data": status})))

    # 先领：已到达则领积分
    if status.get("state") == "arrived":
        ck_code, ck_body = api_call(TRAVEL_BASE, TRAVEL_CLAIM_PATH, token, payload={})
        raw_log.append(("POST travel/claim", ck_code, _deep_mask(ck_body)))
        if ck_code == 200 and isinstance(ck_body, dict) and ck_body.get("code") == 0:
            credit = (ck_body.get("data") or {}).get("reward_credit")
            out["claim"] = {"success": True, "reward_credit": credit}
            out["auto_log"].append("已领取到家的旅行奖励 +%s 积分" % (credit if credit is not None else "?"))
            fresh = _travel_get(token, TRAVEL_STATUS_PATH)
            if fresh is not None:
                status = fresh
        else:
            out["claim"] = {"success": False,
                            "message": (ck_body.get("msg") if isinstance(ck_body, dict) else None) or ("HTTP %s" % ck_code)}
            out["auto_log"].append("领取旅行奖励失败：%s" % out["claim"]["message"])

    # 后派：idle 且未达每日上限才派
    if status.get("daily_limit_reached"):
        out["auto_log"].append("今日派遣次数已用完，跳过派遣")
    elif status.get("state") in (None, "", "idle"):
        config = _travel_get(token, TRAVEL_CONFIG_PATH) or {}
        ids = [loc.get("id") for loc in (config.get("locations") or []) if loc.get("id") is not None]
        chosen = location_id
        if chosen is None:
            chosen = random.choice(ids) if ids else random.choice([1, 2, 3, 4])
        dp_code, dp_body = api_call(TRAVEL_BASE, TRAVEL_DEPART_PATH, token,
                                   payload={"location_id": chosen})
        raw_log.append(("POST travel/depart", dp_code, _deep_mask(dp_body)))
        if dp_code == 200 and isinstance(dp_body, dict) and dp_body.get("code") == 0:
            loc = (dp_body.get("data") or {}).get("location") or {}
            out["depart"] = {"success": True, "location_id": chosen,
                             "location_name": loc.get("name") or LOCATION_NAMES.get(chosen, "地点%s" % chosen)}
            out["auto_log"].append("已派出 Buddy 前往【%s】" % out["depart"]["location_name"])
        else:
            out["depart"] = {"success": False,
                             "message": (dp_body.get("msg") if isinstance(dp_body, dict) else None) or ("HTTP %s" % dp_code)}
            out["auto_log"].append("派出 Buddy 失败：%s" % out["depart"]["message"])
    else:
        out["auto_log"].append("Buddy 正在旅行中，今天已派过，跳过派遣")

    final = _travel_get(token, TRAVEL_STATUS_PATH) or status
    out["available"] = True
    out["state"] = final.get("state") if isinstance(final, dict) else None
    out["state_text"] = TRAVEL_STATE_TEXT.get(out["state"], out["state"])
    out["final_state"] = final
    out["raw"] = raw_log
    return out


def _extract_balance(*bodies):
    candidates = ("total_credits", "total_credit", "total_credit_balance", "total_points",
                  "points_balance", "credit_balance", "balance", "remain_credit",
                  "remain_credits", "remain", "score", "credits", "integral",
                  "totalCredit", "totalCredits", "pointsBalance", "balanceCredit")
    sections = ("", "data", "result", "data.result")
    for body in bodies:
        if not isinstance(body, dict):
            continue
        for sec in sections:
            node = body
            for part in sec.split(".") if sec else []:
                node = node.get(part) if isinstance(node, dict) else None
                if node is None:
                    break
            if not isinstance(node, dict):
                node = body if sec == "" else None
            if not isinstance(node, dict):
                continue
            for k in candidates:
                v = node.get(k)
                if isinstance(v, (int, float)):
                    return v
    return None


# ----------------------------------------------------------------------------
# 飞书推送（签到 / 猫猫 分两段，结论独立）
# ----------------------------------------------------------------------------
def feishu_sign(secret, timestamp):
    # 飞书官方加签算法：以 "timestamp\nsecret" 整串作为 HMAC-SHA256 的 key，消息为空
    string_to_sign = "%d\n%s" % (timestamp, secret)
    mac = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


def push_feishu(webhook, secret, title, content):
    if not webhook:
        return {"ok": False, "detail": "飞书 webhook 未配置（WB_FEISHU_WEBHOOK）"}
    payload = {"msg_type": "text", "content": {"text": "%s\n%s" % (title, content)}}
    url = webhook
    if secret:
        ts = int(time.time())
        sign = feishu_sign(secret, ts)
        sep = "&" if "?" in url else "?"
        # 必须对 sign 做 URL 编码：base64 含 + / =，裸拼进 query 会被 URL 解析篡改 → 飞书 19021
        url = "%s%stimestamp=%d&sign=%s" % (url, sep, ts, urllib.parse.quote(sign, safe=""))
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen_smart(req, HTTP_TIMEOUT) as resp:
            code = resp.getcode()
            body = json.loads(resp.read().decode("utf-8", "replace"))
        return {"ok": (code == 200 and body.get("code") == 0), "http": code, "resp": _deep_mask(body)}
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            body = {}
        return {"ok": False, "http": e.code, "resp": _deep_mask(body)}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:200]}


def _bj_date():
    """北京时间(UTC+8)日期，飞书标签对齐用户时区（服务端按 UTC 自然日记账）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600))

def build_feishu_content(checkin, travel, renew=None):
    today = _bj_date()
    lines = []
    lines.append("📅 WorkBuddy 每日签到 · %s" % today)
    lines.append("")

    # —— 签到段 ——
    lines.append("【签到】")
    if checkin.get("status") == "ok":
        if checkin.get("action") == "skip_already_signed":
            lines.append("状态：今日已签到（幂等跳过）")
        else:
            lines.append("状态：领取成功")
            if checkin.get("points") is not None:
                lines.append("本次积分：+%s" % checkin["points"])
        if checkin.get("detail", {}).get("streak_days") is not None:
            lines.append("连续天数：%s 天" % checkin["detail"]["streak_days"])
        if checkin.get("balance") is not None:
            lines.append("积分余额：%s" % checkin["balance"])
    else:
        lines.append("状态：失败")
        lines.append("原因：%s" % (checkin.get("msg") or "未知"))
    lines.append("")

    # —— 猫猫段（失败不影响签到结论，如实记录）——
    lines.append("【猫猫旅行】")
    if not travel.get("available"):
        lines.append("状态：未操作（%s）" % "；".join(travel.get("auto_log") or ["接口不可用"]))
    else:
        lines.append("当前状态：%s" % (travel.get("state_text") or travel.get("state")))
        if travel.get("claim") and travel["claim"].get("success"):
            lines.append("领取：+%s 积分（已领到家的奖励）" % travel["claim"].get("reward_credit"))
        elif travel.get("claim") and not travel["claim"].get("success"):
            lines.append("领取：失败（%s）" % travel["claim"].get("message"))
        if travel.get("depart") and travel["depart"].get("success"):
            lines.append("派遣：已派往【%s】" % travel["depart"].get("location_name"))
        elif travel.get("depart") and not travel["depart"].get("success"):
            lines.append("派遣：失败（%s）" % travel["depart"].get("message"))
        # 没有 claim/depart 动作时，给出状态说明
        if not travel.get("claim") and not travel.get("depart"):
            lines.append("动作：%s" % "；".join(travel.get("auto_log") or ["无"]))

    # —— 结论（签到成功即成功）——
    ok = checkin.get("status") == "ok"
    lines.append("")
    lines.append("【结论】%s（猫猫是否成功不影响本结论）" %
                 ("整体成功 ✅" if ok else "签到失败 ❌"))

    # —— 自续期告警：仅当配置了 WB_GH_PAT 但续期失败时，提醒重生成 token ——
    # 触发条件：renew.enabled=True（说明 WB_GH_PAT 已配置）且 renew.ok=False（写回失败，
    # 典型为 token 被删除/过期 → GitHub 返回 401）。此时日常签到不受影响，仅自动续期暂停。
    if renew and renew.get("enabled") and not renew.get("ok"):
        lines.append("")
        lines.append("【⚠️ 自续期失败 · 需你处理】")
        lines.append("原因：%s" % (renew.get("detail") or "WB_GH_PAT 无效或权限不足"))
        lines.append("处理步骤：")
        lines.append("1) GitHub → Settings → Developer settings → Fine-grained tokens，")
        lines.append("   把失效的 PAT 删除，重新生成一条：")
        lines.append("   Repository access 选 simona20261008-ctrl/workbuddy；")
        lines.append("   Permissions 勾 Contents / Workflows / Secrets 全部 Read and write。")
        lines.append("2) 把新 token 发给 IA，由 IA 更新仓库 WB_GH_PAT Secret；")
        lines.append("   或你直接在仓库 Settings → Secrets → Actions 改 WB_GH_PAT 的值。")
        lines.append("注：日常签到不受影响，仅令牌自动续期暂停；")
        lines.append("在 WB_REFRESH_TOKEN 到期前，自动化仍可正常运行。")

    return "\n".join(lines)


# ----------------------------------------------------------------------------
# 云端自续期：把刷新得到的「新 refreshToken」写回 GitHub Secret。
# 作用：长效令牌本身只有 ~30 天有效期（实测 refreshExpiresIn=2592000）。
#   不写回 → 每 ~30 天必须开一次本机重注入，与你「无常开设备」诉求冲突；
#   写回   → 云端每日刷新即自动续期，本机只需最初注入一次，之后永远关机也能跑。
# 仅在配置了 WB_GH_PAT（对该仓库具备 Actions secrets 写权限的 PAT）时启用。
# ----------------------------------------------------------------------------
def renew_github_secret(new_refresh_token):
    pat = os.environ.get("WB_GH_PAT", "").strip()
    if not pat:
        return {"enabled": False,
                "detail": "未配置 WB_GH_PAT，跳过自续期（令牌 ~30 天有效，到期需重跑本地注入）"}
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()  # GitHub Actions 自动注入 owner/name
    if not repo:
        return {"enabled": True, "ok": False,
                "detail": "缺少 GITHUB_REPOSITORY（非 GitHub Actions 环境？）"}
    try:
        from nacl import encoding, public
    except Exception as e:
        return {"enabled": True, "ok": False, "detail": "缺少 PyNaCl：%s" % str(e)[:120]}
    # 1) 取仓库公钥
    api = "https://api.github.com/repos/%s/actions/secrets/public-key" % repo
    req = urllib.request.Request(api, headers={
        "Authorization": "Bearer %s" % pat,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urlopen_smart(req, 15) as resp:
            pk = json.loads(resp.read().decode("utf-8"))
        key, key_id = pk["key"], pk["key_id"]
    except Exception as e:
        return {"enabled": True, "ok": False, "detail": "取公钥失败: %s" % str(e)[:160]}
    # 2) libsodium sealed box 加密（标准库无 NaCl，需 PyNaCl）
    try:
        pub = public.PublicKey(key.encode("utf-8"), encoding.Base64Encoder())
        box = public.SealedBox(pub)
        enc = base64.b64encode(box.encrypt(new_refresh_token.encode("utf-8"))).decode("utf-8")
    except Exception as e:
        return {"enabled": True, "ok": False, "detail": "加密失败: %s" % str(e)[:160]}
    # 3) PUT 写回 WB_REFRESH_TOKEN
    api2 = "https://api.github.com/repos/%s/actions/secrets/WB_REFRESH_TOKEN" % repo
    body = json.dumps({"encrypted_value": enc, "key_id": key_id}).encode("utf-8")
    req2 = urllib.request.Request(api2, data=body, method="PUT", headers={
        "Authorization": "Bearer %s" % pat,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urlopen_smart(req2, 15) as resp:
            return {"enabled": True, "ok": (resp.getcode() in (201, 204)),
                    "http": resp.getcode(), "detail": "已写回 WB_REFRESH_TOKEN（自续期生效）"}
    except urllib.error.HTTPError as e:
        return {"enabled": True, "ok": False, "detail": "写回失败 HTTP %s" % e.code}
    except Exception as e:
        return {"enabled": True, "ok": False, "detail": "写回异常: %s" % str(e)[:160]}


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def run(dry_run=False, location_id=None):
    rt = os.environ.get("WB_REFRESH_TOKEN", "").strip()
    feishu_webhook = os.environ.get("WB_FEISHU_WEBHOOK", "").strip()
    feishu_secret = os.environ.get("WB_FEISHU_SECRET", "").strip()
    gh_pat = os.environ.get("WB_GH_PAT", "").strip()

    # 预检：在 Actions 日志一眼看清哪些 Secret 已配置（绝不回显明文，只显示长度）
    def _ss(name, val):
        return "%s=%s" % (name, ("set(%d)" % len(val)) if val else "MISSING")
    print("[preflight] %s | %s | %s | %s" % (
        _ss("WB_REFRESH_TOKEN", rt),
        _ss("WB_FEISHU_WEBHOOK", feishu_webhook),
        _ss("WB_FEISHU_SECRET", feishu_secret),
        _ss("WB_GH_PAT", gh_pat)))

    print("[info] 飞书 webhook: %s" % mask_webhook(feishu_webhook))
    print("[info] 长效令牌(masked): %s" % mask_token(rt))

    # dry-run 允许无真实令牌：用占位令牌跑通脱敏样例，不联网
    if dry_run and not rt:
        rt = "DRYRUN_PLACEHOLDER_TOKEN"

    if not rt:
        msg = "缺少 WB_REFRESH_TOKEN（请在 GitHub 仓库 Secrets 配置；用 refresh_token_local.py 生成本机令牌并注入）"
        print("[error] %s" % msg)
        return 1, {"status": "error", "msg": msg}

    # 1) 刷新令牌
    if dry_run:
        refresh_code, refresh_body = _dry_refresh()
    else:
        refresh_code, refresh_body = refresh_token(rt)
    print("[info] 刷新接口 http=%s code=%s" %
          (refresh_code, refresh_body.get("code") if isinstance(refresh_body, dict) else "?"))
    renew = {"enabled": False, "detail": "刷新未成功，跳过自续期"}
    if not isinstance(refresh_body, dict) or refresh_body.get("code") != 0:
        rb = refresh_body.get("msg") if isinstance(refresh_body, dict) else str(refresh_body)
        checkin = {"status": "error", "msg": "长效令牌刷新失败：%s（请重新运行 refresh_token_local.py 注入最新令牌）" % rb}
        travel = {"available": False, "auto_log": ["令牌刷新失败，跳过旅行"]}
    else:
        at = refresh_body.get("data", {}).get("accessToken") or ""
        new_rt = refresh_body.get("data", {}).get("refreshToken") or ""
        renew = (renew_github_secret(new_rt) if new_rt
                 else {"enabled": False, "detail": "刷新应答无新 refreshToken"})
        print("[info] 自续期: %s" % renew.get("detail", ""))
        checkin = do_checkin(at) if not dry_run else _dry_checkin()
        travel = run_travel(at, location_id=location_id) if not dry_run else _dry_travel()

    # 2) 组装飞书内容并推送（猫猫失败不影响结论；自续期失败会附带告警段）
    content = build_feishu_content(checkin, travel, renew)
    title = "WorkBuddy 每日签到 · %s" % _bj_date()
    push_result = {}
    if feishu_webhook:
        push_result = push_feishu(feishu_webhook, feishu_secret, title, content)
        print("[info] 飞书推送: %s" % ("成功" if push_result.get("ok") else "失败 %s" % push_result.get("detail", push_result.get("http"))))
        if not push_result.get("ok") and push_result.get("resp") is not None:
            print("[info] 飞书响应: %s" % json.dumps(push_result["resp"], ensure_ascii=False)[:300])
    else:
        print("[warn] 未配置 WB_FEISHU_WEBHOOK，跳过飞书推送（仍会输出结果）")

    # 3) 结果
    result = {
        "version": VERSION,
        "checkin": {k: v for k, v in checkin.items() if k != "detail"},
        "travel": {k: v for k, v in travel.items() if k not in ("final_state", "raw")},
        "feishu": {"configured": bool(feishu_webhook),
                   "ok": push_result.get("ok") if feishu_webhook else None},
        # 验收用：脱敏原始返回
        "raw_responses": {
            "refresh": _deep_mask(refresh_body),
            "checkin": checkin.get("detail", {}).get("raw"),
            "travel": travel.get("raw"),
        },
        "renew": renew,
        "conclusion": "success" if checkin.get("status") == "ok" else "failed",
    }
    print("=" * 60)
    print(content)
    print("=" * 60)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    # 结论：签到成功即整体成功（猫猫失败不影响退出码）
    checkin_ok = checkin.get("status") == "ok"
    # 通知兜底：若配置了飞书但推送失败，让 GitHub Actions 判定为失败，
    # 触发 GitHub 自带的失败通知（邮件 / Web 通知），避免“签到成功但通知没送达”的静默丢失。
    if feishu_webhook:
        notify_ok = bool(push_result.get("ok"))
    else:
        notify_ok = True  # 未配置飞书：视为无需通知，不因此判定失败
    exit_code = 0 if (checkin_ok and notify_ok) else 1
    return exit_code, result


# ----------------------------------------------------------------------------
# 本地 dry-run：不联网，用贴合真实契约的脱敏样例跑通全链路，供验收展示
# ----------------------------------------------------------------------------
def _dry_refresh():
    return 200, {
        "code": 0,
        "data": {
            "accessToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VyIjoiZHJ5X3J1biIsImlhdCI6MTc1OTExMjAwMH0.abcDEF1234567890xyz",
            "refreshToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJydCI6InRlc3QifQ.rtTOKENabcDEF1234567890",
            "expiresIn": 7200,
        },
    }


def _dry_checkin():
    return {
        "status": "ok", "action": "clicked", "points": 100, "balance": 1340,
        "msg": "领取成功，+100 积分，连续第 13 天",
        "detail": {"streak_days": 13, "status_http": 200, "checkin_http": 200,
                   "balance": 1340, "raw": [
            ("POST checkin-activity-status", 200, {"code": 0, "data": {"today_checked_in": False,
                                                                      "streak_days": 12, "daily_credit": 100, "total_credits": 1240}}),
            ("POST daily-checkin", 200, {"code": 0, "credit": 100, "streak_days": 13,
                                        "data": {"credit": 100, "daily_credit": 100}}),
        ]},
    }


def _dry_travel():
    now = int(time.time())
    return {
        "available": True, "state": "traveling", "state_text": "旅行中",
        "claim": {"success": True, "reward_credit": 8},
        "depart": {"success": True, "location_id": 3, "location_name": "健身房"},
        "auto_log": ["已领取到家的旅行奖励 +8 积分", "已派出 Buddy 前往【健身房】"],
        "raw": [
            ("GET travel/status", 200, {"code": 0, "data": {"state": "arrived",
                                                            "location": {"id": 1, "name": "咖啡馆"}, "reward_credit": 8,
                                                            "arrive_at": now - 10, "server_now": now,
                                                            "daily_limit_reached": False, "record_id": 123}}),
            ("POST travel/claim", 200, {"code": 0, "data": {"reward_credit": 8}}),
            ("GET travel/status", 200, {"code": 0, "data": {"state": "idle",
                                                            "daily_limit_reached": False}}),
            ("POST travel/depart", 200, {"code": 0, "data": {"location": {"id": 3, "name": "健身房"},
                                                             "arrive_at": now + 7200}}),
            ("GET travel/status", 200, {"code": 0, "data": {"state": "traveling",
                                                            "daily_limit_reached": False}}),
        ],
    }


def main():
    argv = sys.argv[1:]
    dry_run = "--dry-run" in argv or "-n" in argv
    location_id = None
    if "--location" in argv:
        try:
            i = argv.index("--location")
            location_id = int(argv[i + 1])
        except (ValueError, IndexError):
            pass
    try:
        code, _ = run(dry_run=dry_run, location_id=location_id)
        return code
    except Exception as e:
        # 兜底：任何未预期崩溃都尽量发飞书告警，避免静默失败（无飞书则仅打印）
        import traceback
        tb = traceback.format_exc(limit=6)
        wh = os.environ.get("WB_FEISHU_WEBHOOK", "").strip()
        sec = os.environ.get("WB_FEISHU_SECRET", "").strip()
        if wh:
            msg = ("【❌ 运行异常 · 未预期崩溃】\n%s\n\n请查看 Actions 运行日志。\n"
                   "常见原因：WB_REFRESH_TOKEN 失效需重注入、WB_GH_PAT 无效导致自续期异常、"
                   "或云端依赖缺失。" % (tb if len(tb) < 900 else tb[:900]))
            try:
                push_feishu(wh, sec, "WorkBuddy 签到异常告警", msg)
            except Exception:
                pass
        print("[fatal] 未预期异常：%s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
