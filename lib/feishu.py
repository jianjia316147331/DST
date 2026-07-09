#!/usr/bin/env python3
"""lib/feishu.py — Feishu/Lark message sending, user search, event consumption."""
import json, os, sys, time

from .core import (_run, _run_silent, _read_stdin_text, _read_stdin_json,
                   _lark_cli_path, _lark_cli_base_cmd)

def cmd_gen_qr_msg():
    """Generate QR notification post message JSON.
    Args (JSON on stdin or CLI flags):
      --image-key KEY
      --platform "12123公安部" (省份信息，用于展示)
      --company "xxx公司"
      --date "2026-05-21"
      --target-type personal|group
      --user-id ou_xxx       (group @ target)
      --user-name 姓名        (group @ target)
    Output: JSON to stdout.
    """
    p = _parse_qr_msg_args()

    title = "🔑 自动查询12123违章信息 - 需要您扫码登录"
    platform_str = f"🌍 平台：{p['platform']}\n" if p.get('platform') else ""
    header_text = (
        f"📋 自动化查询12123车辆违章\n"
        f"{platform_str}"
        f"🏢 公司：{p['company']}\n"
        f"🕐 时间：{p['date']}\n\n"
        f"📱 请使用「交管12123」APP 扫描下方二维码登录\n\n"
    )

    if p["target_type"] == "group" and p.get("user_id"):
        reply_hint = "④ 登录成功后，在群中回复「已登录」"
        content = [
            [{"tag": "at", "user_id": p["user_id"], "user_name": p.get("user_name", "")},
             {"tag": "text", "text": f" 请扫码登录12123查询违章\n\n{header_text}"}],
            [{"tag": "img", "image_key": p["image_key"]}],
            [{"tag": "text", "text": f"\n📝 登录步骤：\n① 打开交管12123 APP\n② 扫一扫上方二维码\n③ 完成人脸识别\n{reply_hint}"}]
        ]
    else:
        reply_hint = "④ 登录成功后，在此飞书对话中回复「已登录」"
        content = [
            [{"tag": "text", "text": header_text}],
            [{"tag": "img", "image_key": p["image_key"]}],
            [{"tag": "text", "text": f"\n📝 登录步骤：\n① 打开交管12123 APP\n② 扫一扫上方二维码\n③ 完成人脸识别\n{reply_hint}"}]
        ]

    msg = {"zh_cn": {"title": title, "content": content}}
    print(json.dumps(msg, ensure_ascii=False))

def _parse_qr_msg_args():
    p = {"image_key": "", "platform": "", "company": "", "date": "",
         "target_type": "personal", "user_id": "", "user_name": ""}
    text = _read_stdin_text()
    if text:
        try:
            d = json.loads(text)
            p.update(d)
            return p
        except (json.JSONDecodeError, ValueError):
            pass
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--image-key" and i + 1 < len(args):
            p["image_key"] = args[i + 1]; i += 2
        elif args[i] == "--platform" and i + 1 < len(args):
            p["platform"] = args[i + 1]; i += 2
        elif args[i] == "--company" and i + 1 < len(args):
            p["company"] = args[i + 1]; i += 2
        elif args[i] == "--date" and i + 1 < len(args):
            p["date"] = args[i + 1]; i += 2
        elif args[i] == "--target-type" and i + 1 < len(args):
            p["target_type"] = args[i + 1]; i += 2
        elif args[i] == "--user-id" and i + 1 < len(args):
            p["user_id"] = args[i + 1]; i += 2
        elif args[i] == "--user-name" and i + 1 < len(args):
            p["user_name"] = args[i + 1]; i += 2
        else:
            i += 1
    return p
def cmd_gen_qr_fallback():
    """Generate fallback text-only post message JSON."""
    p = {"target_type": "personal", "user_id": "", "user_name": ""}
    _read_stdin_json(p)
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--target-type" and i + 1 < len(args):
            p["target_type"] = args[i + 1]; i += 2
        elif args[i] == "--user-id" and i + 1 < len(args):
            p["user_id"] = args[i + 1]; i += 2
        elif args[i] == "--user-name" and i + 1 < len(args):
            p["user_name"] = args[i + 1]; i += 2
        else:
            i += 1

    title = "🔑 自动查询12123违章信息 - 需要您扫码登录"
    if p["target_type"] == "group" and p.get("user_id"):
        content = [[
            {"tag": "at", "user_id": p["user_id"], "user_name": p.get("user_name", "")},
            {"tag": "text", "text": " 请扫码登录12123查询违章\n\n📝 登录步骤：\n① 打开交管12123 APP\n② 扫一扫上方二维码\n③ 完成人脸识别\n④ 登录成功后，在群中回复「已登录」"}
        ]]
    else:
        content = [[
            {"tag": "text", "text": "📱 请使用「交管12123」APP 扫描上方二维码登录\n\n📝 登录步骤：\n① 打开交管12123 APP\n② 扫一扫上方二维码\n③ 完成人脸识别\n④ 登录成功后，在此飞书对话中回复「已登录」"}
        ]]

    msg = {"zh_cn": {"title": title, "content": content}}
    print(json.dumps(msg, ensure_ascii=False))

def cmd_gen_result_msg():
    """Generate query completion notification post message JSON.
    Only includes a simple summary — no report/db paths, no Feishu docs."""
    p = {
        "company": "", "date": "", "vehicle_count": "0",
        "scanned_count": "0", "new_vehicle_count": "0",
        "new_violation_count": "0", "new_points": "0", "new_unpaid_fine": "0",
        "resolved_count": "0",
        "target_type": "personal", "user_id": "", "user_name": ""
    }
    _read_stdin_json(p)
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        for key in ["company", "date", "vehicle_count", "scanned_count",
                     "new_vehicle_count",
                     "new_violation_count", "new_points", "new_unpaid_fine",
                     "resolved_count",
                     "target_type", "user_id", "user_name"]:
            if args[i] == f"--{key.replace('_', '-')}" and i + 1 < len(args):
                p[key] = args[i + 1]; i += 2; break
        else:
            i += 1

    title = "✅ 12123违章查询完成"
    # Simple summary: 7 key metrics
    lines = [
        f"🏢 {p['company']}  🕐 {p['date']}",
        f"📋 扫描车辆：{p['scanned_count']} 台  🚗 查询车辆：{p['vehicle_count']} 台  🆕 新入库：{p['new_vehicle_count']} 台",
        f"⚠️ 新增违章：{p['new_violation_count']} 条",
        f"📛 新增扣分：{p['new_points']} 分",
        f"💰 新增待缴费：{p['new_unpaid_fine']} 元",
    ]
    # Show resolved count only if > 0
    if p.get("resolved_count") and p["resolved_count"] != "0":
        lines.append(f"✅ 对比历史已处理：{p['resolved_count']} 条")
    summary = "\n".join(lines)

    content_blocks = [[{"tag": "text", "text": f"{summary}\n\n数据来源于12123平台，仅供参考。"}]]

    msg = {"zh_cn": {"title": title, "content": content_blocks}}
    print(json.dumps(msg, ensure_ascii=False))

def cmd_upload_image():
    """Upload an image to Feishu and return image_key.
    Args: --dir /path/to/screenshots --file login_qrcode_xxx.png
    """
    p = {"dir": "", "file": ""}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--dir" and i + 1 < len(args):
            p["dir"] = args[i + 1]; i += 2
        elif args[i] == "--file" and i + 1 < len(args):
            p["file"] = args[i + 1]; i += 2
        else:
            i += 1

    lark = _lark_cli_path()
    result = _run(
        [lark, "im", "images", "create", "--as", "bot",
         "--file", f"image=./{p['file']}",
         "--data", '{"image_type":"message"}'],
        cwd=p["dir"]
    )

    image_key = ""
    try:
        d = json.loads(result.stdout)
        image_key = d.get("data", {}).get("image_key", "") or d.get("image_key", "")
    except (json.JSONDecodeError, ValueError):
        pass

    print(image_key)

def cmd_send_msg():
    """Send a post message via lark-cli.
    Args: --msg-file /path/to/msg.json [--user-id ou_xxx | --chat-id oc_xxx] [--as bot|user]
    """
    p = {"msg_file": "", "user_id": "", "chat_id": "", "as": "bot"}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--msg-file" and i + 1 < len(args):
            p["msg_file"] = args[i + 1]; i += 2
        elif args[i] == "--user-id" and i + 1 < len(args):
            p["user_id"] = args[i + 1]; i += 2
        elif args[i] == "--chat-id" and i + 1 < len(args):
            p["chat_id"] = args[i + 1]; i += 2
        elif args[i] == "--as" and i + 1 < len(args):
            p["as"] = args[i + 1]; i += 2
        else:
            i += 1

    with open(p["msg_file"], "r", encoding="utf-8") as f:
        content = f.read()

    lark = _lark_cli_path()
    cmd = [lark, "im", "+messages-send", "--as", p["as"],
           "--msg-type", "post", "--content", content]
    if p.get("chat_id"):
        cmd += ["--chat-id", p["chat_id"]]
    elif p.get("user_id"):
        cmd += ["--user-id", p["user_id"]]

    result = _run(cmd)
    # Validate response: must have ok=true and message_id, else fail loudly
    try:
        d = json.loads(result.stdout) if result.stdout and result.stdout.strip() else {}
        if not d.get("ok"):
            err = d.get("error", {}).get("message", result.stdout[:200])
            print(f"SEND_FAILED: {err}", file=sys.stderr)
            print(result.stdout, end="")
            if result.stderr:
                print(result.stderr, file=sys.stderr, end="")
            sys.exit(1)
        if not d.get("data", {}).get("message_id"):
            print(f"SEND_FAILED: no message_id in response", file=sys.stderr)
            print(result.stdout, end="")
            sys.exit(1)
    except json.JSONDecodeError:
        print(f"SEND_FAILED: invalid JSON response: {result.stdout[:200] if result.stdout else '(empty)'}", file=sys.stderr)
        print(result.stdout, end="") if result.stdout else None
        sys.exit(1)
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")

def cmd_send_image_msg():
    """Send an image message (fallback path)."""
    p = {"dir": "", "file": "", "user_id": "", "chat_id": ""}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--dir" and i + 1 < len(args):
            p["dir"] = args[i + 1]; i += 2
        elif args[i] == "--file" and i + 1 < len(args):
            p["file"] = args[i + 1]; i += 2
        elif args[i] == "--user-id" and i + 1 < len(args):
            p["user_id"] = args[i + 1]; i += 2
        elif args[i] == "--chat-id" and i + 1 < len(args):
            p["chat_id"] = args[i + 1]; i += 2
        else:
            i += 1

    lark = _lark_cli_path()
    cmd = [lark, "im", "+messages-send", "--as", "bot"]
    if p.get("chat_id"):
        cmd += ["--chat-id", p["chat_id"]]
    elif p.get("user_id"):
        cmd += ["--user-id", p["user_id"]]
    cmd += ["--image", f"./{p['file']}"]

    result = _run(cmd, cwd=p["dir"])
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")

def cmd_search_user():
    """Search Feishu user by name.
    Args: --query "张三" [--exclude-external-users]
    """
    p = {"query": "", "exclude_external": False}
    _read_stdin_json(p)
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--query" and i + 1 < len(args):
            p["query"] = args[i + 1]; i += 2
        elif args[i] == "--exclude-external-users":
            p["exclude_external"] = True; i += 1
        else:
            i += 1

    lark = _lark_cli_path()
    cmd = [lark, "contact", "+search-user", "--query", p["query"], "--as", "user"]
    if p["exclude_external"]:
        cmd.append("--exclude-external-users")

    result = _run(cmd)
    print(result.stdout, end="")

def cmd_search_chat():
    """Search Feishu group chat by name."""
    p = {"query": ""}
    _read_stdin_json(p)
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--query" and i + 1 < len(args):
            p["query"] = args[i + 1]; i += 2
        else:
            i += 1

    lark = _lark_cli_path()
    result = _run([
        lark, "api", "GET", "/open-apis/im/v1/chats/search",
        "--params", json.dumps({"query": p["query"], "page_size": 20}),
        "--as", "bot"
    ])
    print(result.stdout, end="")

def cmd_batch_get_id():
    """Look up Feishu user by mobile number."""
    p = {"mobile": ""}
    _read_stdin_json(p)
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--mobile" and i + 1 < len(args):
            p["mobile"] = args[i + 1]; i += 2
        else:
            i += 1

    lark = _lark_cli_path()
    result = _run([
        lark, "api", "POST", "/open-apis/contact/v3/users/batch_get_id",
        "--data", json.dumps({"mobiles": [p["mobile"]]}),
        "--params", json.dumps({"user_id_type": "open_id"}),
        "--as", "bot"
    ])
    print(result.stdout, end="")

def cmd_consume_event():
    """Run lark-cli event consume."""
    args = sys.argv[2:]
    lark = _lark_cli_path()
    result = _run([lark, "event", "consume"] + args)
    print(result.stdout, end="")

# ============================================================
def cmd_extract_message_id():
    """Extract message_id from lark-cli JSON response on stdin."""
    data = _read_stdin_text()
    try:
        d = json.loads(data)
        msg_id = d.get("data", {}).get("message_id", "") or d.get("message_id", "")
        if not msg_id:
            m = re.search(r'"message_id"\s*:\s*"([^"]+)"', data)
            if m:
                msg_id = m.group(1)
        print(msg_id)
    except (json.JSONDecodeError, ValueError):
        m = re.search(r'"message_id"\s*:\s*"([^"]+)"', data)
        print(m.group(1) if m else "")


