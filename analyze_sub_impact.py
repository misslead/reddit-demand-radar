"""分析：填了真实订阅数之后，触达上限到底什么时候才成为瓶颈。

paying = min(authors × amplify, Σsub可触达 × reach_cap_ratio) × conv
触达上限成为瓶颈的条件：authors > Σsub可触达 × reach_cap_ratio / amplify
"""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCORED = HERE / "review" / "scored.json"
ECON = json.loads((HERE / "config" / "econ.json").read_text(encoding="utf-8"))
OUT = HERE / "review" / "_sub_size_impact.txt"

lines = []
lines.append(f"参数：amplify={ECON['amplify']}  reach_cap_ratio={ECON['reach_cap_ratio']}  "
             f"sub_source_discount={ECON.get('sub_source_discount')}  "
             f"low_conf_discount={ECON.get('low_conf_discount')}  "
             f"assumed_sub_discount={ECON.get('assumed_sub_discount')}")
lines.append("")

d = json.loads(SCORED.read_text(encoding="utf-8"))
lines.append(f"econ_hash = {d['econ_hash']}")
lines.append(f"已填订阅数 {len(d.get('sub_size_snapshot', {}))} 个 / 完全未知的 {len(d.get('sub_size_assumed', []))} 个 "
             f"/ 低置信 {len(d.get('sub_size_low_conf', []))} 个")
lines.append("")

hdr = f"{'簇':<14}{'作者数':>6}{'Σsub可触达':>14}{'触达上限(5%)':>14}{'作者数×300':>12}{'瓶颈临界作者数':>16}  结论"
lines.append(hdr)
lines.append("-" * len(hdr))
for it in d["items"]:
    e = it.get("econ", {})
    reach = e.get("reach_cap_users")
    paying = e.get("paying_users_est")
    authors = next((c.get("unique_authors") for c in [it] if c), None)
    # 阈值：authors > reach_cap / amplify
    crit = (reach / ECON["amplify"]) if reach else None
    binding = "触达受限" if (crit is not None and authors and authors > crit) else "作者数受限（触达远未触顶）"
    lines.append(f"{it['cluster_id']:<14}{authors:>6}{(reach or 0)/max(ECON['reach_cap_ratio'],1e-9):>14,.0f}"
                 f"{reach:>14,.0f}{authors*ECON['amplify']:>12,.0f}"
                 f"{(f'{crit:,.0f}' if crit else '-'):>16}  {binding}")
lines.append("")
lines.append("读法：'作者数×300' < '触达上限(5%)' -> 就算再多作者也轮不到触达上限起作用，")
lines.append("      也就是说 ROI 已经被 amplify 卡住，订阅数填得准不准都改变不了结论。")
lines.append("")
lines.append("各簇结论：")
for it in d["items"]:
    e = it.get("econ", {})
    reach = e.get("reach_cap_users")
    a = it.get("unique_authors")
    crit = (reach / ECON["amplify"]) if reach else 0
    lines.append(f"  {it['cluster_id']:<14} 当前 {a:>3} 作者；需 >{crit:,.0f} 作者 触达上限才会成为瓶颈"
                 f"（约 {crit/max(a,1):,.0f} 倍于当前）")

OUT.write_text("\n".join(lines), encoding="utf-8")
print("wrote", OUT)
