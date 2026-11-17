#!/usr/bin/env bash
# End-to-end demonstration of the delivery guarantees.
#
#   docker compose up -d --build       # from the repository root
#   ./scripts/demo.sh
#
# The script only uses curl + python3 (for JSON pretty output) and talks to
# the three containers.  It is idempotent-ish: re-running it just adds more
# demo mail, which is also a useful way to watch duplicate-report protection.
set -euo pipefail

APP_A="${APP_A:-http://localhost:8000}"
APP_B="${APP_B:-http://localhost:8001}"
DEMO="${DEMO:-http://localhost:8081}"

step() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
json() { python3 -m json.tool; }

wait_for() {
  local url="$1" name="$2" tries="${3:-60}"
  for _ in $(seq 1 "$tries"); do
    if curl -fsS "$url" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  echo "timeout waiting for $name ($url)" >&2
  return 1
}

step "等待服务就绪"
wait_for "$DEMO/healthz" "demo-imap" 120
wait_for "$APP_A/healthz" "web-a" 120
wait_for "$APP_B/healthz" "web-b" 120

deliver() { curl -fsS -X POST "$DEMO/ctl/deliver" -H 'Content-Type: application/json' -d "$1"; echo; }

step "1. 投递一封含两个附件的邮件，由 web-a 采集"
deliver '{"subject":"演示-双附件","attachments":[
  {"filename":"DEMO-R1.csv","report_no":"DEMO-R1","sample_no":"S001","item":"ALT","value":"21"},
  {"filename":"DEMO-R2.csv","report_no":"DEMO-R2","sample_no":"S002","item":"GLU","value":"5.4"}]}'
curl -fsS -X POST "$APP_A/api/scan" | json

step "2. 人工已读（模拟有人在邮箱里先打开）仍会被采集"
deliver '{"subject":"演示-人工已读","attachments":[
  {"filename":"DEMO-R3.csv","report_no":"DEMO-R3","sample_no":"S003","item":"K","value":"4.1"}]}'
# 演示源没有单独的“设为已读”接口，这里直接用 IMAP 客户端改标记。
python3 - "$DEMO" "${IMAP_HOST:-localhost}" "${IMAP_PORT:-1143}" <<'PY'
import imaplib, sys, json, urllib.request
host, port = sys.argv[2], int(sys.argv[3])
try:
    cfg = json.load(urllib.request.urlopen(sys.argv[1] + "/api/state"))
    uids = [m["uid"] for m in cfg["messages"] if m["subject"] == "演示-人工已读"]
except Exception as exc:  # noqa: BLE001
    print("skip pre-read step:", exc)
    raise SystemExit(0)
c = imaplib.IMAP4(host, port)
c.login("lab", "lab-pass")
c.select("INBOX")
for uid in uids:
    c.uid("STORE", str(uid), "+FLAGS.SILENT", "(\\Seen)")
c.logout()
print("marked pre-read message(s):", uids)
PY
curl -fsS -X POST "$APP_B/api/scan" | json

step "3. 同号同原件复用：新邮件带同样的 DEMO-R1 附件"
deliver '{"subject":"演示-复用原件","attachments":[
  {"filename":"DEMO-R1.csv","report_no":"DEMO-R1","sample_no":"S001","item":"ALT","value":"21"}]}'
curl -fsS -X POST "$APP_A/api/scan" | json

step "4. 同号异文整封拒收（DEMO-R2 内容不同），且不会带入其中的新报告号"
deliver '{"subject":"演示-同号异文","attachments":[
  {"filename":"DEMO-R9.csv","report_no":"DEMO-R9","sample_no":"S009","item":"NEW","value":"1"},
  {"filename":"DEMO-R2-bad.csv","content":"报告号,样本号,项目,测值\nDEMO-R2,S999,OTHER,999\n"}]}'
curl -fsS -X POST "$APP_A/api/scan" | json
curl -fsS "$APP_A/api/messages?mark_state=marked" \
  | python3 -c 'import json,sys
rows=json.load(sys.stdin)
bad=[r for r in rows if r["decision"]=="rejected"]
print(json.dumps(bad[:2], ensure_ascii=False, indent=2))
assert not any("DEMO-R9" in (r.get("report_nos") or "") for r in rows), "R9 leaked"'

step "4b. 单报告冲突裁决：对照现行报告与候选原文后确认（双实例）"
# Use a report number unique to this run so the demo can be re-run safely.
CONFLICT_NO="DEMO-C$(date +%s)$$"
echo "本轮裁决报告号 $CONFLICT_NO"
deliver "{\"subject\":\"演示-裁决基线\",\"attachments\":[
  {\"filename\":\"$CONFLICT_NO.csv\",\"report_no\":\"$CONFLICT_NO\",\"sample_no\":\"S010\",\"item\":\"GLU\",\"value\":\"5.4\"}]}"
curl -fsS -X POST "$APP_A/api/scan" | json
deliver "{\"subject\":\"演示-单报告更正\",\"attachments\":[
  {\"filename\":\"$CONFLICT_NO.csv\",\"report_no\":\"$CONFLICT_NO\",\"sample_no\":\"S010\",\"item\":\"GLU\",\"value\":\"6.8\"}]}"
curl -fsS -X POST "$APP_B/api/scan" | json
CONFLICT_ID=$(curl -fsS "$APP_A/api/conflicts" | CONFLICT_NO="$CONFLICT_NO" python3 -c 'import json,os,sys
rows=[r for r in json.load(sys.stdin) if r["report_no"]==os.environ["CONFLICT_NO"]]
assert rows, "expected a single-report conflict for the demo report number"
print(rows[0]["message"]["id"])')
echo "冲突邮件 #$CONFLICT_ID（仅一个合规 CSV 附件，原因 report_conflict）"

# 两个实例各自渲染页面，持有各自的 page_token 与所见现行版本。
curl -fsS "$APP_A/api/conflicts/$CONFLICT_ID" > /tmp/adj-a.json
curl -fsS "$APP_B/api/conflicts/$CONFLICT_ID" > /tmp/adj-b.json
python3 - <<'PY'
import json
a=json.load(open("/tmp/adj-a.json")); b=json.load(open("/tmp/adj-b.json"))
assert a["eligible"] and b["eligible"]
assert a["current"]["version"] == 1
print("现行:", a["current"]["version"], a["current"]["value"], a["current"]["attachment_sha256"][:12])
print("候选:", a["candidate"]["rows"][0][3], a["candidate"]["attachment_sha256"][:12])
PY

# web-b 先完成裁决。
APP_A="$APP_A" APP_B="$APP_B" python3 - <<'PY'
import json, os, urllib.request
b = json.load(open("/tmp/adj-b.json"))
payload = {
    "report_no": b["report_no"],
    "candidate_sha256": b["candidate"]["attachment_sha256"],
    "expected_current_version": b["current"]["version"],
    "expected_current_sha256": b["current"]["attachment_sha256"],
    "page_token": b["page_token"],
}
req = urllib.request.Request(
    f"{os.environ['APP_B']}/api/conflicts/{b['message']['id']}/confirm",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
print(json.load(urllib.request.urlopen(req)))
PY

# web-a 用旧页面确认：必须 409 且不新增版本。
APP_A="$APP_A" python3 - <<'PY'
import json, os, urllib.error, urllib.request
a = json.load(open("/tmp/adj-a.json"))
payload = {
    "report_no": a["report_no"],
    "candidate_sha256": a["candidate"]["attachment_sha256"],
    "expected_current_version": a["current"]["version"],
    "expected_current_sha256": a["current"]["attachment_sha256"],
    "page_token": a["page_token"],
}
req = urllib.request.Request(
    f"{os.environ['APP_A']}/api/conflicts/{a['message']['id']}/confirm",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    urllib.request.urlopen(req)
    raise SystemExit("stale confirmation unexpectedly succeeded")
except urllib.error.HTTPError as exc:
    assert exc.code == 409, exc.code
    print("过期确认被拒绝:", json.loads(exc.read())["detail"])
PY

# 重复提交同一裁决：幂等，不增加版本；两版附件与原始拒收回执均可下载。
curl -fsS -X POST "$APP_B/api/conflicts/$CONFLICT_ID/confirm" \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json
b=json.load(open("/tmp/adj-b.json"))
print(json.dumps({"report_no":b["report_no"],"candidate_sha256":b["candidate"]["attachment_sha256"],
 "expected_current_version":b["current"]["version"],
 "expected_current_sha256":b["current"]["attachment_sha256"],"page_token":b["page_token"]}))')" | json
curl -fsS "$APP_A/api/conflicts/$CONFLICT_ID" | python3 -c 'import json,sys
v=json.load(sys.stdin)
assert [x["version"] for x in v["versions"]] == [2,1], v["versions"]
assert {x["state"] for x in v["versions"]} == {"current","superseded"}
print("版本:", [(x["version"], x["state"], x["value"]) for x in v["versions"]])'
for VID in $(curl -fsS "$APP_A/api/conflicts/$CONFLICT_ID" | python3 -c 'import json,sys
print(" ".join(str(x["id"]) for x in json.load(sys.stdin)["versions"]))'); do
  curl -fsS "$APP_A/api/report-versions/$VID/attachment" | tail -1
done
curl -fsS "$APP_A/api/messages/$CONFLICT_ID/receipt" | python3 -c 'import json,sys; print("原始拒收回执:", json.load(sys.stdin)["type"])'

# 后续同号邮件按当前有效版本判定：重发新件=复用，重发旧件=冲突。
deliver "{\"subject\":\"演示-重发当前版本\",\"attachments\":[
  {\"filename\":\"$CONFLICT_NO.csv\",\"report_no\":\"$CONFLICT_NO\",\"sample_no\":\"S010\",\"item\":\"GLU\",\"value\":\"6.8\"}]}"
curl -fsS -X POST "$APP_A/api/scan" | python3 -c 'import json,sys
r=json.load(sys.stdin); print("重发当前版本: 复用", r["reused_reports"], "拒收", r["rejected"])
assert r["reused_reports"] == 1 and r["rejected"] == 0'
deliver "{\"subject\":\"演示-重发旧版本\",\"attachments\":[
  {\"filename\":\"$CONFLICT_NO-old.csv\",\"report_no\":\"$CONFLICT_NO\",\"sample_no\":\"S010\",\"item\":\"GLU\",\"value\":\"5.4\"}]}"
curl -fsS -X POST "$APP_B/api/scan" | python3 -c 'import json,sys
r=json.load(sys.stdin); print("重发旧版本: 复用", r["reused_reports"], "拒收", r["rejected"])
assert r["rejected"] == 1'

step "5. 标记后断连：STORE 成功但回执丢失，本地保持待标记"
curl -fsS -X POST "$DEMO/ctl/drop-store" -H 'Content-Type: application/json' -d '{"enabled":true}' >/dev/null
deliver '{"subject":"演示-标记后断连","attachments":[
  {"filename":"DEMO-R4.csv","report_no":"DEMO-R4","sample_no":"S004","item":"NA","value":"139"}]}'
curl -fsS -X POST "$APP_A/api/scan" | json
curl -fsS "$APP_A/api/status" | python3 -c 'import json,sys;print("pending:",json.load(sys.stdin)["stats"]["pending_marks"])'
step "5b. 页面/接口重试补标记"
curl -fsS -X POST "$APP_A/api/retry-marks" | json
curl -fsS "$APP_A/api/status" | python3 -c 'import json,sys
s=json.load(sys.stdin)["stats"]; print("pending:",s["pending_marks"],"marked:",s["marked"])'

step "6. 两实例并发：同一时刻只允许一个推进邮箱"
deliver '{"subject":"演示-并发","attachments":[
  {"filename":"DEMO-R5.csv","report_no":"DEMO-R5","sample_no":"S005","item":"BUN","value":"6.2"}]}'
(curl -fsS -o /tmp/scan-a.json -w "web-a: %{http_code}\n" -X POST "$APP_A/api/scan" &
 curl -fsS -o /tmp/scan-b.json -w "web-b: %{http_code}\n" -X POST "$APP_B/api/scan" &
 wait) || true
cat /tmp/scan-a.json 2>/dev/null | json || true
cat /tmp/scan-b.json 2>/dev/null | json || true

step "7. 邮箱重建：UIDVALIDITY 变化 + UID 1 被新邮件重用"
# 7a. 先制造一条“标记后断连”的待标记记录，它属于旧 UIDVALIDITY。
curl -fsS -X POST "$DEMO/ctl/drop-store" -H 'Content-Type: application/json' -d '{"enabled":true}' >/dev/null
deliver '{"subject":"演示-重建前待标记","attachments":[
  {"filename":"DEMO-R7.csv","report_no":"DEMO-R7","sample_no":"S007","item":"AST","value":"33"}]}'
curl -fsS -X POST "$APP_B/api/scan" | json
BEFORE_UIDVALIDITY=$(curl -fsS "$APP_B/api/status" | python3 -c 'import json,sys;print(json.load(sys.stdin)["mailbox_state"]["uidvalidity"])')
BEFORE_RAW=$(curl -fsS "$APP_A/api/messages?mark_state=pending" | python3 -c 'import json,sys;rows=json.load(sys.stdin);print(rows[0]["raw_sha256"] if rows else "none")')
echo "旧代 uidvalidity=$BEFORE_UIDVALIDITY，待标记原文=${BEFORE_RAW:0:16}…"

# 7b. 清空重建邮箱（UIDVALIDITY+1，UID 从 1 重新分配），再投递全新邮件。
curl -fsS -X POST "$DEMO/ctl/rebuild" -H 'Content-Type: application/json' -d '{"purge":true}' | json
deliver '{"subject":"演示-重建后新邮件-UID1","attachments":[
  {"filename":"DEMO-R6.csv","report_no":"DEMO-R6","sample_no":"S006","item":"AST","value":"44"}]}'
curl -fsS -X POST "$APP_B/api/scan" | json
curl -fsS "$APP_B/api/status" | python3 -c 'import json,sys
d=json.load(sys.stdin); print(json.dumps({"mailbox_state":d["mailbox_state"],"stats":d["stats"]}, ensure_ascii=False, indent=2))'

# 7c. 断言：旧待标记记录变成“旧代失效”，新 UID 1 是另一封邮件，且两代互不串用。
curl -fsS "$APP_A/api/messages?mark_state=stale" | python3 -c "import json,sys
rows=json.load(sys.stdin)
quarantined=[r for r in rows if r['raw_sha256']=='$BEFORE_RAW']
assert quarantined, 'expected the old pending mark to be quarantined as stale'
assert quarantined[0]['uidvalidity']==int('$BEFORE_UIDVALIDITY'), 'quarantined row must belong to old UIDVALIDITY'
print('旧代 pending -> stale:', [(r['uidvalidity'], r['uid'], r['raw_sha256'][:12]) for r in quarantined])"
curl -fsS "$APP_A/api/messages" | python3 -c "import json,sys
rows=json.load(sys.stdin)
new=[r for r in rows if r['uidvalidity']!=int('$BEFORE_UIDVALIDITY') and r['uid']==1]
assert new and new[0]['decision']=='accepted' and 'DEMO-R6' in new[0]['report_nos'], new
assert new[0]['raw_sha256'] != '$BEFORE_RAW', 'UID 1 must not inherit the old receipt'
print('新代 UID 1:', new[0]['decision'], new[0]['mark_state'], new[0]['report_nos'], new[0]['raw_sha256'][:12])"

step "8. 最终入库报告与标记状态"
curl -fsS "$APP_A/api/messages?mark_state=marked" | python3 -c 'import json,sys
rows=json.load(sys.stdin)
for r in sorted(rows, key=lambda x: (x["uidvalidity"], x["uid"]))[-10:]:
    print(r["uidvalidity"], r["uid"], r["decision"], r["mark_state"], r["report_nos"], r["reason"] or "")'

echo
echo "全部演示步骤完成。浏览器打开 $APP_A 或 $APP_B 查看页面。"
