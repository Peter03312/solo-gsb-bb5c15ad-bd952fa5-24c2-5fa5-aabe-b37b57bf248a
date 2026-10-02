#!/usr/bin/env bash
# 播放更正链计费争议（暂缓/解除）端到端调用示例。
# 前置：服务已启动（docker compose up -d，或本地 uvicorn）。
# 用法: BASE=http://localhost:8000 bash examples/disputes_demo.sh
set -u
BASE="${BASE:-http://localhost:8000}"
C="curl -sS"

say() { printf '\n=== %s ===\n' "$1"; }

say "健康检查"
$C "$BASE/health"; echo

say "1) 登记作品、协议（RS-A 70% / RS-B 30%）与一条播放 PLAY-D-1（100 分）"
$C -X POST "$BASE/works" -H 'Content-Type: application/json' \
  -d '{"code":"W-DISP-001","title":"争议演示录音"}'; echo
$C -X POST "$BASE/agreements" -H 'Content-Type: application/json' -d '{
  "work_code":"W-DISP-001",
  "valid_from":"2026-01-01T00:00:00+08:00",
  "valid_to":"2027-01-01T00:00:00+08:00",
  "shares":[{"holder_code":"RS-A","percent":"70"},{"holder_code":"RS-B","percent":"30"}]
}'; echo
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-D-1","work_code":"W-DISP-001",
       "occurred_at":"2026-02-10T20:00:00+08:00","amount":100}'; echo

PERIOD='{"start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}'

say "2) 正常试算：100 分 => RS-A 70 / RS-B 30"
$C "$BASE/statements/preview?start=2026-02-01T00:00:00%2B08:00&end=2026-03-01T00:00:00%2B08:00" \
  | python3 -m json.tool

say "3) 财务发现计费依据存疑：凭链内编号 PLAY-D-1 + 唯一争议号 DSP-DEMO-1 登记暂缓（201）"
$C -X POST "$BASE/play-disputes" -H 'Content-Type: application/json' \
  -d '{"play_import_id":"PLAY-D-1","dispute_ref":"DSP-DEMO-1","reason":"播放量来源待第三方核账"}' \
  | python3 -m json.tool

say "4) 暂缓期间试算：链尾列为争议待处理（reason_code=disputed），整链不计应付"
$C "$BASE/statements/preview?start=2026-02-01T00:00:00%2B08:00&end=2026-03-01T00:00:00%2B08:00" \
  | python3 -m json.tool

say "5) 暂缓期间出单：初次单无应付，待处理清单含争议链尾"
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' -d "$PERIOD" \
  | python3 -m json.tool

say "6) 暂缓期间新增更正（链尾前移到 PLAY-D-1-R1，金额 80）：自动继承暂缓"
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-D-1-R1","work_code":"W-DISP-001",
       "occurred_at":"2026-02-10T20:00:00+08:00","amount":80,
       "corrects_id":"PLAY-D-1"}' >/dev/null
$C "$BASE/play-disputes/DSP-DEMO-1" | python3 -m json.tool

say "7) 幂等与拒绝：同号同内容重试 200 duplicate；同链重复开启（新号）409"
$C -i -X POST "$BASE/play-disputes" -H 'Content-Type: application/json' \
  -d '{"play_import_id":"PLAY-D-1","dispute_ref":"DSP-DEMO-1","reason":"播放量来源待第三方核账"}' \
  | grep -E '^HTTP'
$C -i -X POST "$BASE/play-disputes" -H 'Content-Type: application/json' \
  -d '{"play_import_id":"PLAY-D-1-R1","dispute_ref":"DSP-DEMO-2","reason":"重复开启"}' \
  | grep -E '^HTTP'
$C -i -X POST "$BASE/play-disputes" -H 'Content-Type: application/json' \
  -d '{"play_import_id":"GHOST","dispute_ref":"DSP-DEMO-X","reason":"未知播放"}' \
  | grep -E '^HTTP'

say "8) 凭争议号解除（重复解除幂等 200 duplicate）"
$C -X POST "$BASE/play-disputes/DSP-DEMO-1/release" | python3 -m json.tool
$C -i -X POST "$BASE/play-disputes/DSP-DEMO-1/release" | grep -E '^HTTP'

say "9) 解除后试算：按最新链尾 PLAY-D-1-R1（80 分）与协议恢复 => 56/24"
$C "$BASE/statements/preview?start=2026-02-01T00:00:00%2B08:00&end=2026-03-01T00:00:00%2B08:00" \
  | python3 -m json.tool

say "10) 解除后再次出单：此前争议期间未计应付，本次按追补规则入账；应付无变化不出新单"
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' -d "$PERIOD" \
  | python3 -m json.tool
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' -d "$PERIOD" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("no-change note:", d.get("note"))'

say "11) 解除后可用新争议号重开；旧争议号同内容重试仍返回原（已解除）结果"
$C -X POST "$BASE/play-disputes" -H 'Content-Type: application/json' \
  -d '{"play_import_id":"PLAY-D-1-R1","dispute_ref":"DSP-DEMO-3","reason":"复核后再次暂缓"}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d["dispute_status"], d["current_tail_import_id"])'
$C -X POST "$BASE/play-disputes/DSP-DEMO-3/release" >/dev/null

say "完成"
