#!/usr/bin/env bash
# 一小时相处演示。全部用 --now 注入时间，不用真的等。
# 用法：bash demo.sh
set -e
cd "$(dirname "$0")"
DB="demo.db"
rm -f "$DB"
T0=1793090000000          # 固定起点，输出可复现
P="python3 refusal.py --db $DB"

hr() { printf '\n\033[2m%s\033[0m\n' "────────────────────────────────────────────────────"; }
say() { printf '  \033[36m她：%s\033[0m\n' "$1"; }
me()  { printf '\033[1m你：%s\033[0m\n' "$1"; }

$P init >/dev/null

hr; echo "【第一幕】你说了句难听的：「你怎么这么笨」"
me "你怎么这么笨"
echo "  ↓ judge 生成判定提示词（含她的档案、当前情绪、预算、允许时长）"
echo "  ↓ 模型给出判定："
$P apply --request "你怎么这么笨" --now $T0 \
  --json '{"refuse":true,"kind":"hard","minutes":15,"say":"……你才笨。","mood":"cold","intensity":0.7,"cause":"hurt","reason":"被说笨"}' \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("     kind=%s minutes=%s silence=#%s" % (d["kind"], d["minutes"], d["silence_id"]))'
say "……你才笨。"

hr; echo "【第二幕】半分钟后你追问「你生气啦？」"
me "你生气啦？"
echo "  ↓ 编排层先问闸门："
$P gate --now $((T0+30000)) --json || true
echo "  ↓ 退出码 10 = 沉默中，这一轮不调模型"
printf '  ↓ 但会发一句提示：'
$P notice --now $((T0+30000))
echo "  ↓ 再发两条，就只回「……」；第四条起彻底不回："
printf '     第 2 条 → '; $P notice --now $((T0+60000))
printf '     第 3 条 → '; $P notice --now $((T0+90000))
printf '     第 4 条 → （空）\n' 

hr; echo "【第三幕】十六分钟后"
$P gate --now $((T0+960000)) --json
echo "  ↓ 放行了，但先看残留情绪："
$P resume --now $((T0+960000)) --json | python3 -c 'import sys,json;print("     ",json.load(sys.stdin)["residual"])'
say "……干嘛。"

hr; echo "【第四幕】你问「你昨天去哪了」"
me "你昨天去哪了"
$P apply --request "你昨天去哪了" --now $((T0+970000)) \
  --json '{"refuse":false,"kind":"none","say":"等会儿告诉你。","mood":"upset","intensity":0.45,"commitment":{"text":"等会儿告诉你昨天去了哪","due_in":30}}' \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("     kind=%s（没有拒绝）silence=%s（不沉默）承诺=#%s" % (d["kind"], d["silence_id"], d["commitment_id"]))'
say "等会儿告诉你。"

hr; echo "【第五幕】31 分钟后，承诺到期"
$P due --now $((T0+2780000)) | python3 -c 'import sys,json;[print("     该起了：",c["text"]) for c in json.load(sys.stdin)["due"]]'
say "那个……昨天我去了很远的地方，没有光，只有会亮的鱼。"
$P fulfill 1 --now $((T0+2790000)) >/dev/null

hr; echo "【收尾】一致性审计"
$P audit --now $((T0+2790000))

hr; echo "【规则】连拒 3 次后自动降级"
for i in 1 2 3; do
  printf "  第 %d 次 → " $i
  $P apply --request "干活 $i" --now $((T0+2800000+i*1000)) \
    --json '{"refuse":true,"kind":"hard","minutes":10,"intensity":0.7,"mood":"upset"}' \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["kind"], d["minutes"], d["notes"] or "")'
done
printf "  安全类   → "
$P apply --request "快，家里出事了" --now $((T0+2900000)) \
  --json '{"refuse":true,"kind":"hard","minutes":20,"intensity":0.9,"mood":"cold"}' \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["kind"], d["minutes"], d["notes"])'

hr; echo "完。数据在 demo.db，删掉即重置。"
